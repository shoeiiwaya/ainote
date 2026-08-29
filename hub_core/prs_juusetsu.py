"""PRS `juusetsu-hazard-v1` を重説下書きへ接続するfail-closed境界。

スコアAPI `/v2/assess`、A33/A40/A54等の参考レイヤー、ハザードポータル図版は
法定根拠へ流用しない。契約・status・一次原典provenanceを検証できた行だけを既存
35条schemaの空欄へ差し込み、その他は空欄と要確認を維持する。
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windowsはprocess内lock。JSONLは1行append＋fsyncを維持する。
    fcntl = None

CONTRACT = "juusetsu-hazard-v1"
MAX_RESPONSE_BYTES = 1024 * 1024
JST = timezone(timedelta(hours=9))
_RECEIPT_LOCK = threading.Lock()

_ITEMS = (
    ("landslide", "土砂災害警戒区域"),
    ("tsunami", "津波災害警戒区域"),
    ("developed_land", "造成宅地防災区域"),
    ("flood_map", "水害ハザードマップ（洪水）"),
    ("inland_flood_map", "水害ハザードマップ（内水）"),
    ("storm_surge_map", "水害ハザードマップ（高潮）"),
)

_ALLOWED_STATUS = {
    "landslide": {"CONFIRMED_IN", "CONFIRMED_OUT", "NEEDS_PRIMARY_SOURCE"},
    "tsunami": {"CONFIRMED_IN", "CONFIRMED_OUT", "NOT_DESIGNATED",
                "NEEDS_PRIMARY_SOURCE"},
    "developed_land": {"CONFIRMED_IN", "CONFIRMED_OUT", "NOT_DESIGNATED",
                       "NEEDS_PRIMARY_SOURCE"},
    "flood_map": {"MAP_AVAILABLE", "MAP_NOT_AVAILABLE", "NEEDS_PRIMARY_SOURCE"},
    "inland_flood_map": {"MAP_AVAILABLE", "MAP_NOT_AVAILABLE", "NEEDS_PRIMARY_SOURCE"},
    "storm_surge_map": {"MAP_AVAILABLE", "MAP_NOT_AVAILABLE", "NEEDS_PRIMARY_SOURCE"},
}

_BASIS_TOKENS = {
    "landslide": ("16条の4の3第2号", "7条第1項"),
    "tsunami": ("16条の4の3第3号", "53条第1項"),
    "developed_land": ("16条の4の3第1号", "45条第1項"),
    "flood_map": ("16条の4の3第3号の2", "11条第1号"),
    "inland_flood_map": ("16条の4の3第3号の2", "11条第1号"),
    "storm_surge_map": ("16条の4の3第3号の2", "11条第1号"),
}

_FORBIDDEN_SOURCE = {
    "landslide": re.compile(r"(?:国土数値情報\s*)?A33", re.I),
    "tsunami": re.compile(r"(?:国土数値情報\s*)?A40|津波浸水想定", re.I),
    "developed_land": re.compile(r"A54|XKT0?20|大規模盛土造成地", re.I),
    "flood_map": re.compile(r"A31|洪水浸水想定区域|ハザードマップポータル|重ねるハザード", re.I),
    "inland_flood_map": re.compile(r"A51|雨水出水浸水想定区域|ハザードマップポータル|重ねるハザード", re.I),
    "storm_surge_map": re.compile(r"A49|高潮浸水想定区域|ハザードマップポータル|重ねるハザード", re.I),
}

_SCHEMA_ITEMS = {
    "landslide": (
        "土砂災害防止対策推進法に基づく土砂災害警戒区域（イエローゾーン）の区域内／区域外の別",
        "土砂災害防止対策推進法に基づく土砂災害警戒区域の区域内／区域外の別・内容",
    ),
    "tsunami": (
        "津波防災地域づくりに関する法律に基づく津波災害警戒区域の区域内／区域外・未指定の別",
        "津波防災地域づくりに関する法律に基づく津波災害警戒区域の区域内／区域外・未指定の別・内容",
    ),
    "developed_land": (
        "宅地造成及び特定盛土等規制法（盛土規制法）に基づく造成宅地防災区域の区域内／区域外の別",
        "宅地造成及び特定盛土等規制法（盛土規制法）に基づく造成宅地防災区域の区域内／区域外の別・内容",
    ),
    "flood_map": ("洪水ハザードマップの有無（図面名称・照会先）",),
    "inland_flood_map": ("雨水出水（内水）ハザードマップの有無（図面名称・照会先）",),
    "storm_surge_map": ("高潮ハザードマップの有無（図面名称・照会先）",),
}


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def _safe_text(value, *, limit: int = 2048) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text or len(text) > limit or any(ord(ch) < 32 and ch not in "\t" for ch in text):
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)


def _basis_key(value: str) -> str:
    return re.sub(r"[\s　]", "", unicodedata.normalize("NFKC", value))


def _digest_valid(value: str) -> bool:
    return bool(re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", str(value or "").strip()))


def _url_valid(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return False
    return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.fragment)


def _unresolved_row(key: str, label: str, reason: str = "") -> dict:
    return {
        "key": key, "label": label, "status": "NEEDS_PRIMARY_SOURCE",
        "legal_basis": "", "primary_source": "", "source_version": "",
        "checked_at": "", "document_digest": "", "verification_url": "",
        "reason": _safe_text(reason, limit=500),
    }


def unavailable_contract(address: str, reason: str) -> dict:
    return {
        "contract": CONTRACT,
        "connected": False,
        "receipt_id": "",
        "property_address": _safe_text(address, limit=500),
        "received_at": _now(),
        "response_sha256": "",
        "reason": _safe_text(reason, limit=500),
        "items": [_unresolved_row(key, label, reason) for key, label in _ITEMS],
    }


def normalize_contract(payload, *, address: str, response_sha256: str = "") -> dict:
    if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
        return unavailable_contract(address, "PRS応答の契約版を確認できません。")
    raw_items = payload.get("items")
    if not isinstance(raw_items, dict):
        return unavailable_contract(address, "PRS応答に6項目の調査結果がありません。")
    normalized = []
    for key, label in _ITEMS:
        raw = raw_items.get(key)
        if not isinstance(raw, dict):
            normalized.append(_unresolved_row(key, label, "項目がありません。"))
            continue
        status = _safe_text(raw.get("status"), limit=80).upper()
        basis = _safe_text(raw.get("legal_basis"))
        source = _safe_text(raw.get("primary_source"))
        version = _safe_text(raw.get("source_version"), limit=500)
        checked = _safe_text(raw.get("checked_at"), limit=80)
        digest = _safe_text(raw.get("document_digest"), limit=100)
        verify_url = _safe_text(raw.get("verification_url"))
        basis_key = _basis_key(basis)
        provenance_ok = bool(
            status in _ALLOWED_STATUS[key]
            and status != "NEEDS_PRIMARY_SOURCE"
            and all(token in basis_key for token in _BASIS_TOKENS[key])
            and source and version
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked)
            and _digest_valid(digest)
            and _url_valid(verify_url)
            and not _FORBIDDEN_SOURCE[key].search(source)
        )
        if not provenance_ok:
            normalized.append(_unresolved_row(
                key, label, "一次原典・根拠条項・版・確認日・digest・確認先を検証できません。"))
            continue
        normalized.append({
            "key": key, "label": label, "status": status,
            "legal_basis": basis, "primary_source": source,
            "source_version": version, "checked_at": checked,
            "document_digest": digest.lower(), "verification_url": verify_url,
            "reason": "",
        })
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {
        "contract": CONTRACT,
        "connected": True,
        "receipt_id": _safe_text(payload.get("receipt_id"), limit=200),
        "property_address": _safe_text(address, limit=500),
        "received_at": _now(),
        "response_sha256": response_sha256 or hashlib.sha256(canonical).hexdigest(),
        "reason": "",
        "items": normalized,
    }


def _configured_url() -> str:
    value = str(os.environ.get("PRS_JUUSETSU_HAZARD_URL") or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        return ""
    if parsed.scheme == "https":
        return value
    if parsed.scheme != "http":
        return ""
    host = parsed.hostname
    if host == "localhost":
        return value
    try:
        return value if ipaddress.ip_address(host).is_loopback else ""
    except ValueError:
        return ""


def configured() -> bool:
    return bool(_configured_url())


def _receipt_path(data_dir) -> Path:
    return Path(data_dir) / "prs_juusetsu_receipts.jsonl"


def _append_receipt(data_dir, receipt: dict) -> None:
    path = _receipt_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    payload = (json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    with _RECEIPT_LOCK:
        lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def load_last_receipt(data_dir) -> dict:
    path = _receipt_path(data_dir)
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return {}
        value = json.loads(lines[-1])
        return value if isinstance(value, dict) and value.get("contract") == CONTRACT else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def fetch_for_draft(data_dir, address: str) -> dict:
    clean_address = _safe_text(address, limit=500)
    url = _configured_url()
    if not clean_address:
        return unavailable_contract(address, "物件住所が未入力です。")
    if not url:
        return unavailable_contract(clean_address, "PRS重説調査が未接続です。")
    body = json.dumps({"contract": CONTRACT, "property_address": clean_address},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "X-PRS-Contract": CONTRACT, "User-Agent": "ainote-local-web/juusetsu"}
    key = str(os.environ.get("PRS_API_KEY") or os.environ.get("RISK_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
        headers["X-API-Key"] = key
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        timeout = float(os.environ.get("PRS_TIMEOUT_SECONDS", "15"))
    except ValueError:
        timeout = 15.0
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, min(timeout, 60.0))) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        payload = json.loads(raw.decode("utf-8"))
        receipt = normalize_contract(
            payload, address=clean_address,
            response_sha256=hashlib.sha256(raw).hexdigest())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return unavailable_contract(clean_address, f"PRS重説調査を取得できません: {type(exc).__name__}")
    _append_receipt(data_dir, receipt)
    return receipt


def _status_text(row: dict) -> str:
    return {
        "CONFIRMED_IN": "区域内",
        "CONFIRMED_OUT": "区域外",
        "NOT_DESIGNATED": "未指定",
        "MAP_AVAILABLE": "有",
        "MAP_NOT_AVAILABLE": "無",
    }.get(str(row.get("status") or ""), "【要確認】")


def _row_value(row: dict) -> str:
    if row.get("status") == "NEEDS_PRIMARY_SOURCE":
        return ""
    return (f"{_status_text(row)}／原典: {row['primary_source']}／版: {row['source_version']}／"
            f"確認日: {row['checked_at']}／document digest: {row['document_digest']}／"
            f"確認先: {row['verification_url']}")


def render_draft_section(receipt: dict) -> str:
    lines = ["", "## PRS重説ハザード調査（下書き支援）", "",
             f"> contract: {CONTRACT}／receipt: {receipt.get('receipt_id') or '未取得'}。"
             "APIは下書き支援であり、宅地建物取引士の原典確認・ログイン本人確認・記名確定を代替しません。",
             ""]
    for row in receipt.get("items") or []:
        status = _status_text(row)
        lines.append(f"- {row.get('label', '')}：**status {status}**")
        if row.get("status") == "NEEDS_PRIMARY_SOURCE":
            lines.append("  - 根拠条項：☐ 要確認")
            lines.append("  - 都道府県・市町村原典：☐ 要確認")
            lines.append("  - 版／確認日／document digest／確認先：☐ 要確認")
        else:
            lines.append(f"  - 根拠条項：{row.get('legal_basis', '')}")
            lines.append(f"  - 都道府県・市町村原典：{row.get('primary_source', '')}")
            lines.append(f"  - 版：{row.get('source_version', '')}／確認日：{row.get('checked_at', '')}")
            lines.append(f"  - document digest：{row.get('document_digest', '')}")
            lines.append(f"  - 確認先：{row.get('verification_url', '')}")
    return "\n".join(lines)


def apply_to_draft(markdown: str, receipt: dict) -> str:
    """検証済み行だけ既存schemaの空欄へ差し込み、全6行receiptを末尾へ残す。"""
    text = str(markdown or "")
    by_key = {row.get("key"): row for row in receipt.get("items") or []}
    for key, candidates in _SCHEMA_ITEMS.items():
        row = by_key.get(key) or {}
        value = _row_value(row)
        if not value:
            continue
        for item in candidates:
            text = text.replace(f"- ☐ {item}", f"- {item}：**{value}**")
    return text.rstrip() + "\n" + render_draft_section(receipt) + "\n"
