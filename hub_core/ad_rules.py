"""不動産広告の表示チェック（マイソク・LINE物件カード・ポータル掲載文）。

なぜ要るか。マイソクのキャッチコピーは店主が自由に打つ欄で、そのままA4に焼かれて
配られる。「日当たり抜群」「掘出し物件」のような言葉は、不動産の表示に関する
公正競争規約が名指しで制限している。書いた本人に違反の意図が無くても、
広告主（宅建業者）が責任を負う。だから**出す前に機械で見る**。

設計方針
  - fail-closed。判定できない入力は「問題なし」でなく「要確認」に倒す。
  - 既定は厳しく、緩める側（正当な使い方）だけを理由付きで列挙する（allowlist向き）。
    緩めた1件ごとに負例テストを置く（`test_ad_rules.py`）。
  - **自動で書き換えない。** 言い換え案は出すが、差し替えるのは人。広告表現の
    最終責任は広告主にあり、ソフトが黙って本文を変えると誰の表現か分からなくなる。
  - 語は NFKC 正規化してから当てる（「ＮＯ．１」「no.1」を同じに見る）。

出典（条文の文言は `SOURCES` に、各規則の `source` から引く）
  - 宅地建物取引業法 32条（誇大広告等の禁止）・34条（取引態様の明示）
  - 不当景品類及び不当表示防止法 5条（優良誤認・有利誤認）
  - 不動産の表示に関する公正競争規約／同施行規則
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field as _dc_field

# ---------------------------------------------------------------- 出典

SOURCES: dict[str, str] = {
    "gyouhou32": "宅地建物取引業法 第32条（誇大広告等の禁止・違反は法81条1号の罰則対象）",
    "gyouhou34": "宅地建物取引業法 第34条第1項（取引態様の明示）",
    "keihyou5": "不当景品類及び不当表示防止法 第5条（優良誤認・有利誤認）",
    "kiyaku18_1": "不動産の表示に関する公正競争規約 第18条第1項（特定用語の使用基準）",
    "kiyaku18_2_1": "同 第18条第2項第1号（最上級を意味する用語）",
    "kiyaku18_2_2": "同 第18条第2項第2号（著しく安いという印象を与える用語）",
    "kiyaku18_2_3": "同 第18条第2項第3号（全く欠けるところがないことを意味する用語）",
    "kiyaku18_2_4": "同 第18条第2項第4号（競争事業者よりも優位に立つことを意味する用語）",
    "kiyaku18_2_5": "同 第18条第2項第5号（一定の基準により選別されたことを意味する用語）",
    "kiyaku18_2_6": "同 第18条第2項第6号（著しく人気が高いという印象を与える用語）",
    "kiyaku21": "同 第21条（おとり広告の禁止）",
    "kiyaku23_21": "同 第23条第1項第21号（増改築した建物を新築と誤認させる表示）",
    "kiyaku23_70": "同 第23条第1項第70号（完売していないのに完売と誤認させる表示）",
    "kiyaku23_hazard": "同 第23条第1項（実際のものより優良・有利と誤認されるおそれのある表示）",
    "kisoku9_1": "同 施行規則 第9条第1号（取引態様は「売主」「貸主」「代理」「媒介（仲介）」の語で表示）",
    "kisoku9_9": "同 施行規則 第9条第9号（徒歩所要時間＝道路距離80mにつき1分・端数切上げ）",
    "kisoku9_16": "同 施行規則 第9条第16号（畳1枚あたり1.62㎡以上）",
    "kisoku9_21": "同 施行規則 第9条第21号（増築・改築・改装・改修は内容及び時期を明示）",
    "kisoku_hissu": "同 施行規則 別表（必要表示事項・新聞折込チラシ等）",
}

BLOCK = "block"      # 但し書きが無い禁止、または計算で確定する違反。直さないと出せない
CONFIRM = "confirm"  # 根拠があれば出せる。根拠を持っているか（＋場合により併記したか）を人が見る
NOTE = "note"        # 出せるが、確認しておいた方がよい

# 規約18条2項の但し書きは二段構え。ここを1段で実装すると判定を誤る。
#   HOLD   = 合理的な根拠資料を「現に有している」こと（広告面の外の事実＝機械では見えない）
#   INLINE = 上に加えて「根拠となる事実を併せて表示」すること（第1号・第2号のみ）
HOLD = "hold"
INLINE = "inline"

_LEVEL_ORDER = {BLOCK: 0, CONFIRM: 1, NOTE: 2}


@dataclass(frozen=True)
class Issue:
    level: str
    field: str
    term: str
    why: str
    source: str
    suggestion: str = ""
    evidence: str = ""   # HOLD / INLINE / ""（根拠の出し方。空＝根拠では正当化できない）
    listed: bool = True  # True=条文に列挙された語 / False=カテゴリ該当（判断を伴う）

    @property
    def source_text(self) -> str:
        return SOURCES.get(self.source, self.source)

    def as_dict(self) -> dict:
        return {"level": self.level, "field": self.field, "term": self.term,
                "why": self.why, "source": self.source_text,
                "suggestion": self.suggestion, "evidence": self.evidence,
                "listed": self.listed}


# ---------------------------------------------------------------- 正規化

def normalize(s) -> str:
    """全角/半角・大文字小文字・記号の揺れを畳んでから語を当てる。

    「ＮＯ．１」「No.1」「no．１」を同じ語として見るため。NFKC は全角英数を
    半角に、丸数字やローマ数字も畳む。区切り記号は落として `no1` の形に寄せる。
    """
    t = unicodedata.normalize("NFKC", str(s or ""))
    return t.lower()


def _squash(s: str) -> str:
    """記号・空白を落とした比較用の形（「N o . 1」「No-1」を同一視する）。"""
    return re.sub(r"[\s\.\-–—_/·・:：'\"”’`]+", "", normalize(s))


# ---------------------------------------------------------------- 語の辞書

@dataclass(frozen=True)
class TermRule:
    term: str
    level: str
    why: str
    source: str
    suggestion: str = ""
    # 正当な使い方（この語を含んでいても違反とみなさない複合語）。
    # 既定は「当てたら指摘」で、ここに書いた分だけを理由付きで緩める。
    allow_in: tuple = ()
    # 緩めた理由（テストで1件ずつ負例を持つ）
    allow_reason: str = ""
    evidence: str = ""    # HOLD=根拠資料の保有で足りる / INLINE=併記まで要る / ""=根拠で正当化できない
    listed: bool = True   # 条文に列挙された語か（False＝カテゴリ該当・判断を伴う）


def _t(term, level, why, source, suggestion="", allow_in=(), allow_reason="",
       evidence="", listed=True):
    return TermRule(term=term, level=level, why=why, source=source,
                    suggestion=suggestion, allow_in=tuple(allow_in),
                    allow_reason=allow_reason, evidence=evidence, listed=listed)


# ── 規約18条2項 第1号：最上級を意味する用語（根拠資料の保有＋根拠事実の**併記**が要る）
_SUPERLATIVE = [
    _t("最高級", CONFIRM, "最上級を意味する語です。根拠資料を持ち、根拠となる事実を広告に併記しないと使えません。",
       "kiyaku18_2_1", "比較の軸と時点を書く（例：当社取扱物件のうち最上位グレード・2026年時点）。",
       evidence=INLINE),
    _t("最高", CONFIRM, "最上級を意味する語です。根拠資料を持ち、根拠となる事実を広告に併記しないと使えません。",
       "kiyaku18_2_1", "「南向き」「駅から徒歩5分」のように測れる事実に置き換える。",
       allow_in=("最高高さ", "最高限度", "最高価格", "最高裁", "最高気温"),
       allow_reason="建築基準・法令用語としての「最高高さ／最高限度」、価格帯表示の「最高価格」は数値の名称。",
       evidence=INLINE),
    _t("極上", CONFIRM, "最上級を意味する語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_1", evidence=INLINE),
    _t("特級", CONFIRM, "最上級を意味する語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_1", evidence=INLINE),
    _t("最上級", CONFIRM, "最上級を意味する語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_1", evidence=INLINE),
    # 条文に列挙は無いがカテゴリ該当（最上級＋価格）
    _t("最安値", CONFIRM, "最上級かつ価格が著しく安いという印象を与える語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_1", "価格をそのまま書く。", evidence=INLINE, listed=False),
    _t("最安", CONFIRM, "最上級かつ価格が著しく安いという印象を与える語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_1", "価格をそのまま書く。", evidence=INLINE, listed=False),
]

# ── 第2号：著しく安いという印象を与える用語（根拠資料の保有＋根拠事実の**併記**）
_PRICE = [
    _t(t, CONFIRM, "価格が著しく安いという印象を与える語です。根拠資料を持ち、根拠となる事実を広告に併記しないと使えません。",
       "kiyaku18_2_2", "近隣相場との比較を出典と時点付きで書けないなら、価格をそのまま書く。",
       evidence=INLINE)
    for t in ("買得", "掘出", "土地値", "格安", "投売り", "破格", "特安", "激安",
              "バーゲンセール", "安値")
] + [
    _t("バーゲン", CONFIRM, "価格が著しく安いという印象を与える語です。根拠資料と根拠事実の併記が要ります。",
       "kiyaku18_2_2", evidence=INLINE, listed=False),
    _t("堀出", CONFIRM, "「掘出」の異表記です。価格が著しく安いという印象を与えます。",
       "kiyaku18_2_2", evidence=INLINE, listed=False),
]

# ── 第3号：全く欠けるところがないことを意味する用語（根拠資料の**保有**で足りる）
_ABSOLUTE = [
    _t("完全", CONFIRM, "「全く欠けるところがない」ことを意味する語です。裏付ける根拠資料を持っていない限り使えません。",
       "kiyaku18_2_3", "対象を限定して書く（例：住戸間の界壁あり）。",
       allow_in=("完全分離", "完全個室", "完全予約制", "完全週休"),
       allow_reason="間取り・営業条件の事実を指す複合語で、物件の形質の完全性の主張ではない。",
       evidence=HOLD),
    _t("完ぺき", CONFIRM, "「全く欠けるところがない」ことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_3", evidence=HOLD),
    _t("完璧", CONFIRM, "「完ぺき」の異表記です。「全く欠けるところがない」ことを意味します。",
       "kiyaku18_2_3", evidence=HOLD, listed=False),
    _t("絶対", CONFIRM, "「全く欠けるところがない」ことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_3", "断定を外して事実だけ書く。",
       allow_in=("絶対高さ",),
       allow_reason="「絶対高さ制限」は建築基準法上の制限の名称で、物件の形質の主張ではない。",
       evidence=HOLD),
    _t("万全", CONFIRM, "「全く手落ちがない」ことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_3", evidence=HOLD),
]

# ── 第4号：競争事業者よりも優位に立つことを意味する用語（根拠資料の**保有**）
_SUPERIORITY = [
    _t("日本一", CONFIRM, "他社より優位に立つことを意味する語です。裏付ける根拠資料が要ります。",
       "kiyaku18_2_4", "調査主体・対象範囲・時点を書けないなら外す。", evidence=HOLD),
    _t("日本初", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD),
    _t("業界一", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD),
    _t("当社だけ", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD),
    _t("他に類を見ない", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD),
    _t("抜群", CONFIRM, "他より著しく優れていることを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", "「南向き」「駅から徒歩5分」のように、測れる事実に置き換える。",
       evidence=HOLD),
    # 「超」は条文に明記。生成文で「超駅近」「超広々」の形で頻出するので必ず見る。
    _t("超", CONFIRM, "他より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", "程度を表す「超」を外し、数値で書く。",
       allow_in=("超高層", "超音波", "超過", "超える", "超えた", "超短期", "超低金利",
                 "超高速", "超々"),
       allow_reason="「超高層」等は建築・設備の分類名で、優位性の主張ではない。",
       evidence=HOLD),
    # 条文に列挙は無いがカテゴリ該当
    _t("no1", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", "調査主体・対象範囲・時点を書けないなら外す。",
       evidence=HOLD, listed=False),
    _t("ナンバーワン", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD, listed=False),
    _t("業界初", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD, listed=False),
    _t("唯一", CONFIRM, "他社より優位に立つことを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", evidence=HOLD, listed=False),
    _t("一級", CONFIRM, "他より著しく優れていることを意味する語です。根拠資料が要ります。",
       "kiyaku18_2_4", allow_in=("一級建築士", "一級河川"),
       allow_reason="資格名・河川等級は事実の名称であって優位性の主張ではない。",
       evidence=HOLD, listed=False),
]

# ── 第5号：一定の基準により選別されたことを意味する用語（根拠資料の**保有**）
_SELECTED = [
    _t("特選", CONFIRM, "一定の基準で選別したことを意味する語です。選別基準の根拠資料が要ります。",
       "kiyaku18_2_5", "選別基準を書けないなら外す。", evidence=HOLD),
    _t("厳選", CONFIRM, "一定の基準で選別したことを意味する語です。選別基準の根拠資料が要ります。",
       "kiyaku18_2_5", "選別基準を書けないなら外す。", evidence=HOLD),
]

# ── 第6号：著しく人気が高いという印象を与える用語（根拠資料の**保有**）
_POPULARITY = [
    _t("完売", CONFIRM, "著しく人気が高いという印象を与える語です。実際に完売した事実の根拠が要ります。",
       "kiyaku18_2_6", "完売していないのに使うと、規約23条1項70号の不当表示にもあたります。",
       evidence=HOLD),
    _t("即完", CONFIRM, "著しく人気が高いという印象を与える語です。事実の根拠が要ります。",
       "kiyaku18_2_6", evidence=HOLD, listed=False),
    _t("残りわずか", CONFIRM, "著しく人気が高いという印象を与える語です。残戸数の事実が要ります。",
       "kiyaku18_2_6", "残戸数を数字で書く。", evidence=HOLD, listed=False),
]

# ── 規約23条1項：**但し書きが無い**＝根拠を示しても使えない
_MISLEADING = [
    _t("新築同様", BLOCK, "増改築した建物を新築と誤認させる表示は、根拠があっても使えません。",
       "kiyaku23_21", "改装の内容と時期をそのまま書く（例：2024年6月に内装全面改装）。",
       evidence=""),
    _t("新築そっくり", BLOCK, "増改築した建物を新築と誤認させる表示は、根拠があっても使えません。",
       "kiyaku23_21", "改装の内容と時期をそのまま書く。", evidence=""),
    _t("新築並み", BLOCK, "増改築した建物を新築と誤認させる表示は、根拠があっても使えません。",
       "kiyaku23_21", "改装の内容と時期をそのまま書く。", evidence="", listed=False),
]

# ── 宅建業法32条：将来を保証する表現（誇大広告。違反は罰則対象）
_GUARANTEE = [
    _t("必ず儲かる", BLOCK, "将来の収益を保証する表現は誇大広告にあたります。", "gyouhou32"),
    _t("値上がり確実", BLOCK, "将来の価格を保証する表現は誇大広告にあたります。", "gyouhou32"),
    _t("損はしません", BLOCK, "将来の収益を保証する表現は誇大広告にあたります。", "gyouhou32"),
    _t("利回り保証", CONFIRM, "保証の主体・条件・期間を示せない限り使えません。", "gyouhou32",
       evidence=HOLD),
    _t("空室保証", CONFIRM, "保証の主体・条件・期間を示せない限り使えません。", "gyouhou32",
       evidence=HOLD),
]

TERM_RULES: tuple = tuple(_SUPERLATIVE + _PRICE + _ABSOLUTE + _SUPERIORITY
                          + _SELECTED + _POPULARITY + _MISLEADING + _GUARANTEE)

# 広告文が入る欄（ここを検査する）。数値欄・会社欄は別の規則で見る。
TEXT_FIELDS: tuple = (
    "title_copy", "sales_points", "lead", "bikou", "setsubi",
    "surroundings", "staging_note", "genkyo", "access", "property_name",
)

FIELD_LABEL: dict[str, str] = {
    "title_copy": "キャッチコピー", "sales_points": "セールスポイント",
    "lead": "リード文", "bikou": "備考", "setsubi": "設備状況",
    "surroundings": "周辺環境", "staging_note": "画像注記",
    "genkyo": "現況", "access": "交通", "property_name": "物件名",
    "built": "築年月", "torihiki_taiyo": "取引態様",
}


# ---------------------------------------------------------------- 語の検査

def check_text(text, field: str = "") -> list:
    """1つの文について、制限語の該当を返す。

    当て方は正規化後の部分一致。日本語は語境界が無いので `\\b` は使えない
    （「厳選」「no.1」はいずれも語中に現れる）。誤爆は allow_in で理由付きに
    緩め、その1件ごとに負例テストを置く。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    n = normalize(raw)
    sq = _squash(raw)
    out = []
    for rule in TERM_RULES:
        term_n = normalize(rule.term)
        term_sq = _squash(rule.term)
        # 「no1」のように記号を挟んで書かれる語は squash 側で当てる
        hit = (term_n in n) or (len(term_sq) >= 3 and term_sq in sq)
        if not hit:
            continue
        if any(normalize(a) in n for a in rule.allow_in):
            continue
        out.append(Issue(level=rule.level, field=field, term=rule.term,
                         why=rule.why, source=rule.source,
                         suggestion=rule.suggestion, evidence=rule.evidence,
                         listed=rule.listed))
    return out


