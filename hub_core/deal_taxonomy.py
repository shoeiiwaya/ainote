"""hub_core/deal_taxonomy.py — 重説の取引種別を単一の正規形に正規化する合流点。

あいのて には取引種別の語彙が三系統ある（設計書 design-juusetsu-branch.md §1.2）:
  1. cases.取引種別（自由文: 「賃貸借（居住用）」「売買」…）
  2. operations.py の四象限コード（lease_tenant / sale_buyer …＝取引×立場）
  3. ri-chousa の二軸（deal_type∈{売買,賃貸} × property_kind∈{土地,戸建,区分,収益一棟}）
この三つを重説の様式選択に必要な正規形へ落とす（§3.1）。正規形は ri-chousa の値域に一致させ、
既存実装（applies_to / applies_property_kinds）をそのまま活かせるようにする。

規律: **推定しない項目は None を返す**（金メッキ禁止・§3.2）。property_kind は cases・operations の
どちらも保持しないため、明示入力が無ければ None（会話ファーストで1問聞く＝§5.2）。stdlib のみ。
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import NamedTuple, Optional

# --- 正規形の値域（ri-chousa の VALID_PROPERTY_KINDS / deal_type と対応）------------
TRANSACTIONS = ("sale", "lease")                          # 取引の別（売買・交換 / 貸借）
PROPERTY_KINDS = ("land", "detached", "condo", "income_whole")  # 物件種別
LEASE_USES = ("residential", "business")                  # 貸借の用途（居住用 / 事業用）
LEASE_TERMS = ("regular", "fixed", "land_lease")          # 貸借の型（普通借家 / 定期借家 / 借地）

# 表示ラベル（UI の1問フロー・確定表示に使う日本語）。
TRANSACTION_LABELS = {"sale": "売買", "lease": "賃貸"}
PROPERTY_KIND_LABELS = {
    "land": "土地", "detached": "戸建", "condo": "区分所有", "income_whole": "一棟（収益）",
}
LEASE_USE_LABELS = {"residential": "居住用", "business": "事業用"}
LEASE_TERM_LABELS = {"regular": "普通借家", "fixed": "定期借家", "land_lease": "借地"}

# ri-chousa の日本語 property_kind → 正規形（schema.json の applies_property_kinds も同じ値域）。
_KIND_JA = {"土地": "land", "戸建": "detached", "区分": "condo", "収益一棟": "income_whole"}
# 物件種別の表記ゆれ吸収（確定できる語のみ・優先度順で判定）。区分/一棟は戸建より先に見る。
_KIND_SUBSTR = (
    ("区分", "condo"), ("マンション", "condo"),
    ("収益", "income_whole"), ("一棟", "income_whole"),
    ("戸建", "detached"), ("一戸建", "detached"),
    ("土地", "land"),
)


class NormalizedDeal(NamedTuple):
    """重説様式の選択に必要な正規形。推定できない軸は None（金メッキしない）。"""
    transaction: Optional[str]
    property_kind: Optional[str]
    lease_use: Optional[str]
    lease_term: Optional[str]


def normalize_transaction(raw) -> Optional[str]:
    """自由文・四象限コード → transaction（sale / lease）。判定不能は None。

    四象限コード（operations.DEAL_TYPES）は接頭辞 sale_/lease_ で落とす。自由文は
    operations.normalize_deal_type と同じ語彙で判定するが、あちらは常に4値へ倒す（既定 lease_tenant）
    のに対し、ここは**未知を None に保つ**（§3.2 の「推定不能は None」を満たすため直接判定する）。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("sale_"):
        return "sale"
    if low.startswith("lease_"):
        return "lease"
    if any(k in s for k in ("売買", "購入", "売却", "交換")) or any(k in low for k in ("sale", "buy", "baibai")):
        return "sale"
    if any(k in s for k in ("賃貸", "貸借", "居住", "借家", "借地")) or any(k in low for k in ("lease", "rent", "chintai", "chinshaku")):
        return "lease"
    return None


def normalize_property_kind(raw) -> Optional[str]:
    """物件種別文字列 → property_kind（land/detached/condo/income_whole）。判定不能は None。"""
    s = str(raw or "").strip()
    if not s:
        return None
    if s in _KIND_JA:
        return _KIND_JA[s]
    low = s.lower()
    if low in PROPERTY_KINDS:
        return low
    for token, kind in _KIND_SUBSTR:
        if token in s:
            return kind
    return None


