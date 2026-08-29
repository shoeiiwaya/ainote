"""あいのて 書類ストア (Phase1b)。書類 = ファイル正本（Obsidian式）+ 派生インデックス。

設計書 分岐5=A:
- 書類は `data_dir/documents/<doc_id>/` 配下のファイルを **正本** とする。
  1書類1ディレクトリ・1版1ファイル(`v{N}.{ext}`) + `v{N}.json`(メタ: content_sha256/author/...)
  + `meta.json`(doc 全体: kind/latest)。
- **hub.db に依存しない**。ただし業務データ全体にはSQLite正本もあるため、`data_dir` を
  iCloud/Google Drive等で同期する複数端末運用はサポートしない。
- 各版は `content_sha256` で本文を束縛。記名確定(`chat_bridge.finalize`)はこの hash に署名する。
- 版は **append-only**（過去版を破壊しない）。"latest" ポインタの付替えのみ。
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - initial public target is macOS
    fcntl = None

_JST = timezone(timedelta(hours=9))
# 拡張子の正規化（許可フォーマットのみ）
# 注意: 版メタを v{n}.json に書くため "json" 拡張子は本文と衝突する→使わない。
# 構造化データ(マイソク様式フィールド等)は fmt="txt" に JSON 文字列で格納し kind で判別する。
_FMT_EXT = {"md": "md", "markdown": "md", "html": "html", "htm": "html",
            "txt": "txt", "text": "txt", "ics": "ics"}
# 案件画面で別々の業務lifecycleとして扱う4帳票。保存時の既存kind
# ``juusetsu`` は35条書面としてだけ正規化し、売買条件確認票と37条書面へは流用しない。
FOUR_DOCUMENT_KINDS = (
    "juusetsu35",
    "sale_condition_check",
    "article37",
    "maisoku",
)
_FOUR_DOCUMENT_KIND_ALIASES = {
    "juusetsu": "juusetsu35",
    "juusetsu35": "juusetsu35",
    "sale_condition_check": "sale_condition_check",
    "article37": "article37",
    "maisoku": "maisoku",
}
FOUR_DOCUMENT_OUTPUT_FORMATS = {
    "juusetsu35": frozenset({"docx", "pdf"}),
    "sale_condition_check": frozenset({"docx", "pdf"}),
    "article37": frozenset({"docx", "pdf"}),
    "maisoku": frozenset({"xlsx", "pdf"}),
}
# doc_id のサニタイズ: パス区切り・制御文字・各OSで問題になる文字のみ '_' 化。
# 日本語等は許可(読める書類名にする)。'.'/'..'/空 は別途拒否(下記 _safe_doc_id)。
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_DOCUMENT_LOCK = threading.RLock()
_DOCUMENT_LOCAL = threading.local()


class DocError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(_JST).replace(microsecond=0).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_doc_id(doc_id: str) -> str:
    raw = (doc_id or "").strip()
    safe = _UNSAFE.sub("_", raw)
    if not safe or safe in (".", "..") or set(safe) == {"_"}:
        raise DocError(400, f"無効な doc_id です: {doc_id!r}")
    return safe


def canonical_four_document_kind(kind: str) -> str | None:
    """Return the case-workspace kind without merging distinct legal documents."""
    return _FOUR_DOCUMENT_KIND_ALIASES.get(str(kind or "").strip().lower())


def four_document_output_formats(kind: str) -> frozenset[str]:
    """Return the exact output allowlist for one canonical/legacy four-kind name."""
    canonical = canonical_four_document_kind(kind)
    return FOUR_DOCUMENT_OUTPUT_FORMATS.get(canonical, frozenset())


def _docs_root(data_dir) -> Path:
    return Path(data_dir) / "documents"


def _doc_dir(data_dir, doc_id: str) -> Path:
    return _docs_root(data_dir) / _safe_doc_id(doc_id)


@contextlib.contextmanager
def document_transaction(data_dir, doc_id: str):
    """Serialize a document append and its audit commit across threads/processes."""
    root = _docs_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    key = str(root.resolve())
    depths = getattr(_DOCUMENT_LOCAL, "depths", {})
    with _DOCUMENT_LOCK:
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        _DOCUMENT_LOCAL.depths = depths
        handle = None
        try:
            if depth == 0:
                handle = (root / ".documents.lock").open("a+")
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if depth == 0 and handle is not None:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            if depth:
                depths[key] = depth
            else:
                depths.pop(key, None)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Commit one file in-place without exposing a truncated target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _write_json(p: Path, obj: dict) -> None:
    _atomic_write_text(p, json.dumps(obj, ensure_ascii=False, indent=1))


def latest_version(data_dir, doc_id: str) -> int:
    """最新版番号。書類が無ければ 0。ファイル走査で求める（インデックス非依存=同期後も正しい）。"""
    d = _doc_dir(data_dir, doc_id)
    if not d.exists():
        return 0
    vs = []
    for f in d.glob("v*.json"):
        m = re.fullmatch(r"v(\d+)\.json", f.name)
        if m:
            vs.append(int(m.group(1)))
    return max(vs) if vs else 0


def save_version(data_dir, doc_id: str, body: str, *, kind: str = "",
                 fmt: str = "md", author: str = "", sample: bool = False,
                 case_id: str = "", customer_id: str = "",
                 company_profile_hash: str | None = None) -> dict:
    """新しい版を書き込む（append-only）。返り値に version/content_sha256/path。"""
    if body is None:
        raise DocError(400, "本文(body)が必要です。")
    ext = _FMT_EXT.get((fmt or "md").lower())
    if ext is None:
        raise DocError(400, f"未対応フォーマットです: {fmt}（md/html/txt）")
    with document_transaction(data_dir, doc_id):
        d = _doc_dir(data_dir, doc_id)
        d.mkdir(parents=True, exist_ok=True)
        doc_meta_path = d / "meta.json"
        meta = _read_json(doc_meta_path)
        previous_case = str(meta.get("case_id") or "").strip()
        requested_case = str(case_id or "").strip()
        if previous_case and requested_case and previous_case != requested_case:
            raise DocError(
                409,
                f"書類 {doc_id} は案件 {previous_case} に紐付いているため、"
                f"案件 {requested_case} へ付け替えられません。",
            )
        bound_case = requested_case or previous_case
        previous_customer = str(meta.get("customer_id") or "").strip()
        requested_customer = str(customer_id or "").strip()
        if previous_customer and requested_customer and previous_customer != requested_customer:
            raise DocError(
                409,
                f"書類 {doc_id} は顧客 {previous_customer} に紐付いているため、"
                f"顧客 {requested_customer} へ付け替えられません。",
            )
        bound_customer = requested_customer or previous_customer
        version = latest_version(data_dir, doc_id) + 1
        body_str = str(body)
        chash = _sha256(body_str)
        body_path = d / f"v{version}.{ext}"
        version_meta_path = d / f"v{version}.json"
        # 新規版は通常、現在の会社プロファイルを刻む。既存書類の編集では呼び手が旧版の
        # hash を明示し、本文と会社表示の時間軸を勝手に現在へ進めない。
        from hub_core import branding as _br
        if company_profile_hash is None:
            try:
                _ph = _br.snapshot_profile(data_dir)
            except Exception:      # メモ等の下書き保存は会社情報が無くても許容する
                _ph = ""
        else:
            _ph = str(company_profile_hash or "").strip().lower()
            if _ph:
                if not re.fullmatch(r"[0-9a-f]{64}", _ph):
                    raise DocError(400, "会社プロファイル参照の形式が不正です。")
                profile = _br.load_snapshot(data_dir, _ph)
                if not profile or _br.profile_hash(profile) != _ph:
                    raise DocError(409, "保存時の会社プロファイルを確認できません。")
        vmeta = {
            "doc_id": _safe_doc_id(doc_id), "version": version, "kind": kind, "fmt": ext,
            "content_sha256": chash, "author": author, "created": _now(),
            "bytes": len(body_str.encode("utf-8")), "company_profile_hash": _ph,
            "sample": bool(sample), "case_id": bound_case,
            "customer_id": bound_customer,
        }
        try:
            _atomic_write_text(body_path, body_str)
            _write_json(version_meta_path, vmeta)
            # doc 全体メタ（latest ポインタ）。kind は初回 or 明示時に更新。
            meta.update({"doc_id": _safe_doc_id(doc_id), "latest": version, "updated": _now()})
            if bound_case:
                meta["case_id"] = bound_case
            if bound_customer:
                meta["customer_id"] = bound_customer
            if kind:
                meta["kind"] = kind
            elif "kind" not in meta:
                meta["kind"] = ""
            # 一度でも実データとして保存された書類を、古い見本版だけを理由に見本扱いし続けない。
            meta["sample"] = bool(sample)
            _write_json(d / "meta.json", meta)
        except Exception:
            version_meta_path.unlink(missing_ok=True)
            body_path.unlink(missing_ok=True)
            raise
        return {"doc_id": _safe_doc_id(doc_id), "version": version, "content_sha256": chash,
                "kind": meta.get("kind", ""), "fmt": ext, "path": str(body_path),
                "company_profile_hash": _ph, "case_id": bound_case,
                "customer_id": bound_customer}


def signature_body(body: str, name: str, registration_no: str) -> str:
    """Return the issued body with its draft notices converted and signature stamped."""
    issued = str(body)
    final_notice = (
        "> 【確定版】本書は宅地建物取引士が全適用項目を確認し、記名確定した重要事項説明書です。"
        "交付時は監査台帳の書類ID・版・本文ハッシュ・案件IDとの一致を確認します。"
    )
    issued, notice_count = re.subn(
        r"^>[^\n]*(?:下書き|交付不可)[^\n]*$", final_notice, issued,
        count=1, flags=re.MULTILINE,
    )
    if final_notice in issued:
        notice_count = 1
    if not notice_count:
        lines = issued.splitlines()
        insert_at = 1
        if len(lines) > 1 and lines[1].startswith("<!-- ainote-juusetsu-schema:"):
            insert_at = 2
        lines.insert(insert_at, final_notice)
        issued = "\n".join(lines)
    issued = re.sub(
        r"^-\s*☐\s*交付前に「記名確定」で確定[^\n]*$",
        "- 全適用項目確認済み・記名確定済み（監査台帳へ記録）",
        issued, count=1, flags=re.MULTILINE,
    )
    line = f"- 宅地建物取引士（記名）: {name}　登録番号: {registration_no}"
    replaced, count = re.subn(r"- 宅地建物取引士（記名）:[^\n]*", line, issued, count=1)
    if count:
        return replaced
    return issued.rstrip("\n") + "\n\n" + line + "\n"


def require_finalized_version(data_dir, doc_id: str, version: int | None = None,
                              *, require_case: bool = True) -> dict:
    """Return an exact finalized重説 version and its verified audit event.

    A matching content hash alone is insufficient: document id, version, case and
    company-profile snapshot must all describe the same append-only version.  The
    audit HMAC chain and current statutory-schema completion are rechecked at output
    time so a legacy/stale finalization event cannot authorize customer delivery.
    """
    cur = get_version(data_dir, doc_id, version)
    meta = cur.get("meta") or {}
    if str(meta.get("kind") or "") != "juusetsu":
        raise DocError(403, "重要事項説明書として保存された版ではありません。")
    bound_case = str(meta.get("case_id") or "").strip()
    if require_case and not bound_case:
        raise DocError(409, "この重説は案件に紐付いていないため、顧客向け確定出力にできません。")

    try:
        from hub_core.audit import AuditChainError, verify_audit_chain
        log = Path(data_dir) / "audit_log.jsonl"
        broken = verify_audit_chain(log)
        if broken:
            raise AuditChainError(f"broken audit entries: {broken}")
        events = []
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("audit event is not an object")
                    events.append(event)
    except Exception as exc:  # audit verification failure must never become publishable
        raise DocError(409, "監査台帳を安全に検証できないため、確定版を出力できません。") from exc

    target = f"{meta.get('doc_id') or _safe_doc_id(doc_id)}#v{int(meta.get('version') or 0)}"
    digest = str(meta.get("content_sha256") or "")
    profile_hash = str(meta.get("company_profile_hash") or "")
    match = None
    for event in events:
        if (event.get("action") == "finalized_with_signature"
                and str(event.get("target") or "") == target
                and hmac.compare_digest(str(event.get("content_hash") or ""), digest)
                and str(event.get("case_id") or "").strip() == bound_case
                and str(event.get("company_profile_hash") or "") == profile_hash):
            match = event
    if match is None:
        raise DocError(403, "この書類ID・版・本文ハッシュ・案件に一致する記名確定記録がありません。")

    signer = str(match.get("takkenshi_name") or "").strip()
    registration = str(match.get("registration_no") or match.get("license_no") or "").strip()
    signature_line = f"宅地建物取引士（記名）: {signer}　登録番号: {registration}"
    if not signer or not registration or signature_line not in cur["body"]:
        raise DocError(409, "記名確定記録と書面の宅地建物取引士署名が一致しません。")
    try:
        from hub_core.deal_taxonomy import finalize_type_gate
        gate = finalize_type_gate(cur["body"])
    except Exception as exc:
        raise DocError(409, "法定項目を再検査できないため、確定版を出力できません。") from exc
    if gate.get("blocked"):
        raise DocError(409, gate.get("message") or "法定項目が未充足のため、確定版を出力できません。")
    return {"document": cur, "event": match, "case_id": bound_case,
            "target": target, "content_sha256": digest}


def rollback_uncommitted_version(data_dir, doc_id: str, saved: dict,
                                 previous_meta: bytes | None) -> None:
    """Remove only a just-written version whose audit commit failed."""
    d = _doc_dir(data_dir, doc_id)
    version = int(saved.get("version") or 0)
    meta_path = d / f"v{version}.json"
    version_meta = _read_json(meta_path)
    if (version < 1 or latest_version(data_dir, doc_id) != version
            or version_meta.get("content_sha256") != saved.get("content_sha256")):
        raise DocError(409, "監査失敗後の未確定版を安全に戻せません。書類を保全してください。")
    ext = version_meta.get("fmt") or saved.get("fmt") or "md"
    (d / f"v{version}.{ext}").unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    doc_meta = d / "meta.json"
    if previous_meta is None:
        doc_meta.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(doc_meta, previous_meta)


def get_version_metadata(data_dir, doc_id: str,
                         version: int | None = None) -> dict:
    """Read and validate one version's metadata without opening its body.

    Authorization code must use this function first.  Checking the body hash still
    belongs to :func:`get_version`, but ownership decisions must not require secret
    document bytes to be read before the decision is made.
    """
    d = _doc_dir(data_dir, doc_id)
    v = version if version is not None else latest_version(data_dir, doc_id)
    if (not isinstance(v, int) or isinstance(v, bool) or v < 1
            or not d.is_dir() or d.is_symlink()):
        raise DocError(404, f"書類が見つかりません: {doc_id}")
    doc_meta_path = d / "meta.json"
    version_meta_path = d / f"v{v}.json"
    if (not doc_meta_path.is_file() or doc_meta_path.is_symlink()
            or not version_meta_path.is_file() or version_meta_path.is_symlink()):
        raise DocError(404, f"版が見つかりません: {doc_id} v{v}")
    doc_meta = _read_json(doc_meta_path)
    vmeta = _read_json(version_meta_path)
    if not vmeta:
        raise DocError(404, f"版が見つかりません: {doc_id} v{v}")
    safe_doc_id = _safe_doc_id(doc_id)
    stored_version = vmeta.get("version")
    if (str(vmeta.get("doc_id") or "") != safe_doc_id
            or not isinstance(stored_version, int) or isinstance(stored_version, bool)
            or stored_version != v):
        raise DocError(409, f"書類版メタの識別子が一致しません: {doc_id} v{v}")
    if doc_meta.get("doc_id") and str(doc_meta.get("doc_id")) != safe_doc_id:
        raise DocError(409, f"書類メタの識別子が一致しません: {doc_id}")
    doc_kind = str(doc_meta.get("kind") or "").strip()
    version_kind = str(vmeta.get("kind") or "").strip()
    if doc_kind and version_kind and doc_kind != version_kind:
        raise DocError(409, f"書類種別が版メタと一致しません: {doc_id} v{v}")
    for key in ("case_id", "customer_id"):
        doc_value = str(doc_meta.get(key) or "").strip()
        version_value = str(vmeta.get(key) or "").strip()
        if doc_value and version_value and doc_value != version_value:
            raise DocError(409, f"{key} が書類メタと版メタで一致しません: {doc_id} v{v}")
        # customer_id is a new version-level authorization fact.  Do not let a
        # doc-wide migration silently backfill it into an older version: an exact
        # case/customer/version export requires the version metadata itself to
        # carry that binding.  case_id retains its historical compatibility.
        vmeta[key] = version_value if key == "customer_id" else (version_value or doc_value)
    vmeta["kind"] = version_kind or doc_kind
    ext = str(vmeta.get("fmt") or "md").strip().lower()
    if ext not in set(_FMT_EXT.values()):
        raise DocError(409, f"本文形式が不正です: {doc_id} v{v}")
    digest = str(vmeta.get("content_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DocError(409, f"本文ハッシュが不正です: {doc_id} v{v}")
    if "bytes" in vmeta and (not isinstance(vmeta["bytes"], int)
                              or isinstance(vmeta["bytes"], bool)
                              or vmeta["bytes"] < 0):
        raise DocError(409, f"本文byte数が不正です: {doc_id} v{v}")
    body_path = d / f"v{v}.{ext}"
    if not body_path.is_file() or body_path.is_symlink():
        raise DocError(404, f"本文ファイルが見つかりません: {body_path.name}")
    vmeta["fmt"] = ext
    vmeta["content_sha256"] = digest
    return vmeta


def get_version(data_dir, doc_id: str, version: int | None = None) -> dict:
    """指定版（None=最新）の {meta, body} を返す。"""
    vmeta = get_version_metadata(data_dir, doc_id, version)
    d = _doc_dir(data_dir, doc_id)
    v = int(vmeta["version"])
    ext = str(vmeta["fmt"])
    body_path = d / f"v{v}.{ext}"
    body = body_path.read_text(encoding="utf-8")
    actual_hash = _sha256(body)
    if not vmeta.get("content_sha256") or not hmac.compare_digest(
            str(vmeta.get("content_sha256")), actual_hash):
        raise DocError(
            409,
            f"書類本文のハッシュが版メタと一致しません: {doc_id} v{v}。"
            "原本を保全し、バックアップから復元してください。",
        )
    return {"meta": vmeta, "body": body}


def list_versions(data_dir, doc_id: str) -> list[dict]:
    """版メタの昇順リスト。"""
    d = _doc_dir(data_dir, doc_id)
    if not d.is_dir() or d.is_symlink():
        return []
    out = []
    for n in range(1, latest_version(data_dir, doc_id) + 1):
        try:
            out.append(get_version_metadata(data_dir, doc_id, n))
        except DocError:
            continue
    return out


def diff(data_dir, doc_id: str, v_from: int, v_to: int) -> str:
    """2版間の unified diff（テキスト）。"""
    a = get_version(data_dir, doc_id, v_from)["body"].splitlines(keepends=True)
    b = get_version(data_dir, doc_id, v_to)["body"].splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"v{v_from}", tofile=f"v{v_to}"))


def list_documents(data_dir) -> list[dict]:
    """全書類のインデックス（ファイル走査=正本から再構築・同期後も正しい）。"""
    root = _docs_root(data_dir)
    if not root.exists():
        return []
    docs = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.is_symlink():
            continue
        lv = latest_version(data_dir, d.name)
        if lv < 1:
            continue
        try:
            vmeta = get_version_metadata(data_dir, d.name, lv)
        except DocError:
            continue
        meta = _read_json(d / "meta.json")
        docs.append({"doc_id": d.name, "kind": vmeta.get("kind", ""), "latest": lv,
                     "fmt": vmeta.get("fmt", ""),
                     "sample": bool(vmeta.get("sample", meta.get("sample", False))),
                     "case_id": str(vmeta.get("case_id") or ""),
                     "customer_id": str(vmeta.get("customer_id") or ""),
                     "latest_sha256": vmeta.get("content_sha256", ""),
                     "updated": meta.get("updated", "")})
    return docs

def profile_drift(data_dir, doc_id: str, version: int | None = None) -> dict:
    """確定した書類の会社情報が、その後変更されたかを返す。

    {"stamped": <当時のhash>, "current": <今のhash>, "drifted": bool, "profile": <当時の値>}
    交付済みの書類を後から見たとき「今の業者情報で作り直されている」と誤解しないための材料。
    """
    from hub_core import branding as _br
    from hub_core.auth import CompanyProfileError, load_company
    v = get_version(data_dir, doc_id, version)
    stamped = str((v.get("meta") or {}).get("company_profile_hash") or "")
    try:
        current = _br.profile_hash(load_company(data_dir, strict=True) or {})
    except CompanyProfileError:
        current = ""
    return {"stamped": stamped, "current": current,
            "drifted": bool(stamped and current and stamped != current),
            "profile": _br.load_snapshot(data_dir, stamped)}