# ---------------------------------------------------------------- 数値の規則

_WALK_M_PER_MIN = 80  # 道路距離80mにつき1分・端数切上げ（施行規則の表示基準）

_WALK_RE = re.compile(r"徒歩\s*(?:約)?\s*(\d+)\s*分")
_DIST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|ｍ|メートル)")


def walk_minutes_for(distance_m) -> int:
    """道路距離から表示してよい徒歩分数（80mにつき1分・端数は切り上げ）。"""
    d = float(distance_m)
    if d <= 0:
        raise ValueError("距離は正の数で指定してください")
    return max(1, math.ceil(d / _WALK_M_PER_MIN))


def check_walk(text, distance_m=None, field: str = "access") -> list:
    """「徒歩○分」の表記を検査する。

    距離が分かるなら 80m/分・切上げ で突き合わせる。距離が分からないときは
    「問題なし」ではなく「確認」に倒す（判定不能を安全側へ）。
    """
    raw = str(text or "")
    m = _WALK_RE.search(normalize(raw))
    if not m:
        return []
    shown = int(m.group(1))
    dist = distance_m
    if dist in (None, "", 0):
        md = _DIST_RE.search(normalize(raw))
        dist = float(md.group(1)) if md else None
    if dist in (None, "", 0):
        return [Issue(NOTE, field, f"徒歩{shown}分",
                      "道路距離が分からないため、この分数が規則どおりか確かめられません。",
                      "kisoku_hyouji",
                      "道路距離80mにつき1分（端数は切り上げ）で数えた値か確認してください。")]
    try:
        need = walk_minutes_for(dist)
    except (TypeError, ValueError):
        return [Issue(CONFIRM, field, f"徒歩{shown}分",
                      "道路距離を数値として読み取れませんでした。", "kisoku_hyouji")]
    if shown < need:
        return [Issue(BLOCK, field, f"徒歩{shown}分",
                      f"道路距離{int(float(dist))}mなら徒歩{need}分と表示します"
                      f"（80mにつき1分・端数は切り上げ）。",
                      "kisoku_hyouji", f"徒歩{need}分に直す。")]
    return []