def normalize_lease_use(raw) -> Optional[str]:
    """貸借の用途文字列 → lease_use（residential/business）。明示が無ければ None（既定は様式側で仮置き）。"""
    s = str(raw or "")
    if any(k in s for k in ("居住", "住居", "住宅")):
        return "residential"
    if any(k in s for k in ("事業", "店舗", "事務所", "オフィス", "業務")):
        return "business"
    return None


def normalize_lease_term(raw) -> Optional[str]:
    """貸借の型文字列 → lease_term（regular/fixed/land_lease）。明示が無ければ None。"""
    s = str(raw or "")
    if "定期" in s:
        return "fixed"
    if "普通" in s:
        return "regular"
    if "借地" in s:
        return "land_lease"
    return None


def normalize(deal_type_raw="", property_kind_raw="", *, lease_use_raw="", lease_term_raw="") -> NormalizedDeal:
    """三系統の取引種別入力 → 正規形 NormalizedDeal。

    - transaction は deal_type_raw（自由文 or 四象限コード）から。
    - property_kind は property_kind_raw（明示）からのみ。cases/operations は保持しないので推定しない。
    - lease_use / lease_term は transaction=lease のときだけ、明示 raw or deal_type_raw の記述から拾う。
    """
    tx = normalize_transaction(deal_type_raw)
    pk = normalize_property_kind(property_kind_raw)
    lu = lt = None
    if tx == "lease":
        lu = normalize_lease_use(lease_use_raw or deal_type_raw)
        lt = normalize_lease_term(lease_term_raw or deal_type_raw)
    return NormalizedDeal(tx, pk, lu, lt)


def schema_key(transaction) -> str:
    """正規形 transaction → juusetsu_schema.json のキー。sale→baibai・それ以外(lease/None)→chintai。

    None（種別未設定）を chintai に倒すのは render_juusetsu_md の現行フォールバック（既定=賃貸様式）と
    同一挙動を保つため。売買のときだけ baibai に切る。
    """
    return "baibai" if transaction == "sale" else "chintai"


def section_applies(applies_property_kinds, property_kind) -> bool:
    """章メタ applies_property_kinds（例: 区分所有追加=["区分"]）に property_kind が該当するか。

    - メタが無い（None/空）＝全物件種別に適用 → True。
    - property_kind が未確定（None）＝様式を隠さない（法定章の取りこぼし防止）→ True。
      種別が確定してから隠す（区分所有章は property_kind=condo のときだけ出す＝§4.4）。
    """
    if not applies_property_kinds:
        return True
    if property_kind is None:
        return True
    allowed = {normalize_property_kind(v) for v in applies_property_kinds}
    allowed.discard(None)
    return property_kind in allowed


# --- 記名確定の種別必須ゲート（§6 fail-closed）------------------------------------
# 種別必須ラベル: ドラフト本文に「記入済（☐でない）」で存在すべき法定コア項目。非該当（他種別・非区分）は
# 含めない（§6.2 偽充足・埋めようのない欄での永久停止を避ける）。ラベルは部分一致で本文行を探す。
_REQUIRED_COMMON = ("免許証番号", "宅地建物取引士")
_REQUIRED_SALE = ("代金",)      # Ⅱ-1 代金（売買のみ）
_REQUIRED_LEASE = ("賃料",)     # 17章 賃料（賃貸のみ）
_REQUIRED_CONDO = ("敷地利用権",)  # Ⅰ-16 区分所有追加（property_kind=condo のときだけ）


def required_labels(transaction, property_kind=None) -> list:
    """transaction × property_kind に対する種別必須ラベル集合（§4 の4群を反映）。"""
    labels = list(_REQUIRED_COMMON)
    if transaction == "sale":
        labels += list(_REQUIRED_SALE)
        if property_kind == "condo":
            labels += list(_REQUIRED_CONDO)
    elif transaction == "lease":
        labels += list(_REQUIRED_LEASE)
    return labels


def transaction_from_draft(text) -> Optional[str]:
    """生成済みドラフト本文から transaction を判定（様式タイトル or 取引種別行）。判定不能は None。"""
    t = str(text or "")
    if "売買・交換用" in t:
        return "sale"
    if "建物賃貸借用" in t or "建物賃貸借" in t:
        return "lease"
    for raw in t.splitlines():
        s = raw.strip().lstrip("-・ ").strip()
        for key in ("取引種別", "取引態様", "売買・交換の別"):
            if s.startswith(key):
                rest = s[len(key):]
                for sep in ("：", ":"):
                    if sep in rest:
                        rest = rest.split(sep, 1)[1]
                        break
                tx = normalize_transaction(rest.strip().strip("*").strip())
                if tx:
                    return tx
    return None


