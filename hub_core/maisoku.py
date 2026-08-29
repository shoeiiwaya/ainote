"""マイソク(販売図面)= テンプレ駆動レンダラ。

確定様式は ri-maisoku の6 variant を `hub_core/templates/maisoku/<variant>.html` に取り込み(自己完結)。
既定=dense-pro(公正競争規約の必要表示事項対応)。自由文でなく**様式に値を差し込む**。
差し込む値は必ず HTML エスケープ(物件/会社の自由文経由の注入を防ぐ)。stdlib・外部0。
ブラウザ印刷で HTML→PDF(ローカル完結)。
"""
from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

_TPL_DIR = Path(__file__).parent / "templates" / "maisoku"
# shuueki=収益物件(A4横・利回り/満室想定/積算)。standard=実需の販売図面。他はデザイン版。
VARIANTS = ["survey", "shuueki", "standard", "dense-pro", "clean-ui", "editorial", "v2-photo", "v2-sumi", "v2-tate"]
DEFAULT_VARIANT = "shuueki"
XLSX_VARIANTS = ("A", "C", "B")
XLSX_VARIANT_LABELS = {
    "A": "A 標準A4横（通常出力）",
    "C": "C 業者間FAX白黒",
    "B": "B 買主向けA4縦",
}
VARIANT_LABELS = {
    "survey": "調査資料重視",
    "shuueki": "収益物件（横）",
    "standard": "標準販売図面",
    "dense-pro": "情報量重視",
    "clean-ui": "余白重視",
    "editorial": "写真重視",
    "v2-photo": "写真中心",
    "v2-sumi": "墨一色",
    "v2-tate": "縦組み・墨一色",
}