_TATAMI_MIN_SQM = 1.62  # 畳1枚あたりの下限（表示基準）
_JO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:畳|帖)")


def check_tatami(text, sqm=None, field: str = "") -> list:
    """畳数表示を検査する。畳1枚 1.62㎡ 以上として書く必要がある。"""
    raw = normalize(text or "")
    m = _JO_RE.search(raw)
    if not m or sqm in (None, "", 0):
        return []
    try:
        jo = float(m.group(1))
        area = float(sqm)
    except (TypeError, ValueError):
        return []
    if jo <= 0 or area <= 0:
        return []
    per = area / jo
    if per < _TATAMI_MIN_SQM:
        return [Issue(BLOCK, field, f"{m.group(1)}畳",
                      f"畳1枚あたり{per:.2f}㎡になります。"
                      f"{_TATAMI_MIN_SQM}㎡以上として表示してください。",
                      "kisoku_hyouji",
                      f"{math.floor(area / _TATAMI_MIN_SQM * 10) / 10}畳以下に直す。")]
    return []


_YEAR_RE = re.compile(r"(\d{4})\s*年")


def check_shinchiku(text, built="", occupied=None, today=None, field: str = "") -> list:
    """「新築」の使い方を検査する。

    規約の定義は「建築後1年未満であって、居住の用に供されたことがないもの」。
    どちらか一方でも外れたら新築とは書けない。判定材料が無いときは確認に倒す。
    """
    if "新築" not in normalize(text or ""):
        return []
    src = "kiyaku_tokutei"
    if occupied is True:
        return [Issue(BLOCK, field, "新築",
                      "入居したことがある建物は「新築」と書けません"
                      "（建築後1年未満かつ未入居のものだけ）。",
                      src, "「築浅」等、事実に合う語に直す。")]
    y = _YEAR_RE.search(normalize(built or ""))
    if not y or not today:
        return [Issue(CONFIRM, field, "新築",
                      "建築後1年未満かつ未入居であることを確かめてください。", src)]
    try:
        built_year = int(y.group(1))
        this_year = int(today[:4]) if isinstance(today, str) else int(today.year)
    except (TypeError, ValueError, IndexError):
        return [Issue(CONFIRM, field, "新築",
                      "築年月を読み取れませんでした。1年未満か確かめてください。", src)]
    if this_year - built_year >= 2:
        return [Issue(BLOCK, field, "新築",
                      f"{built_year}年築は建築後1年以上です。「新築」とは書けません。",
                      src, "「築浅」等、事実に合う語に直す。")]
    if this_year - built_year == 1:
        return [Issue(CONFIRM, field, "新築",
                      "築年が1年前です。建築の月日まで見て1年未満か確かめてください。", src)]
    if occupied is None:
        return [Issue(CONFIRM, field, "新築", "未入居であることを確かめてください。", src)]
    return []