def _is_placeholder(text) -> bool:
    """実ドラフトでない（未配線プレースホルダ）か。ゲート対象外にする。"""
    t = str(text or "")
    if "重説ドラフトが見つかりません" in t:
        return True
    return "## " not in t and "|" not in t


def _is_form_draft(text) -> bool:
    """決定論フォーム様式（render_juusetsu_md 出力）のドラフトか。タイトル「重要事項説明書（…用）」で判定。

    ri-chousa の skeleton（「ドラフトスケルトン」＋要確認一覧）は完成モデルが異なる（M7で合流）ため、
    種別ごとの欠落チェックは form 様式にのみ適用する。
    """
    t = str(text or "")
    return "重要事項説明書（" in t and "用）" in t


def _label_filled(text, label) -> bool:
    """本文に label を含む行があり、かつ記入済（☐でなく値がある）か。"""
    for raw in str(text or "").splitlines():
        s = raw.strip()
        if label not in s or "☐" in s:
            continue
        if label == "宅地建物取引士" and "宅地建物取引士（記名）" in s:
            match = re.search(
                r"宅地建物取引士（記名）\s*[:：]\s*(.*?)\s+登録番号\s*[:：]\s*(.+)$", s)
            if match and all(value.strip(" *_＿") for value in match.groups()):
                return True
        # 記入済フォーマット: `- {項目}：**{値}**` / markdown表 `| ラベル | 値 |`
        if "：**" in s or (s.startswith("|") and s.count("|") >= 2 and s.strip("| ").strip()):
            return True
    return False


def missing_required(text, transaction, property_kind=None) -> list:
    """ドラフト本文で未充足（☐のまま/不在）の種別必須ラベル一覧。"""
    return [lb for lb in required_labels(transaction, property_kind) if not _label_filled(text, lb)]


_SCHEMA_MARKER = re.compile(r"<!--\s*ainote-juusetsu-schema:(\{.*?\})\s*-->")
_UNRESOLVED_VALUES = {
    "", "-", "―", "—", "なし", "不明", "未定", "未確認", "要確認", "確認中",
    "非該当", "該当なし", "対象外", "未記入", "＿＿＿＿", "＿",
}
_NON_APPLICABLE = re.compile(
    r"^(?:非該当|該当なし|対象外)\s*[（(:：]\s*(?:理由\s*[:：]\s*)?(.+?)[）)]?\s*$"
)


def _schema_context(text: str) -> tuple[str, Optional[str], str]:
    """Return the schema key/property kind embedded by the deterministic renderer.

    The marker is part of the stored source, not user-visible output.  Finalization
    fails closed without it because guessing property kind can silently omit a
    conditional statutory section.
    """
    match = _SCHEMA_MARKER.search(str(text or ""))
    if not match:
        raise ValueError("schema marker missing")
    raw = json.loads(match.group(1))
    key = str(raw.get("schema") or "").strip()
    if key not in ("baibai", "chintai"):
        raise ValueError("unknown schema")
    pk_raw = str(raw.get("property_kind") or "").strip()
    pk = normalize_property_kind(pk_raw) if pk_raw else None
    if pk_raw and pk is None:
        raise ValueError("unknown property kind")
    tx = normalize_transaction(raw.get("transaction"))
    if tx is None or schema_key(tx) != key:
        raise ValueError("transaction missing or inconsistent")
    return key, pk, tx


def _applicable_schema_items(schema_key_value: str,
                             property_kind: Optional[str]) -> list[tuple[str, str]]:
    schema_path = Path(__file__).parent / "templates" / "office" / "juusetsu_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sections = schema.get(schema_key_value)
    if not isinstance(sections, list) or not sections:
        raise ValueError("schema unavailable")
    items: list[tuple[str, str]] = []
    for section in sections:
        if not section_applies(section.get("applies_property_kinds"), property_kind):
            continue
        section_no = str(section.get("no") or "").strip()
        section_items = section.get("items")
        if not section_no or not isinstance(section_items, list) or not section_items:
            raise ValueError("invalid schema section")
        items.extend((section_no, str(item)) for item in section_items)
    if not items or len(items) != len(set(items)):
        raise ValueError("invalid schema items")
    return items