# 編集フォームの項目定義: (key, ラベル, グループ, 複数行か)。
# 販売図面(実需=standard / 収益=shuueki)の共通項目。
MAISOKU_FIELDS = [
    # 物件基本
    ("property_type", "物件種目", "物件基本", False),
    ("property_name", "物件名", "物件基本", False),
    ("owner_change", "オーナーチェンジ等の区分", "物件基本", False),
    ("title_copy", "キャッチコピー(見出し)", "物件基本", False),
    ("sales_points", "セールスポイント", "物件基本", True),
    ("price", "価格", "物件基本", False),
    ("price_label", "価格ラベル", "物件基本", False),
    ("yield_rate", "利回り(現状/想定)", "物件基本", False),
    ("torihiki_taiyo", "取引態様/形態", "物件基本", False),
    # 立地
    ("address", "所在地", "立地", False),
    ("nearest_station", "最寄駅", "立地", False),
    ("route_lines", "沿線(複数路線)", "立地", True),
    ("access", "交通", "立地", False),
    ("surroundings", "周辺環境", "立地", True),
    # 物件概要
    ("land_area", "土地/敷地面積", "物件概要", False),
    ("building_area", "建物/専有/延床面積", "物件概要", False),
    ("balcony_area", "バルコニー面積", "物件概要", False),
    ("floor_plan", "間取り", "物件概要", False),
    ("floors_total", "階建/所在階", "物件概要", False),
    ("built", "築年月", "物件概要", False),
    ("chikunensu", "築年数", "物件概要", False),
    ("structure", "構造", "物件概要", False),
    ("total_units", "総戸数", "物件概要", False),
    ("direction", "方位(採光面)", "物件概要", False),
    ("genkyo", "現況", "物件概要", False),
    ("hikiwatashi", "引渡時期", "物件概要", False),
    # 法令・権利
    ("land_right", "土地権利", "法令・権利", False),
    ("chimoku", "地目/種類", "法令・権利", False),
    ("toshi_keikaku", "都市計画", "法令・権利", False),
    ("youto", "用途地域", "法令・権利", False),
    ("kenpei_yoseki", "建ぺい率／容積率", "法令・権利", False),
    ("bouka", "防火規制", "法令・権利", False),
    ("other_law", "他の法令上の制限", "法令・権利", False),
    ("road", "接道状況", "法令・権利", False),
    ("shidou", "私道負担", "法令・権利", False),
    # 維持費・収益(投資指標)
    ("management_fee", "管理費(月)", "維持費・収益", False),
    ("repair_fund", "修繕積立金(月)", "維持費・収益", False),
    ("other_fee", "その他費用", "維持費・収益", False),
    ("parking", "駐車場/駐輪場", "維持費・収益", False),
    ("monthly_rent", "月額賃料/満室想定(月)", "維持費・収益", False),
    ("annual_income", "年間収入/満室想定(年)", "維持費・収益", False),
    ("income_basis_label", "賃料・収入の基準", "維持費・収益", False),
    ("lease_period", "賃貸借期間", "維持費・収益", False),
    # 関係会社
    ("bunjou_company", "分譲会社", "関係会社", False),
    ("kanri_company", "管理会社", "関係会社", False),
    ("sekou_company", "施工", "関係会社", False),
    # 設備・備考
    ("setsubi", "設備状況", "設備・備考", True),
    ("bikou", "備考(路線価/積算等)", "設備・備考", True),
    # 業者間流通(出典: fudosan-ontology)。広告不可の無断掲載はレインズ規程・宅建業法違反。
    ("reins_no", "レインズ登録番号", "流通・広告(物確で確認)", False),
    ("ad_permission", "広告可否(広告転載区分)", "流通・広告(物確で確認)", False),
    ("obi_swap", "帯替え可否", "流通・広告(物確で確認)", False),
    # 帯=マイソク下部の取扱業者欄。客付は自社へ帯替えして使う(元付の承諾事項)。
    ("company_name", "会社名", "帯(取扱業者欄・必要表示事項)", False),
    ("license", "免許番号", "帯(取扱業者欄・必要表示事項)", False),
    ("association", "保証協会", "帯(取扱業者欄・必要表示事項)", False),
    ("fair_trade_association", "公正取引協議会", "帯(取扱業者欄・必要表示事項)", False),
    ("company_address", "会社所在地", "帯(取扱業者欄・必要表示事項)", False),
    ("company_tel", "TEL", "帯(取扱業者欄・必要表示事項)", False),
    ("company_fax", "FAX", "帯(取扱業者欄・必要表示事項)", False),
    ("company_email", "Email", "帯(取扱業者欄・必要表示事項)", False),
    ("staff", "担当", "帯(取扱業者欄・必要表示事項)", False),
    ("fee", "手数料", "帯(取扱業者欄・必要表示事項)", False),
    ("key_handling", "鍵", "帯(取扱業者欄・必要表示事項)", False),
    ("holiday", "定休日", "帯(取扱業者欄・必要表示事項)", False),
    # 掲載管理
    ("yuko_kigen", "取引条件の有効期限", "掲載管理", False),
    ("published", "情報公開日", "掲載管理", False),
    ("next_update", "次回更新予定日", "掲載管理", False),
    ("staging_note", "画像注記", "掲載管理", True),
    ("price_note", "価格の注記（税込等・確認済みの場合のみ）", "掲載管理", True),
    ("lead", "リード文(デザイン版用・任意)", "広告コピー(任意)", True),
    ("photo_main_caption", "メイン写真の説明", "写真・図面", False),
    ("photo_main_tag", "メイン写真の種別", "写真・図面", False),
    ("photo_sub1_caption", "写真1の説明", "写真・図面", False),
    ("photo_sub1_tag", "写真1の種別", "写真・図面", False),
    ("photo_sub2_caption", "写真2の説明", "写真・図面", False),
    ("photo_sub2_tag", "写真2の種別", "写真・図面", False),
    ("photo_sub3_caption", "写真3の説明", "写真・図面", False),
    ("photo_sub3_tag", "写真3の種別", "写真・図面", False),
    ("photo_floorplan_caption", "間取り図の説明", "写真・図面", False),
    ("photo_map_caption", "地図の説明", "写真・図面", False),
    ("photo_map_tag", "地図の種別", "写真・図面", False),
    ("special_notes", "広告の特記事項", "法令・権利", True),
]
FIELD_KEYS = [f[0] for f in MAISOKU_FIELDS]
PUBLISH_EVIDENCE_FIELDS = [
    ("walk_distance_m", "徒歩経路の道路距離(m・分数確認用)", "公開前の根拠", False),
]
PUBLISH_EVIDENCE_KEYS = [f[0] for f in PUBLISH_EVIDENCE_FIELDS]
EDITABLE_FIELD_KEYS = [*FIELD_KEYS, *PUBLISH_EVIDENCE_KEYS]
# 欄名→人が読むラベル（画面で「何が足りないか」を日本語で言うため）
MAISOKU_LABELS = {f[0]: f[1] for f in (*MAISOKU_FIELDS, *PUBLISH_EVIDENCE_FIELDS)}