# 現況が成約済みのまま広告に出し続けると、おとり広告になる。
_SOLD_WORDS = ("成約", "契約済", "申込済", "商談中", "止め", "満室止")


def check_otori(fields: dict) -> list:
    g = normalize((fields or {}).get("genkyo") or "")
    for w in _SOLD_WORDS:
        if normalize(w) in g:
            return [Issue(BLOCK, "genkyo", w,
                          "取引できない物件を広告に出し続けることはできません。",
                          "otori", "掲載を取り下げるか、現況を実際の状態に直す。")]
    return []


# 取引態様はこの語を使って表示する（施行規則9条1号）。「仲介」は「媒介」の言い換えとして可。
TAIYO_WORDS = ("売主", "貸主", "代理", "媒介", "仲介")
# 媒介契約の種類であって取引態様そのものではない語。付け足すと別の意味に読まれうる。
_TAIYO_EXTRA = ("専任", "専属専任", "一般媒介", "一般", "取扱")


def check_torihiki_taiyo(fields: dict) -> list:
    """取引態様の明示（宅建業法34条1項）と、その表記（施行規則9条1号）。

    広告のたびに必要で、空欄では出せない。さらに「売主／貸主／代理／媒介（仲介）」の
    語を使って表す決まりなので、独自の言い回しは通さない。
    """
    v = str((fields or {}).get("torihiki_taiyo") or "").strip()
    if not v or v in ("（要入力）", "-", "—"):
        return [Issue(BLOCK, "torihiki_taiyo", "取引態様",
                      "取引態様は広告のたびに明示する必要があります。",
                      "gyouhou34", "「売主」「貸主」「代理」「媒介（仲介）」から選ぶ。")]
    n = normalize(v)
    if not any(normalize(w) in n for w in TAIYO_WORDS):
        return [Issue(BLOCK, "torihiki_taiyo", v,
                      "取引態様は「売主」「貸主」「代理」「媒介（仲介）」の語を使って表示します。",
                      "kisoku9_1", "この4つのいずれかに書き直す。")]
    extra = [w for w in _TAIYO_EXTRA if normalize(w) in n]
    if extra:
        return [Issue(NOTE, "torihiki_taiyo", v,
                      f"「{extra[0]}」は媒介契約の種類であって取引態様ではありません。",
                      "kisoku9_1",
                      "広告の取引態様欄は「媒介」（または「仲介」）だけにする。")]
    return []


