"""hub_core/juusetsu_draft.py — フィールド入力から重要事項説明書(35条)の下書き(markdown)を生成。

弱いローカルLLMに本文を書かせず、法定様式(juusetsu_schema.json)に沿って**決定論的に**下書きを組む。
入力された項目は本文へ差し込み、記入・確認が要る法定項目は ☐ のチェックリストとして残す
(宅建士が公開/交付前に確認・記名確定する前提)。金メッキしない・断定しない。stdlibのみ。
"""
from __future__ import annotations

import datetime
import html
import json


FIELDS = [
    # (key, ラベル, グループ)
    ("deal_type", "取引態様", "取引"),
    ("torihiki_keitai", "取引形態（貸主/代理/媒介）", "取引"),
    # 宅建業者
    ("company_name", "宅地建物取引業者（商号）", "宅建業者"),
    ("license_no", "免許証番号", "宅建業者"),
    ("company_address", "主たる事務所の所在地", "宅建業者"),
    ("company_tel", "電話番号", "宅建業者"),
    ("association", "保証協会", "宅建業者"),
    # 宅建士
    ("takkenshi_name", "説明する宅地建物取引士", "宅建士"),
    ("takkenshi_reg", "登録番号", "宅建士"),
    # 物件
    ("property_name", "物件名", "物件"),
    ("address", "所在地", "物件"),
    ("floors", "階数・所在階", "物件"),
    ("structure", "種類・構造", "物件"),
    ("area", "専有面積（㎡）", "物件"),
    ("layout", "間取り", "物件"),
    ("built", "築年月", "物件"),
    # 契約条件（売買）
    ("price", "売買代金（総額）", "契約条件"),
    # 契約条件（賃貸）
    ("rent", "賃料（月額）", "契約条件"),
    ("kanri_fee", "管理費・共益費（月額）", "契約条件"),
    ("shikikin", "敷金", "契約条件"),
    ("reikin", "礼金", "契約条件"),
    ("term", "契約期間", "契約条件"),
    ("koushinryo", "更新料", "契約条件"),
    # インフラ
    ("water", "水道", "インフラ"),
    ("electric", "電気", "インフラ"),
    ("gas", "ガス", "インフラ"),
    ("drainage", "排水", "インフラ"),
    # 法令・ハザード
    ("youto", "用途地域", "法令・ハザード"),
    ("kenpei_yoseki", "建ぺい率／容積率", "法令・ハザード"),
    ("flood", "洪水浸水想定区域", "法令・ハザード"),
    ("landslide", "土砂災害警戒区域", "法令・ハザード"),
]
FIELD_KEYS = [f[0] for f in FIELDS]


def _v(fields: dict, key: str) -> str:
    return str((fields or {}).get(key) or "").strip()


def _row(label: str, value: str) -> str:
    return f"| {label} | {value if value else '（未記入 ☐）'} |"


# 会社プロフィール(company.json)→ 重説の宅建業者/宅建士フィールドへの補完（法定の業者情報を欠かさない）。
_COMPANY_TO_JU = {"name": "company_name", "license_no": "license_no", "address": "company_address",
                  "tel": "company_tel", "association": "association", "staff": "takkenshi_name"}


def fill_from_company(fields: dict, company: dict) -> dict:
    """業者プロフィール由来の宅建業者/宅建士フィールドを補完（フォーム側に値があればそちら優先）。
    重説は宅建業者・免許の記載が法定＝フォーム往復に依存せず profile から必ず入れる。"""
    out = dict(fields or {})
    for ck, jk in _COMPANY_TO_JU.items():
        v = str((company or {}).get(ck) or "").strip()
        if v and not str(out.get(jk) or "").strip():
            out[jk] = v
    return out