def field_groups():
    """[(group, [(key,label,multiline), ...]), ...] の順序付き。"""
    groups = []
    for key, label, grp, ml in (*MAISOKU_FIELDS, *PUBLISH_EVIDENCE_FIELDS):
        if not groups or groups[-1][0] != grp:
            groups.append((grp, []))
        groups[-1][1].append((key, label, ml))
    return groups


# 業者プロフィール(company.json)→ マイソクの帯(取扱業者欄=必要表示事項)へのマッピング。
# 会社情報を設定画面で一度入れれば全書類の帯が自動で埋まる（他社原紙の流用を不要にする）。
COMPANY_TO_OBI = {
    "name": "company_name", "license_no": "license", "association": "association",
    "fair_trade": "fair_trade_association", "address": "company_address",
    "tel": "company_tel", "fax": "company_fax", "email": "company_email",
    "staff": "staff", "holiday": "holiday",
}


# 重説(juusetsu_draft)のフィールド → マイソクのフィールド への写像（1回入力で両方作る）。
_JU_TO_MS = {
    "property_name": "property_name", "address": "address", "structure": "structure",
    "area": "building_area", "layout": "floor_plan", "built": "built",
    "youto": "youto", "kenpei_yoseki": "kenpei_yoseki", "rent": "monthly_rent",
    "deal_type": "property_type", "torihiki_keitai": "torihiki_taiyo",
    "company_name": "company_name", "license_no": "license", "association": "association",
    "company_address": "company_address", "company_tel": "company_tel", "takkenshi_name": "staff",
}


def from_property_fields(f: dict) -> dict:
    """重説の物件フィールド辞書 → マイソクのフィールド辞書（共有項目を写す）。空値は入れない。"""
    out = {}
    for jk, mk in _JU_TO_MS.items():
        v = str((f or {}).get(jk) or "").strip()
        if v:
            out.setdefault(mk, v)
    # 価格ラベル: 賃貸なら月額賃料を price に
    if out.get("monthly_rent") and not out.get("price"):
        out["price"] = out["monthly_rent"]
        out["price_label"] = "賃料（月額）"
    return out


def company_to_obi(company: dict) -> dict:
    """業者プロフィール dict → 帯フィールド dict（空値は入れない）。"""
    out = {}
    for ck, fk in COMPANY_TO_OBI.items():
        v = str((company or {}).get(ck) or "").strip()
        if v:
            out[fk] = v
    return out


def fields_with_company(fields: dict, company: dict, *, overwrite: bool = False) -> dict:
    """マイソクfieldsに業者プロフィール由来の帯を差し込む。

    ``overwrite=True`` は顧客向け表示・出力用。保存本文の帯を信用せず、版に束縛された
    会社スナップショットだけを正本にする。スナップショットで空の項目も本文から消す。
    """
    obi = company_to_obi(company)
    out = dict(fields)
    if overwrite:
        for key in COMPANY_TO_OBI.values():
            out[key] = ""
    for k, v in obi.items():
        if overwrite or not str(out.get(k) or "").strip():
            out[k] = v
    return out


def default_fields() -> dict:
    return {k: "" for k in EDITABLE_FIELD_KEYS}


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


# ブランドテーマ: 業者のアクセント色1系統で「自社のもの」に見せる（AI臭のcream/terracotta/明朝は既定に置かない）。
DISPLAY_FONTS = {
    "gothic": '"Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic","Noto Sans JP",sans-serif',
    "condensed": '"Roboto Condensed","Yu Gothic","Hiragino Sans","Noto Sans JP",sans-serif',
    "rounded": '"Hiragino Maru Gothic ProN","Rounded Mplus 1c","Noto Sans JP",sans-serif',
}
# 会社の色を**使わない**様式は、理由を書いて宣言する。宣言の無い「色が効かない様式」は
# テストが落とす（2026-08-07 監査 1-13＝9様式中8つで色が効かなかったことへの是正）。
MONOCHROME_VARIANTS = {
    "v2-sumi": "墨一色の意匠。濃淡と余白だけで作る様式なので、有彩色を入れると設計が崩れる。",
    "v2-tate": "縦組み・墨一色の意匠。罫と字送りで見せる様式なので、有彩色を入れない。",
}