_REFORM_WORDS = ("リフォーム済", "改装済", "改修済", "増築", "改築", "リノベーション済")


def check_reform(fields: dict) -> list:
    """増築・改築・改装・改修を書くなら、その内容と時期を明示する（施行規則9条21号）。

    「リフォーム済」とだけ書いて中身も時期も無い販売図面は珍しくないが、規則は
    2点セットを求めている。時期は年が書いてあるかで見る。
    """
    joined = " ".join(str((fields or {}).get(k) or "") for k in TEXT_FIELDS)
    n = normalize(joined)
    hit = next((w for w in _REFORM_WORDS if normalize(w) in n), "")
    if not hit:
        return []
    has_when = bool(re.search(r"(19|20)\d{2}\s*年|令和\s*\d+\s*年|平成\s*\d+\s*年", n))
    if has_when:
        return []
    return [Issue(CONFIRM, "bikou", hit,
                  "改装・改修を書くときは、その内容と時期の両方を示す決まりです。",
                  "kisoku9_21", "「2024年6月に内装全面改装」のように、いつ何をしたかを書く。")]


# ---------------------------------------------------------------- まとめ

def review(fields: dict, *, distance_m=None, occupied=None, today=None) -> list:
    """マイソク1枚ぶんの広告表現を検査して、指摘を重い順に返す。

    空リストなら、この検査の範囲では出せる。**「合法である」ことの保証ではない**
    ことに注意する（この検査が見るのは語彙・数値規則・必須項目の範囲だけ）。
    """
    f = dict(fields or {})
    issues: list = []
    for key in TEXT_FIELDS:
        issues += check_text(f.get(key), field=key)
    issues += check_walk(f.get("access"), distance_m, field="access")
    issues += check_tatami(f.get("floor_plan"), f.get("building_area_sqm"),
                           field="floor_plan")
    for key in ("title_copy", "lead", "property_name", "sales_points"):
        issues += check_shinchiku(f.get(key), f.get("built"), occupied, today, field=key)
    issues += check_otori(f)
    issues += check_torihiki_taiyo(f)
    issues += check_reform(f)
    return dedupe(issues)


