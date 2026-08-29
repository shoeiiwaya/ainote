"""hub_core/files.py — Obsidian式のローカルフォルダ書類/写真管理。

受領ファイル(物件写真・内見写真・収入証明・本人確認書類 等)を
  <data_dir>/files/<scope>/<entity_id>/   (scope = case | customer | property)
に置く。ユーザーは Finder で直接フォルダにドロップでき、OS はそれを読み・サムネイル表示し・
UIからアップロードもできる。DBに閉じ込めず『ファイルが正本』(documents.py と同思想)。
収入証明等はPIIなので、業務データ全体をファイル同期サービスへ置く運用はサポートしない。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCOPES = ("case", "customer", "property")
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp"}
WEB_PHOTO = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}  # ブラウザで直表示できる
DOC_EXT = {".pdf", ".docx", ".xlsx", ".doc", ".xls", ".txt", ".csv", ".pptx"}
MAX_UPLOAD = 25 * 1024 * 1024  # 1ファイル25MB上限

# ファイル名から書類種別を推定(書類チェックリストとの突合用)。
_KIND_HINTS = [
    ("収入証明", ("収入", "源泉", "給与", "課税", "所得", "income")),
    ("本人確認", ("本人確認", "免許", "マイナ", "パスポート", "在留", "保険証", "id")),
    ("内見写真", ("内見", "現地", "viewing")),
    ("物件写真", ("外観", "間取", "室内", "madori", "photo", "img", "dsc")),
    ("申込書", ("申込", "moushikomi", "application")),
    ("契約書", ("契約", "keiyaku", "contract")),
    ("重要事項説明書", ("重説", "重要事項", "juusetsu")),
]


def _safe_name(name: str) -> str:
    """ファイル名をパストラバーサル不能・安全な basename に。日本語は保持。"""
    base = Path(str(name or "")).name
    base = re.sub(r'[\\/\x00-\x1f]', "_", base).strip().lstrip(".")
    return base[:120] or "file"


def _safe_id(entity_id: str) -> str:
    s = re.sub(r'[\\/\x00-\x1f]', "_", str(entity_id or "")).strip()
    s = s[:120]
    # ドットのみのID（"."/".."）は親ディレクトリ脱出になるため拒否（path traversal封じ）
    if not s or set(s) <= {"."}:
        return "_"
    return s


def files_dir(data_dir, scope: str, entity_id: str) -> Path:
    if scope not in SCOPES:
        raise ValueError(f"scope は {SCOPES} のいずれか: {scope}")
    base = (Path(data_dir) / "files" / scope).resolve()
    d = (base / _safe_id(entity_id)).resolve()
    # 封じ込め検証: いかなる入力でも <data_dir>/files/<scope>/ の外へ出ない（fail-closed）
    if d != base and base not in d.parents:
        raise ValueError(f"entity_id が不正です: {entity_id!r}")
    if d == base:
        raise ValueError(f"entity_id が不正です（空/ドットのみ）: {entity_id!r}")
    return d


def kind_hint(filename: str) -> str:
    low = str(filename or "").lower()
    for label, keys in _KIND_HINTS:
        if any(k.lower() in low for k in keys):
            return label
    return ""


def _meta(p: Path) -> dict:
    ext = p.suffix.lower()
    kind = "photo" if ext in PHOTO_EXT else ("doc" if ext in DOC_EXT else "other")
    sz = p.stat().st_size
    mt = datetime.fromtimestamp(p.stat().st_mtime, timezone(timedelta(hours=9)))
    return {"name": p.name, "ext": ext, "kind": kind,
            "web_viewable": ext in WEB_PHOTO,
            "size": sz, "size_h": _human(sz),
            "mtime": mt.strftime("%Y-%m-%d %H:%M"),
            "doc_hint": kind_hint(p.name)}


def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def list_files(data_dir, scope: str, entity_id: str) -> list:
    """フォルダ内のファイルを一覧(写真/書類/その他)。隠しファイルは除外。新しい順。"""
    d = files_dir(data_dir, scope, entity_id)
    if not d.exists():
        return []
    items = [_meta(p) for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
    items.sort(key=lambda m: m["mtime"], reverse=True)
    return items


def save_upload(data_dir, scope: str, entity_id: str, filename: str, data: bytes) -> str:
    """アップロードを保存。サイズ上限・拡張子許可・上書き回避(連番)。保存名を返す。"""
    if not data:
        raise ValueError("空のファイルです。")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"ファイルが大きすぎます(上限 {MAX_UPLOAD // (1024*1024)}MB)。")
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in PHOTO_EXT and ext not in DOC_EXT:
        raise ValueError("対応していない形式です(写真/PDF/Office のみ)。")
    d = files_dir(data_dir, scope, entity_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    i = 1
    while p.exists():
        p = d / f"{Path(name).stem}_{i}{ext}"
        i += 1
    p.write_bytes(data)
    return p.name


def read_file(data_dir, scope: str, entity_id: str, name: str):
    """ファイルのバイト列を返す(パストラバーサル防止・フォルダ外は拒否)。無ければ None。"""
    d = files_dir(data_dir, scope, entity_id).resolve()
    p = (d / _safe_name(name)).resolve()
    if not str(p).startswith(str(d) + "/") and p != d:
        return None
    if not p.is_file():
        return None
    return p.read_bytes()


def counts(data_dir, scope: str, entity_id: str) -> dict:
    items = list_files(data_dir, scope, entity_id)
    return {"total": len(items),
            "photo": sum(1 for i in items if i["kind"] == "photo"),
            "doc": sum(1 for i in items if i["kind"] == "doc")}