# fillルール: (項目テキストに含むキーワード, 入力フィールド, 許可セクション集合)。
# **賃貸様式(頭書/数字)と売買様式(第一面/物件表示/Ⅰ-n)の両方を1つの表で扱う**（かつては賃貸のセクション番号
# 固定で、売買様式では全項目がセクション不一致でスキップ＝本文が☐だらけになるバグがあった。2026-07-13是正）。
# 具体的なキーワードを先に置く(最初の一致を採用)。allowed に無いセクションでは差さない(誤fill防止・金メッキ禁止)。
_FILL_RULES = [
    # 宅建業者・宅建士（賃貸=頭書 / 売買=第一面）
    ("商号又は名称", "company_name", {"頭書", "第一面"}),
    ("免許証番号", "license_no", {"頭書", "第一面"}),
    # 売買の第一面は「主たる事務所の所在地・電話番号」が1行。片方だけ入れると
    # 電話番号の入力欄が黙って捨てられるので、2つまとめて入れる。
    ("主たる事務所", ("company_address", "company_tel"), {"頭書", "第一面"}),
    ("事務所・所在地", ("company_address", "company_tel"), {"頭書"}),
    ("宅地建物取引士：氏名", "takkenshi_name", {"頭書", "第一面"}),
    ("取引士：氏名", "takkenshi_name", {"頭書", "第一面"}),
    ("宅地建物取引士：登録番号", "takkenshi_reg", {"頭書", "第一面"}),
    ("取引士：登録番号", "takkenshi_reg", {"頭書", "第一面"}),
    ("保証協会の名称", "association", {"1"}),
    # 取引態様。**2つは別の項目**なので取り違えない。
    #   deal_type       = 売買か賃貸か（取引の種別）
    #   torihiki_keitai = 貸主／売主／代理／媒介のどれで関与するか（法34条の明示事項）
    # 賃貸の頭書「取引態様（貸主・代理・仲介（媒介）の別）」は後者。ここに deal_type を
    # 入れると、入力した「媒介（専任）」が消えて「賃貸」が印字される（2026-08-07 是正）。
    ("取引態様", "torihiki_keitai", {"頭書"}),
    ("取引の態様：自ら売主", "torihiki_keitai", {"第一面"}),
    ("売買・交換の別", "deal_type", {"第一面"}),
    # 物件（賃貸=3 / 売買=物件表示・Ⅰ-16）。売買は「建物：所在地」を特定して土地:所在地・地番への誤fillを避ける。
    ("建物：所在地", "address", {"物件表示"}),
    ("所在地", "address", {"3"}),
    ("建物の名称", "property_name", {"物件表示", "Ⅰ-16"}),
    ("建物名称", "property_name", {"3"}),
    ("階数等", "floors", {"3"}),
    ("種類・構造", "structure", {"3"}),
    # 売買の物件表示は「種類及び構造・階数」が1行。片方だけ入れると階数が落ちる。
    ("建物：種類及び構造・階数", ("structure", "floors"), {"物件表示"}),
    ("建物：新築年月", "built", {"物件表示"}),
    ("間取り", "layout", {"3"}),
    ("面積（㎡）", "area", {"3"}),
    ("床面積（各階", "area", {"物件表示"}),
    ("専有面積", "area", {"Ⅰ-16"}),
    ("建築年月", "built", {"3"}),
    # 契約条件（売買）
    ("売買代金の総額", "price", {"Ⅱ-1"}),
    # 契約条件（賃貸）
    ("賃料", "rent", {"15", "17"}),
    ("管理費", "kanri_fee", {"17"}),
    ("共益費", "kanri_fee", {"17"}),
    ("敷金", "shikikin", {"17", "21"}),
    ("礼金", "reikin", {"17"}),
    ("契約期間", "term", {"15"}),
    ("更新料", "koushinryo", {"17"}),
    # 法令・ハザード（賃貸=5）
    # 賃貸の第5節は「法令に基づく制限の内容」1行にまとまっているので、まとめて入れる。
    # 個別キーワードで当てると、項目文の**説明部分**（「容積率・建ぺい率…は原則説明不要」）に
    # 引っかかって片方だけ入り、もう片方が黙って落ちる。
    ("法令に基づく制限の内容", ("youto", "kenpei_yoseki"), {"5"}),
    ("用途地域", "youto", {"Ⅰ-3"}),
    ("建ぺい率", "kenpei_yoseki", {"Ⅰ-3"}),
    ("容積率", "kenpei_yoseki", {"Ⅰ-3"}),
    # 供給施設・排水（賃貸=6 / 売買=Ⅰ-5）。売買側は「直ちに利用可能な施設」の行だけに入れる
    # （「整備予定」の行に同じ値を書くと、予定が無いのに有るように読める）。
    ("電気", "electric", {"6"}),
    ("電気：直ちに", "electric", {"Ⅰ-5"}),
    ("水道", "water", {"6"}),
    ("飲用水：直ちに", "water", {"Ⅰ-5"}),
    ("ガス", "gas", {"6"}),
    ("ガス：直ちに", "gas", {"Ⅰ-5"}),
    ("排水", "drainage", {"6"}),
    ("排水（汚水）：直ちに", "drainage", {"Ⅰ-5"}),
    # 災害（賃貸=9・11 / 売買=Ⅰ-3・Ⅰ-10）
    ("土砂災害警戒区域", "landslide", {"9", "Ⅰ-10"}),
    ("該当図面における当該宅地建物の所在地", "flood", {"11"}),
    ("水防法に基づく洪水", "flood", {"Ⅰ-3"}),
]