def dedupe(issues) -> list:
    """同じ欄の重複を畳んで重い順に並べる。

    「最高級」が当たった欄で「最高」も当てると同じ指摘が2行出るので、
    より長い語に含まれる短い語の指摘は落とす（指摘の水増しを防ぐ）。
    """
    items = list(issues or [])
    by_field: dict = {}
    for i in items:
        by_field.setdefault(i.field, set()).add(i.term)
    seen, uniq = set(), []
    for i in items:
        others = by_field.get(i.field, set())
        if any(o != i.term and i.term in o for o in others):
            continue
        k = (i.field, i.term, i.level)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(i)
    uniq.sort(key=lambda i: (_LEVEL_ORDER.get(i.level, 9), i.field))
    return uniq


def blocking(issues) -> list:
    return [i for i in issues or [] if i.level == BLOCK]


def summary(issues) -> str:
    """画面に出す1行。指摘が無いときも「見た」ことが分かる文にする。"""
    if not issues:
        return "広告の表示規則について、この検査で見つかった問題はありません。"
    b = len(blocking(issues))
    c = len([i for i in issues if i.level == CONFIRM])
    n = len(issues) - b - c
    parts = []
    if b:
        parts.append(f"直さないと出せないもの {b} 件")
    if c:
        parts.append(f"根拠が要るもの {c} 件")
    if n:
        parts.append(f"確認したいもの {n} 件")
    return "、".join(parts) + "。"