DEFAULT_ACCENT = "#b3261e"   # 朱（既定）。cream+terracotta+serifのAIクラスタを既定にしない。


def _accent_ink(hexcolor: str) -> str:
    """アクセント色上の文字色を相対輝度から自動決定（濃紺でも朱でも黄でも破綻しない）。"""
    h = (hexcolor or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#ffffff"
    def _lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    return "#16191c" if lum > 0.5 else "#ffffff"


def _valid_hex(c: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", (c or "").strip()))


# 公正競争規約の必要表示事項（公開前 fail-closed チェックの対象）。
# 必要表示事項（施行規則 別表・新聞折込チラシ等の欄）。マイソク＝「その他の方法により
# 配布されるチラシ」に当たるので、この列に○が付く事項を満たす必要がある。
#   ここに入れているのは**媒体・物件種別によらず必須**のもの。
#   「礼金・敷金・保証会社・損害保険料」等は「必要とするときはその旨」＝条件付きなので
#   一律必須にはできない。「定期建物賃貸借であるときはその旨」も同様。
#   これらは物件種別ごとの判定が要るため、ここではなく画面側の案内で扱う。
REQUIRED_FOR_PUBLISH = ("company_name", "license", "association", "fair_trade_association",
                        "company_address", "company_tel", "price", "land_area",
                        "building_area", "built",
                        "torihiki_taiyo", "address", "nearest_station")
_MISSING_PLACEHOLDERS = ("（要入力）", "（免許番号を登録）", "（/setup で会社情報を登録）")
_DASH_TRANSLATION = str.maketrans({c: "-" for c in "‐‑‒–—―−ー－"})
_EMPTY_REQUIRED_VALUES = {
    "", "-", "?", "??", "???", "n/a", "na", "none", "null", "tbd", "tba",
    "未定", "未確認", "未入力", "未記入", "不明", "調査中", "確認中", "要確認",
    "別途", "後日", "応相談", "要相談", "お問い合わせ", "問い合わせ", "非公開",
}
_PLACEHOLDER_RUNS = (
    "○○", "〇〇", "◯◯", "××", "△△", "□□", "●●", "＊＊", "**", "##",
    "__", "...", "‥", "・・", "〓", "??", "？？",
)
_QUANTITY_REQUIRED = {"price", "land_area", "building_area", "built"}
_FIELD_ECHO_VALUES = {
    "company_name": {"会社", "会社名", "不動産会社", "株式会社"},
    "company_address": {"住所", "所在地", "会社住所", "会社所在地"},
    "price": {"価格", "販売価格", "賃料", "金額"},
    "land_area": {"面積", "土地面積", "敷地面積"},
    "building_area": {"面積", "建物面積", "専有面積"},
    "built": {"築年月", "建築年月", "完成年月"},
    "address": {"住所", "所在地", "物件所在地"},
    "nearest_station": {"駅", "最寄駅", "最寄り駅", "交通"},
}


def _required_norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", text)
    return text.strip().translate(_DASH_TRANSLATION).casefold()


def _has_word_or_number(value: str) -> bool:
    """句読点・装飾記号だけで必須欄を埋める迂回を拒否する。"""
    return any(unicodedata.category(ch)[0] in ("L", "N") for ch in value)


def _positive_number(value: str) -> bool:
    numbers = re.findall(r"\d+(?:[.,]\d+)?", value.replace(",", ""))
    return bool(numbers) and any(float(number) > 0 for number in numbers)


def _looks_like_address(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return len(compact) >= 5 and bool(re.search(r"[都道府県市区町村郡]", compact))


def _looks_like_built_date(value: str) -> bool:
    return bool(
        re.search(r"(?:明治|大正|昭和|平成|令和)?\d{1,4}年(?:\d{1,2}月)?", value)
        or re.search(r"(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])(?:[-/.]\d{1,2})?", value)
    )


def _required_value_is_meaningful(key: str, value: object) -> bool:
    """公開必須値の意味と最低限の型を項目別に検証する。

    自由文を辞書で「正しい」と断定はしない。一方、空相当・句読点だけ・未確定語と、
    金額/面積/築年月/電話/免許/団体名/取引態様の型違反は fail-closed にする。
    """
    raw = str(value or "").strip()
    norm = _required_norm(raw)
    if norm in _EMPTY_REQUIRED_VALUES or not _has_word_or_number(norm):
        return False
    if any(marker in norm for marker in _PLACEHOLDER_RUNS):
        return False
    if norm in _FIELD_ECHO_VALUES.get(key, set()):
        return False
    if any(norm.rstrip("。.!！?？ ・:").endswith(word)
           for word in ("未定", "未確認", "未入力", "不明", "調査中", "確認中", "要確認",
                        "別途", "後日", "応相談", "要相談", "非公開")):
        return False
    if key == "price":
        return _positive_number(norm) and "円" in norm
    if key in ("land_area", "building_area"):
        if key == "land_area" and "敷地権" in raw:
            return bool(re.search(r"\d+\s*/\s*\d+", norm))
        return _positive_number(norm) and bool(re.search(r"(?:m2|平方メートル)", norm))
    if key == "built":
        return _looks_like_built_date(norm)
    if key == "company_tel":
        digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", raw))
        return 9 <= len(digits) <= 11 and digits.startswith("0")
    if key == "license":
        number = re.search(r"第\s*0*(\d+)\s*号", norm)
        return bool(number and int(number.group(1)) > 0
                    and any(authority in raw for authority in ("知事", "大臣")))
    if key == "association":
        return "保証" in raw or "協会" in raw
    if key == "fair_trade_association":
        return "公取" in raw or "公正取引" in raw
    if key == "torihiki_taiyo":
        return any(term in raw for term in ("売主", "貸主", "代理", "媒介", "仲介"))
    if key in ("company_address", "address"):
        return _looks_like_address(norm)
    if key == "nearest_station":
        return len(norm) >= 2 and bool(re.search(r"(?:駅|停留所|バス停)", norm))
    return True


class ComplianceError(Exception):
    """必要表示事項の欠落（公開前 fail-closed）。"""
    code = 409

    def __init__(self, missing: list):
        self.missing = missing
        labels = [MAISOKU_LABELS.get(key, key) for key in missing]
        super().__init__("必要表示事項が未入力または未表示です: " + "、".join(labels))


class AdComplianceError(Exception):
    """広告表現の機械確認で指摘が残っている（公開前 fail-closed）。"""
    code = 409

    def __init__(self, issues: list):
        self.issues = list(issues or [])
        labels = []
        for issue in self.issues:
            field = MAISOKU_LABELS.get(getattr(issue, "field", ""), getattr(issue, "field", ""))
            term = str(getattr(issue, "term", "") or "").strip()
            labels.append(f"{field}（{term}）" if term else field)
        super().__init__("広告表現の確認が必要です: " + "、".join(labels))


def check_required(fields: dict) -> list:
    """必要表示事項のうち、欠落または意味・型が無効なキーを返す（空なら公開OK）。"""
    values = fields or {}
    return [k for k in REQUIRED_FOR_PUBLISH
            if str(values.get(k) or "").strip() in _MISSING_PLACEHOLDERS
            or not _required_value_is_meaningful(k, values.get(k))]


class _VisibleText(HTMLParser):
    """印刷面で読める本文だけを抽出する。head/script/style/comment/inline hidden は除外。"""

    _HIDDEN_TAGS = {"head", "script", "style", "template", "title", "svg"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._tag_stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag_name = tag.lower()
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        style = re.sub(r"\s+", "", attr_map.get("style", "").lower())
        is_hidden = (
            tag_name in self._HIDDEN_TAGS
            or "hidden" in attr_map
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if is_hidden:
            self._hidden_depth += 1
        if tag_name not in self._VOID_TAGS:
            self._tag_stack.append((tag_name, is_hidden))

    def handle_endtag(self, tag):
        tag_name = tag.lower()
        while self._tag_stack:
            opened, was_hidden = self._tag_stack.pop()
            if was_hidden and self._hidden_depth:
                self._hidden_depth -= 1
            if opened == tag_name:
                break

    def handle_startendtag(self, tag, attrs):
        return None

    def handle_data(self, data):
        if not self._hidden_depth and data.strip():
            self.parts.append(data.strip())


def _visible_text_from_html(html_text: str) -> str:
    parser = _VisibleText()
    parser.feed(html_text or "")
    parser.close()
    return " ".join(parser.parts)


def _visible_norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def check_required_visible_in_html(fields: dict, html_text: str) -> list:
    """入力上は足りているが、公開HTMLの可視本文に焼かれていない必要表示事項を返す。"""
    missing = list(check_required(fields))
    if missing:
        return missing
    visible = _visible_norm(_visible_text_from_html(html_text))
    for key in REQUIRED_FOR_PUBLISH:
        expected = _visible_norm((fields or {}).get(key))
        if expected and expected not in visible:
            missing.append(key)
    return missing


def theme_fields(company: dict | None = None, *, accent: str = "", font: str = "") -> dict:
    """テーマトークン(accent/accent_ink/font_display)を返す。company.brand_color を既定に、
    per-maisoku の accent 指定があれば優先。不正な色はサニタイズ（既定へ）。"""
    a = (accent or (company or {}).get("brand_color") or "").strip()
    if not _valid_hex(a):
        a = DEFAULT_ACCENT
    f = (font or (company or {}).get("display_font") or "gothic").strip()
    return {"accent": a, "accent_ink": _accent_ink(a), "accent_soft": _accent_soft(a),
            "accent_text": _accent_text(a),
            "font_display": DISPLAY_FONTS.get(f, DISPLAY_FONTS["gothic"])}


def _rel_lum(r: int, g: int, b: int) -> float:
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _hex_rgb(hexcolor: str):
    h = (hexcolor or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return (179, 38, 30)


_TEXT_MIN_CONTRAST = 4.5   # 紙に刷る文字の下限（WCAG AA 相当）


def _accent_text(hexcolor: str) -> str:
    """紙（白地）の上に**文字として**置くブランド色。

    黄色や水色をブランド色にしている会社は珍しくない。地の色としては映えるが、
    白地に同じ色で小見出しを打つと読めない。色味は保ったまま、白地との
    コントラストが下限を超えるまで暗くする（色相を変えないので別の色には見えない）。
    """
    r, g, b = _hex_rgb(hexcolor)
    for _ in range(24):
        contrast = (1.0 + 0.05) / (_rel_lum(r, g, b) + 0.05)
        if contrast >= _TEXT_MIN_CONTRAST:
            break
        r, g, b = (max(0, round(c * 0.88)) for c in (r, g, b))
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def _accent_soft(hexcolor: str) -> str:
    """アクセント色の淡い方（帯の地・罫の副色）。白と混ぜて作るので必ず同系色になる。

    様式によっては濃淡2段でブランド色を使う。淡い方を別入力にすると業者が2色
    決めることになるので、濃い方から機械的に作る。
    """
    h = (hexcolor or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        r, g, b = (179, 38, 30)
    mix = lambda c: round(c + (255 - c) * 0.62)   # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(mix(r), mix(g), mix(b))


def _derive(fields: dict) -> dict:
    out = dict(fields)
    price = str(out.get("price", ""))
    m = re.match(r"^([0-9０-９,，.．]+)(.*)$", price)
    out["price_num"] = m.group(1) if m else price
    out["price_unit"] = m.group(2) if m else ""
    if not str(out.get("price_label", "")).strip():
        out["price_label"] = "価格"
    if not str(out.get("floor", "")).strip():
        out["floor"] = str(out.get("floors_total", "") or "")
    caption_defaults = {
        "photo_main_caption": "メイン写真",
        "photo_sub1_caption": "写真1",
        "photo_sub2_caption": "写真2",
        "photo_sub3_caption": "写真3",
        "photo_floorplan_caption": "間取り図",
        "photo_map_caption": "案内図",
    }
    for key, default in caption_defaults.items():
        if not str(out.get(key, "")).strip():
            out[key] = default
    for key in ("photo_main_tag", "photo_sub1_tag", "photo_sub2_tag", "photo_sub3_tag"):
        if not str(out.get(key, "")).strip():
            out[key] = "写真"
    if not str(out.get("photo_map_tag", "")).strip():
        out["photo_map_tag"] = "地図"
    if not str(out.get("income_basis_label", "")).strip():
        out["income_basis_label"] = "月額賃料"
    return out


# 販売図面に焼く写真スロット＝Vault素材参照のみ許可（外部URL/貼付画像は不可・provenanceゲート）。
PHOTO_SLOTS = ("photo_main", "photo_sub1", "photo_sub2", "photo_sub3", "photo_floorplan", "photo_map")
PHOTO_EMPTY_LABELS = {
    "photo_main": "画像なし",
    "photo_sub1": "画像なし",
    "photo_sub2": "画像なし",
    "photo_sub3": "画像なし",
    "photo_floorplan": "間取り図なし",
    "photo_map": "案内図なし",
}
PRESERVED_KEYS = (*EDITABLE_FIELD_KEYS, *PHOTO_SLOTS, "property", "_variant",
                  "_xlsx_variant", "_accent", "_font")


def _screen_fit(html_text: str, variant: str) -> str:
    """A4の印刷寸法を保ったまま、画面上だけビューポート幅へ縮小する。"""
    width_px = 1122.52 if variant == "shuueki" else 793.70
    style = (
        '<style id="ainote-screen-fit">'
        f'@media screen and (max-width:{width_px}px){{'
        'html,body{min-width:0!important;overflow-x:hidden!important}'
        '.sheet,.page{margin-left:0!important;margin-right:0!important;'
        f'zoom:calc(100vw / {width_px}px)}}}}'
        '</style>'
    )
    if "</head>" in html_text:
        return html_text.replace("</head>", style + "</head>", 1)
    return style + html_text


def _mark_draft(html_text: str) -> str:
    """Mark every non-publish render as a draft on screen and paper."""
    style = (
        '<style id="ainote-draft-mark">'
        '.ainote-draft-mark{position:fixed;z-index:2147483647;top:5mm;right:5mm;'
        'padding:2mm 3.5mm;border:.45mm solid #8a1b12;background:#fff;color:#8a1b12;'
        'font:700 10pt/1.2 sans-serif;letter-spacing:0;box-shadow:0 1mm 3mm rgba(0,0,0,.12)}'
        '@media print{.ainote-draft-mark{position:fixed;box-shadow:none}}'
        '</style>'
    )
    badge = '<div class="ainote-draft-mark" role="status">下書き・配布不可</div>'
    if "</head>" in html_text:
        html_text = html_text.replace("</head>", style + "</head>", 1)
    else:
        html_text = style + html_text
    if "<body>" in html_text:
        return html_text.replace("<body>", "<body>" + badge, 1)
    return badge + html_text


def _resolve_photos(fields: dict, data_dir, prop: str, *, publish: bool):
    """写真スロットを Vault 素材へ解決（provenance）。外部URL/貼付画像は publish時 fail-closed・
    preview時プレースホルダ。data_dir 無しは検証不能＝空スロットのみ通し他は空にする（無断転載防止）。
    戻り: (置換後fields, manifest=解決したasset_sha256集合)。"""
    from hub_core import provenance
    out = dict(fields)
    manifest = []
    for slot in PHOTO_SLOTS:
        raw = str(out.get(slot) or "").strip()
        if not raw:
            out[slot] = ""
            continue
        if data_dir is None:
            # 検証できない＝焼かない（未検証バイトを販売図面に入れない）
            if publish:
                from hub_core.provenance import ProvenanceError
                raise ProvenanceError(403, "写真の検証に data_dir が必要です（公開は素材ハブ経由）。")
            out[slot] = ""
            continue
        src, sha = provenance.resolve_photo_slot(data_dir, raw, prop, publish=publish,
                                                 purpose="advertise")
        out[slot] = src
        if sha:
            manifest.append(sha)
    return out, manifest


def _photo_display_fields(fields: dict) -> dict:
    """解決済み写真に合わせて、紙面へ出す説明と空表示を確定する。

    キャプションとタグは写真そのものの説明なので、写真が解決できなかった場合は
    紙面へ流さない。プレースホルダには入力文ではなく、スロット種別ごとの中立表示を使う。
    """
    out = dict(fields)
    for slot, empty_label in PHOTO_EMPTY_LABELS.items():
        caption_key = f"{slot}_caption"
        tag_key = f"{slot}_tag"
        has_photo = bool(str(out.get(slot) or "").strip())
        out[f"{slot}_empty_label"] = empty_label
        out[f"{slot}_alt"] = str(out.get(caption_key) or "") if has_photo else empty_label
        if not has_photo:
            out[caption_key] = ""
            if tag_key in out:
                out[tag_key] = ""
    return out


def render_flyer(data: dict, variant: str = DEFAULT_VARIANT, *,
                 data_dir=None, prop: str = "", publish: bool = False, company: dict | None = None) -> str:
    """様式(variant)に data を差し込んだ販売図面HTMLを返す。値はHTMLエスケープ。
    写真スロットは Vault 素材参照のみ許可＝外部URL/貼付画像は焼けない（無断転載防止・provenance）。
    publish=True は未許諾素材で fail-closed（ProvenanceError）。"""
    if publish:
        return render_flyer_publish(data, variant, data_dir, prop, company=company)[0]
    if variant not in VARIANTS:
        variant = DEFAULT_VARIANT
    fields = _derive(fields_with_company(
        {**default_fields(), **(data or {})}, company or {}, overwrite=company is not None))
    for k, v in theme_fields(company, accent=str((data or {}).get("_accent") or ""),
                             font=str((data or {}).get("_font") or "")).items():
        fields.setdefault(k, v)
    fields, _ = _resolve_photos(fields, data_dir, prop, publish=publish)
    fields = _photo_display_fields(fields)
    tpl = _TPL_DIR / f"{variant}.html"
    if not tpl.exists():
        tpl = _TPL_DIR / f"{DEFAULT_VARIANT}.html"
    src = tpl.read_text(encoding="utf-8")

    def sub(mm):
        key = mm.group(1).strip()
        # 写真スロットは解決済み src（data:URI等）を verbatim（エスケープすると data:URI が壊れる）
        if key in PHOTO_SLOTS:
            return str(fields.get(key, ""))
        return _e(fields.get(key, ""))

    return _mark_draft(_screen_fit(re.sub(r"\{\{([^}]+)\}\}", sub, src), variant))


def validate_for_publish(data: dict, data_dir, prop: str,
                         company: dict | None = None) -> tuple[dict, list]:
    """全ての顧客向けマイソク出力に共通する fail-closed 検証。

    会社帯は保存本文でなく、版に束縛された会社スナップショットから必ず再構成する。
    指摘に根拠を記録する仕組みはまだ無いため、広告表現は level を問わず残件があれば止める。
    """
    from hub_core import ad_rules

    fields = _derive(fields_with_company(
        {**default_fields(), **(data or {})}, company or {}, overwrite=company is not None))
    fields, manifest = _resolve_photos(fields, data_dir, prop, publish=True)
    fields = _photo_display_fields(fields)
    missing = check_required(fields)
    if missing:
        raise ComplianceError(missing)
    raw_distance = str(fields.get("walk_distance_m") or "").strip()
    try:
        distance_m = float(raw_distance) if raw_distance else None
    except ValueError:
        distance_m = None
    issues = ad_rules.review(fields, distance_m=distance_m)
    if issues:
        raise AdComplianceError(issues)
    return fields, manifest


def render_flyer_publish(data: dict, variant: str, data_dir, prop: str, company: dict | None = None) -> tuple[str, list]:
    """公開用レンダ＝写真を fail-closed 検証し (html, manifest) を返す。manifest は render 出力から
    導出した asset_sha256 集合（呼び手申告でない）。公開ゲートはこの manifest を検証する。"""
    if variant not in VARIANTS:
        variant = DEFAULT_VARIANT
    fields, manifest = validate_for_publish(data, data_dir, prop, company=company)
    for k, v in theme_fields(company, accent=str((data or {}).get("_accent") or ""),
                             font=str((data or {}).get("_font") or "")).items():
        fields.setdefault(k, v)
    tpl = _TPL_DIR / f"{variant}.html"
    if not tpl.exists():
        tpl = _TPL_DIR / f"{DEFAULT_VARIANT}.html"
    src = tpl.read_text(encoding="utf-8")

    def sub(mm):
        key = mm.group(1).strip()
        if key in PHOTO_SLOTS:
            return str(fields.get(key, ""))
        return _e(fields.get(key, ""))

    rendered = _screen_fit(re.sub(r"\{\{([^}]+)\}\}", sub, src), variant)
    missing = check_required_visible_in_html(fields, rendered)
    if missing:
        raise ComplianceError(missing)
    return rendered, manifest
