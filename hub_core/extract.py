"""M-extract — 書類からの構造化抽出の骨格（Wave1 V2）。

思想（サーバはLLMを呼ばない原則の維持）:
- 抽出の実行者 = AIクライアント（Claude/MCP経由の operate）または人間（手動入力）。
- サーバの責務 = **検証・出典束縛・承認ゲート・改竄検知**。
  1書類1つの `<原本>.extract.json` を原本の隣（Vault 調査/・素材/）に置く＝ファイルが正本
  （hub.db 非依存＝再構築で自然に生き残る）。
- 各抽出値は必ず出典を持つ: page（1始まり・**自己申告値**＝サーバはPDFを開かない設計のため
  頁実在の照合は承認者=宅建士の責務）＋任意の bbox（正規化 0..1 の [x0,y0,x1,y1]）。
  ＝機械保証は「原本sha256束縛＋出典欄の強制」であり「頁内容の真実性」ではない（過大主張しない）。
- source_sha256 で原本に内容束縛: 原本が差し替えられたら抽出は**失効**（tampered）と表示され、
  承認も拒否される（fail-closed）。
- 承認（extraction_approve）は宅建士/責任者/代表のみ・HMAC監査。承認前の値は draft 表示のみ。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
SUFFIX = ".extract.json"
_ALLOWED_PARENTS = ("調査", "素材", "許諾")


class ExtractError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_source(data_dir, source_rel: str) -> Path:
    """Vault内の原本パスへ解決（トラバーサル拒否・許可親フォルダのみ）。"""
    root = Path(data_dir).resolve()
    p = (root / source_rel).resolve()
    if not p.is_relative_to(root):   # startswithはprefix-sibling迂回可(vault_evil)→is_relative_toで厳密封じ込め
        raise ExtractError(400, f"Vault外のパスは指定できません: {source_rel}")
    if not p.is_file():
        raise ExtractError(404, f"原本が見つかりません: {source_rel}")
    if p.name.endswith(SUFFIX):
        raise ExtractError(400, "extract.json 自体は原本にできません。")
    if not any(seg in _ALLOWED_PARENTS for seg in p.parent.parts):
        raise ExtractError(400, f"原本は Vault の {'/'.join(_ALLOWED_PARENTS)} 配下のみ: {source_rel}")
    return p


def _validate_fields(fields) -> list[dict]:
    if not isinstance(fields, list) or not fields:
        raise ExtractError(400, "fields は1件以上のリストが必要です。")
    out = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise ExtractError(400, f"fields[{i}] がdictでない。")
        key = str(f.get("key") or "").strip()
        value = str(f.get("value") or "").strip()
        if not key or not value:
            raise ExtractError(400, f"fields[{i}]: key と value は必須。")
        page = f.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= 9999):
            raise ExtractError(400, f"fields[{i}] ({key}): page は1〜9999の整数必須（出典なしの値は保存できない）。")
        bbox = f.get("bbox")
        if bbox is not None:
            ok = (isinstance(bbox, (list, tuple)) and len(bbox) == 4
                  and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bbox)
                  and bbox[0] < bbox[2] and bbox[1] < bbox[3])
            if not ok:
                raise ExtractError(400, f"fields[{i}] ({key}): bbox は正規化 [x0,y0,x1,y1]（0..1・x0<x2,y0<y3）。")
        out.append({"key": key, "value": value, "page": page,
                    "bbox": list(bbox) if bbox is not None else None})
    return out


def save_extraction(data_dir, source_rel: str, fields, actor: str,
                    extractor: str = "manual") -> dict:
    """抽出値を原本の隣へ draft 保存（AIクライアント/手動の共通入口）。"""
    src = _resolve_source(data_dir, source_rel)
    rec = {
        "source": source_rel,
        "source_sha256": _sha256(src),
        "extractor": extractor,          # manual / ai:<model> 等（自己申告・監査に残る）
        "status": "draft",
        "fields": _validate_fields(fields),
        "saved_by": actor,
        "saved_at": _now(),
        "approved_by": "",
        "approved_at": "",
    }
    out = src.with_name(src.name + SUFFIX)
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(out.relative_to(Path(data_dir).resolve())), "fields": len(rec["fields"])}


def approve_extraction(data_dir, source_rel: str, actor: str) -> dict:
    """人間承認（宅建士等・RBACは operations 層）。原本sha256一致が前提=改竄はfail-closed。"""
    src = _resolve_source(data_dir, source_rel)
    ext = src.with_name(src.name + SUFFIX)
    if not ext.is_file():
        raise ExtractError(404, f"抽出がありません: {source_rel}{SUFFIX}")
    rec = json.loads(ext.read_text(encoding="utf-8"))
    cur = _sha256(src)
    if cur != rec.get("source_sha256"):
        raise ExtractError(409, "原本が抽出時から変更されています（sha256不一致）。抽出をやり直してください。")
    rec["status"] = "approved"
    rec["approved_by"] = actor
    rec["approved_at"] = _now()
    ext.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(ext), "fields": len(rec.get("fields", []))}


def load_extractions(data_dir, property_name: str | None = None) -> list[dict]:
    """Vault走査で全 extract.json を返す。原本sha256を毎回照合し、不一致は status=tampered
    に**表示上**倒す（ファイルは書き換えない・証拠保全）。"""
    root = Path(data_dir).resolve()
    base = root / "物件"
    if not base.is_dir():
        return []
    out = []
    for ext in sorted(base.rglob("*" + SUFFIX)):
        try:
            rec = json.loads(ext.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            prop = ext.relative_to(base).parts[0]
        except (ValueError, IndexError):
            prop = ""
        if property_name and prop != property_name:
            continue
        src = ext.with_name(ext.name[: -len(SUFFIX)])
        if not src.is_file() or _sha256(src) != rec.get("source_sha256"):
            rec = dict(rec, status="tampered")
        rec["property"] = prop
        rec["_path"] = str(ext.relative_to(root))
        out.append(rec)
    return out