# 1行に2つ入れるときの見出し（「所在地／TEL 03-…」の形にして読めるようにする）
_JOIN_PREFIX = {"company_tel": "TEL "}


def _fill_for(item: str, fields: dict, sec_no: str = "") -> str:
    """項目テキストにマッチする入力値を返す（賃貸/売買両様式・セクション許可制・誤fill防止）。最初の一致を採用。

    fk が複数キーのときは、値のあるものだけを「／」で連結する（様式の1行が
    複数の入力欄に対応する場合。片方だけ入れて残りを捨てない）。
    """
    for kw, fk, allowed in _FILL_RULES:
        if kw in item and str(sec_no) in allowed:
            keys = fk if isinstance(fk, tuple) else (fk,)
            vals = [f"{_JOIN_PREFIX.get(k, '')}{_v(fields, k)}" for k in keys if _v(fields, k)]
            if vals:
                return "／".join(vals)
    return ""


def _load_schema(is_chintai: bool):
    import json as _j
    from pathlib import Path as _P
    p = _P(__file__).parent / "templates" / "office" / "juusetsu_schema.json"
    try:
        s = _j.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return s.get("chintai" if is_chintai else "baibai", [])


def render_juusetsu_md(fields: dict, *, deal_type: str = "", property_kind: str = "", today: str = "") -> str:
    """フィールド辞書 → 重要事項説明書の markdown 下書き（**国交省標準様式の全法定項目**に沿う）。
    入力値は該当項目へ自動fill・残りは ☐（宅地建物取引士が交付前に確認・記入）。法定項目は捏造せず様式通り。

    様式選択（賃貸/売買）は deal_taxonomy の正規形 transaction 経由に一本化（キーワード二値判定を置換）。
    property_kind（区分所有等）が確定していれば、区分所有追加章（schema の applies_property_kinds）を出し分ける。
    """
    from . import deal_taxonomy as _tax
    dt = (deal_type or _v(fields, "deal_type")).strip()
    tx = _tax.normalize_transaction(dt)
    pk = _tax.normalize_property_kind(property_kind or _v(fields, "property_kind"))
    is_chintai = (tx != "sale")   # 売買のみ baibai・それ以外(賃貸/未設定)は賃貸様式（現行フォールバック維持）
    kind_label = "建物賃貸借用" if is_chintai else "売買・交換用"
    made = (today or "").strip()
    sections = _load_schema(is_chintai)

    lines = []
    lines.append(f"# 重要事項説明書（{kind_label}）")
    lines.append("<!-- ainote-juusetsu-schema:" + json.dumps(
        {"schema": "chintai" if is_chintai else "baibai", "transaction": tx or "",
         "property_kind": pk or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + " -->")
    lines.append("")
    lines.append("> 【下書き・交付不可】宅地建物取引業法第35条に基づく重要事項説明書の下書き"
                 "（国交省標準様式準拠）。未記入項目は交付・説明前に宅地建物取引士が調査・確認して記入し、記名のうえ確定してください。"
                 "本下書きは宅地建物取引士の調査義務・説明義務を代替しません。")
    lines.append("")
    lines.append(f"- 作成日: {made if made else '（　年　月　日）'}")
    lines.append("- 被説明者（相手方）: ＿＿＿＿＿＿＿＿　殿")
    lines.append("")

    if not sections:
        # スキーマが読めない場合の最小フォールバック（データだけは載せる）
        lines.append("（標準様式データを読み込めませんでした。物件: "
                     + _v(fields, "property_name") + " / 賃料: " + _v(fields, "rent") + "）")
        return "\n".join(lines)

    for sec in sections:
        # property_kind による章の出し分け（区分所有追加章は property_kind=condo のときだけ）。
        if not _tax.section_applies(sec.get("applies_property_kinds"), pk):
            continue
        no = str(sec.get("no") or "")
        title = str(sec.get("title") or "")
        lines.append(f"## {no}　{title}" if no else f"## {title}")
        lines.append("")
        for item in sec.get("items", []):
            val = _fill_for(item, fields, no)
            if val:
                lines.append(f"- {item}：**{val}**")
            else:
                lines.append(f"- ☐ {item}")
        lines.append("")

    lines.append("## 宅地建物取引士による記名")
    lines.append("")
    lines.append("上記のとおり、宅地建物取引業法第35条に規定する重要事項を説明しました。")
    lines.append("")
    tk = _v(fields, "takkenshi_name")
    tr = _v(fields, "takkenshi_reg")
    lines.append(f"- 宅地建物取引士（記名）: {tk or '＿＿＿＿＿＿'}　登録番号: {tr or '＿＿＿＿'}")
    lines.append("- ☐ 交付前に「記名確定」で確定し、改ざん検知つきの監査台帳に記録する")
    return "\n".join(lines)


def parse_pasted(text: str) -> dict:
    """自由記述の貼り付けから既知フィールドを緩く抽出（ラベル: 値／【見出し】に対応）。
    抽出できない項目は空のまま（金メッキしない）。厳密パースでなく補助。"""
    fields: dict = {}
    aliases = {
        "物件名": "property_name", "所在地": "address", "構造": "structure",
        "専有面積": "area", "面積": "area", "間取り": "layout", "築年月": "built",
        "売買代金": "price", "価格": "price",
        "賃料": "rent", "管理費": "kanri_fee", "共益費": "kanri_fee", "敷金": "shikikin",
        "礼金": "reikin", "契約期間": "term", "更新料": "koushinryo",
        "宅建業者": "company_name", "免許": "license_no",
        "宅建士": "takkenshi_name", "登録番号": "takkenshi_reg",
        "水道": "water", "電気": "electric", "ガス": "gas", "排水": "drainage",
        "用途地域": "youto", "建ぺい率": "kenpei_yoseki", "容積率": "kenpei_yoseki",
        "洪水": "flood", "土砂": "landslide",
    }
    for raw in (text or "").replace("／", "/").splitlines():
        line = raw.strip().lstrip("・-").strip()
        if not line:
            continue
        for sep in ("：", ":"):
            if sep in line:
                k, v = line.split(sep, 1)
                k = k.strip().strip("【】[]")
                v = v.strip()
                for alias, fk in aliases.items():
                    if alias in k and v and fk not in fields:
                        fields[fk] = v
                # 取引態様: 賃貸/売買は deal_type、媒介/代理/貸主は torihiki_keitai へ振り分け
                if ("取引態様" in k or "取引種別" in k) and v:
                    if any(x in v for x in ("賃貸", "貸借", "売買", "居住")):
                        fields.setdefault("deal_type", v)
                    if any(x in v for x in ("媒介", "代理", "仲介", "貸主")):
                        fields.setdefault("torihiki_keitai", v)
                break
    return fields