def _item_value(text: str, section_no: str, item: str) -> Optional[str]:
    """Read one exact schema item from renderer markdown; substring matches are forbidden."""
    filled_prefix = f"- {item}：**"
    empty_line = f"- ☐ {item}"
    in_section = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            heading = line[3:].strip()
            in_section = (heading == section_no or heading.startswith(section_no + "　"))
            continue
        if not in_section:
            continue
        if line == empty_line:
            return ""
        if line.startswith(filled_prefix) and line.endswith("**"):
            return line[len(filled_prefix):-2].strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip().strip("*").strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] == item:
                return cells[1]
    return None


def _resolved_value(value: Optional[str]) -> bool:
    if value is None:
        return False
    normalized = re.sub(r"\s+", " ", str(value)).strip().strip("* ")
    if ("☐" in normalized or "＿＿" in normalized or normalized in _UNRESOLVED_VALUES
            or any(marker in normalized for marker in
                   ("未記入", "未確認", "要確認", "確認中", "未定", "不明"))):
        return False
    if normalized.startswith(("非該当", "該当なし", "対象外")):
        match = _NON_APPLICABLE.fullmatch(normalized)
        return bool(match and len(match.group(1).strip(" 　。")) >= 2)
    return True


def schema_completion(text: str) -> dict:
    """Validate every applicable statutory schema item in a deterministic draft."""
    key, property_kind, transaction = _schema_context(text)
    expected = _applicable_schema_items(key, property_kind)
    missing = [f"{section_no} / {item}" for section_no, item in expected
               if not _resolved_value(_item_value(text, section_no, item))]
    checkbox_lines = [
        line.strip() for line in str(text or "").splitlines()
        if "☐" in line and line.strip()
    ]
    return {
        "schema": key,
        "transaction": transaction,
        "property_kind": property_kind or "",
        "expected_count": len(expected),
        "missing": missing,
        "unresolved_checkbox_lines": checkbox_lines,
        "blocked": bool(missing or checkbox_lines),
    }


def finalize_type_gate(draft_text, deal_type="", property_kind="") -> dict:
    """記名確定前の種別必須ゲート（§6）。戻り: {"blocked": bool, "message": str, "missing": [str]}。

    - 実ドラフトが無い（プレースホルダ）＝確定不可。
    - transaction 判定不能＝種別未設定で block（fail-closed）。
    - 正式フォーム様式だけを受理し、調査途中skeletonや未知形式は確定不可。
    - 種別必須ラベルの欠落は要確認一覧を返す。
    """
    text = str(draft_text or "")
    if _is_placeholder(text):
        return {"blocked": True,
                "message": "記名できる実データの重要事項説明書がありません。正式様式の下書きを作成してください。",
                "missing": []}
    tx = normalize_transaction(deal_type) or transaction_from_draft(text)
    pk = normalize_property_kind(property_kind)
    if tx is None:
        return {"blocked": True,
                "message": "取引種別（売買／賃貸）が未設定です。種別を確定してから記名してください。",
                "missing": []}
    if not _is_form_draft(text):
        return {"blocked": True,
                "message": "この下書きは正式な重要事項説明書様式ではないため記名確定できません。"
                           "調査スケルトンと要確認事項を正式様式へ反映してください。",
                "missing": []}
    try:
        completion = schema_completion(text)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"blocked": True,
                "message": "この下書きの法定schema識別情報を確認できません。最新の正式様式で作り直してください。",
                "missing": []}
    expected_key = schema_key(tx)
    if completion["schema"] != expected_key:
        return {"blocked": True,
                "message": "本文の取引種別と法定schemaが一致しないため記名確定できません。",
                "missing": []}
    if property_kind and completion["property_kind"] != (pk or ""):
        return {"blocked": True,
                "message": "本文の物件種別と確定対象の物件種別が一致しません。",
                "missing": []}
    missing = completion["missing"]
    if missing or completion["unresolved_checkbox_lines"]:
        shown = missing[:8]
        suffix = f"（ほか{len(missing) - len(shown)}項目）" if len(missing) > len(shown) else ""
        return {"blocked": True,
                "message": "適用される法定項目が未充足です。各項目に確認済みの値、または非該当理由を記入してください: "
                           + " / ".join(shown) + suffix,
                "missing": missing}
    return {"blocked": False, "message": "", "missing": [],
            "schema": completion["schema"], "checked": completion["expected_count"]}
