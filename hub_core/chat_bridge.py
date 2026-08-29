"""あいのて 会話ブリッジ (Phase1a)。

会話AI(Claude) → 既存ツールへの **role束縛 dispatch**。設計書 §2/§8 準拠:

- 同一プロセスで hub_core.views(read)/hub_core.operations(write)/audit を import dispatch
  する(別 JSON-RPC stdio を立てない)。
- **Fix1**: read は views.query_page を通し、必ず viewer 認可(行スコープ+個人情報列マスク S0-3)
  を適用する(生PIIをAIに素通ししない)。
- **Fix3**: actor/role は呼出時の Viewer に束縛する。RI_HUB_MCP_ROLE env 既定『代表』には
  一切依存しない(env を立てても権限昇格しない)。
- **安全境界 (設計書 分岐3 = B)**: 可逆操作(case_advance/task_done)は role を満たせば AI 自動可。
  不可逆操作(approval_decide/hold_release)と記名確定(finalize)は confirm=True(人間の確認)が
  無ければ実行せず needs_confirmation を返す。finalize は role ゲート(宅建士/責任者/代表)も必須。

全ての状態変更は既存 apply_operation / 監査追記を通り HMAC 監査される。
LLM との往復ループ(BYO ANTHROPIC_API_KEY)は本モジュールの execute() を呼ぶ薄い層として上に載る。
"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import documents
from .audit import AuditChainError, append_events, verify_audit_chain
from .operations import OP_ROLES, OpError, apply_operation
from .views import query_page

# 可逆=取り消せる前進操作。不可逆=決定/確定/金銭で取り消しが重い操作。
REVERSIBLE_OPS = {"case_advance", "task_done", "lead_convert", "viewing_schedule",
                  "stage_advance", "attribute_update", "contact_log_add", "property_register",
                  "requirement_check"}
IRREVERSIBLE_OPS = {"approval_decide", "hold_release", "billing_create"}
FINALIZE_ROLES = {"宅建士"}

_JST = timezone(timedelta(hours=9))


class BridgeError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(_JST).replace(microsecond=0).isoformat()


def catalog(viewer) -> dict:
    """この viewer が会話で使えるアクションと、その安全区分(role/確認要否)を返す。"""
    role = getattr(viewer, "role", None)
    ops = []
    for op in ("case_advance", "task_done", "lead_convert", "viewing_schedule",
               "stage_advance", "attribute_update", "contact_log_add", "property_register",
               "requirement_check", "approval_decide", "hold_release", "billing_create"):
        role_allowed = role in OP_ROLES.get(op, set())
        ops.append({
            "op": op,
            "reversible": op in REVERSIBLE_OPS,
            "ai_auto": op in REVERSIBLE_OPS and role_allowed,   # 可逆かつ権限あり=AI自動可
            "needs_human_confirm": op in IRREVERSIBLE_OPS,
            "role_allowed": role_allowed,
        })
    return {
        "viewer_role": role,
        "read": {"action": "read", "args": ["source"], "note": "viewer認可(行スコープ+個人情報列マスク)を適用"},
        "save_document": {"action": "save_document", "args": ["doc_id", "body", "kind", "fmt"],
                          "reversible": True, "ai_auto": True, "note": "版append-only・ファイル正本に保存"},
        "operations": ops,
        "finalize": {"needs_human_confirm": True, "role_allowed": role in FINALIZE_ROLES,
                     "args": ["doc_id|content", "takkenshi_name", "license_no"]},
    }


def read(data_dir, source: str, viewer) -> dict:
    """状況把握用の読み取り。Fix1: query_page を通し viewer 認可を必ず適用する。"""
    res = query_page(Path(data_dir), source, viewer)
    if res is None:
        raise BridgeError(404, f"読み取り不可のソースです: {source}")
    labels, rows = res
    return {"source": source, "columns": labels, "rows": rows, "count": len(rows),
            "viewer_role": getattr(viewer, "role", None)}


def operate(data_dir, op: str, params: dict, viewer, confirm: bool = False) -> dict:
    """状態遷移。Fix3: actor/role は viewer 束縛。不可逆は confirm 無しでは実行しない。"""
    actor = getattr(viewer, "user", "?")
    role = getattr(viewer, "role", None)
    if op not in REVERSIBLE_OPS and op not in IRREVERSIBLE_OPS:
        raise BridgeError(400, f"operate で扱わない操作です: {op}")
    if op in IRREVERSIBLE_OPS and not confirm:
        return {"status": "needs_confirmation", "op": op, "params": params,
                "reason": "不可逆操作のため人間の確認(承認ボタン)が必要です。",
                "role_allowed": role in OP_ROLES.get(op, set())}
    try:
        res = apply_operation(Path(data_dir), op, params or {}, actor, role)
    except OpError as exc:
        return {"status": "error", "op": op, "code": exc.code, "error": exc.msg}
    res.update({"status": "ok", "actor": actor, "role": role})
    return res


def _safe_juusetsu_id(name: str) -> str:
    """物件名から安全な doc_id を導出。英数・日本語・_- のみ許可（パス区切り/../ を排除）。"""
    import re as _re
    safe = _re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff_-]", "", str(name or ""))[:64]
    return "JU-" + (safe or "doc")


def create_juusetsu(data_dir, params: dict, viewer) -> dict:
    """物件・契約情報から重要事項説明書(35条)の下書き(md)を決定論的に生成して保存する。
    params: text(貼り付け・任意) と 個別フィールド(property_name/rent 等)。doc_id 不要（自動採番）。
    金メッキしない＝本文はLLMでなく法定様式生成器で組む。宅建士の確認・記名確定を前提。"""
    from hub_core import juusetsu_draft as _jd
    from hub_core import documents as _docs
    fields = {}
    text = str(params.get("text") or "").strip()
    if text:
        fields.update(_jd.parse_pasted(text))
    for k in _jd.FIELD_KEYS:
        v = params.get(k)
        if v is not None and str(v).strip():
            fields[k] = str(v).strip()
    if not fields.get("property_name") and not fields.get("address"):
        raise BridgeError(400, "物件名か所在地が要ります（重説の対象物件を特定するため）。")
    from hub_core.auth import load_company as _lc
    fields = _jd.fill_from_company(fields, _lc(data_dir, strict=True))  # 業者/宅建士を profile から必ず補完(法定)
    import datetime as _dt
    md = _jd.render_juusetsu_md(fields, today=_dt.date.today().isoformat())
    from hub_core import prs_juusetsu as _prs_j
    md = _prs_j.apply_to_draft(
        md, _prs_j.fetch_for_draft(data_dir, fields.get("address") or ""))
    name = fields.get("property_name") or "重要事項説明書"
    did = _safe_juusetsu_id(name)   # ホワイトリスト（パストラバーサル防止・派生点で封鎖）
    case_id = str(params.get("case_id") or params.get("case") or "").strip()
    from hub_core.access import authorized_case_binding
    binding = authorized_case_binding(data_dir, viewer, case_id)
    if binding is None:
        raise BridgeError(403, "担当案件を指定しない書類、または他の担当者の案件の書類は作成できません。")
    res = _docs.save_version(
        data_dir, did, md, kind="juusetsu", fmt="md",
        author=f"あいのて(チャット/{getattr(viewer, 'user', 'ai')})",
        case_id=case_id,
        customer_id=str(binding.get("customer_id") or "").strip(),
    )
    import urllib.parse as _up
    return {"ok": True, "doc_id": res["doc_id"], "version": res["version"],
            "preview_url": "/doc/preview?doc=" + _up.quote(res["doc_id"], safe=""),
            "note": "重要事項説明書の下書きを作成しました。PRS調査の要確認欄を含め、"
                    "プレビューで原典を確認・編集し、ログイン中の宅地建物取引士が記名確定してください。"}


def save_document(data_dir, doc_id: str, body, viewer, *, kind: str = "", fmt: str = "md") -> dict:
    """書類の新しい版を保存(ファイル正本・append-only)。下書き保存=可逆なのでAI自動可。"""
    try:
        from hub_core.access import document_access_allowed
        if not getattr(viewer, "sees_all_rows", lambda: False)() \
                and not document_access_allowed(data_dir, viewer, doc_id):
            return {"status": "error", "code": 403,
                    "error": "担当案件に紐付かない書類は保存できません。"}
        res = documents.save_version(data_dir, doc_id, body, kind=kind, fmt=fmt,
                                     author=getattr(viewer, "user", "?"))
    except documents.DocError as exc:
        return {"status": "error", "code": exc.code, "error": exc.msg}
    res["status"] = "saved"
    return res


def _claim_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return "".join(ch for ch in text if not ch.isspace())


def _signer_claims(data_dir, viewer) -> tuple[set[str], set[str]]:
    """署名者としてすでに束縛されている氏名・登録番号を返す。"""
    names: set[str] = set()
    regs: set[str] = set()

    def add_name(value):
        token = _claim_key(value)
        if token:
            names.add(token)

    def add_reg(value):
        token = _claim_key(value)
        if token:
            regs.add(token)

    if viewer is not None and (
            str(getattr(viewer, "display_name", "") or "").strip()
            or str(getattr(viewer, "registration_no", "") or "").strip()):
        for ident in getattr(viewer, "identities", lambda: {getattr(viewer, "user", "")})():
            add_name(ident)
        add_reg(getattr(viewer, "registration_no", ""))

    try:
        from hub_core.auth import load_users
        rec = load_users(data_dir).get(getattr(viewer, "user", ""), {})
    except Exception:
        rec = {}
    if isinstance(rec, dict):
        for key in ("display_name", "signer_name", "takkenshi_name"):
            add_name(rec.get(key))
        for key in ("registration_no", "takkenshi_reg"):
            add_reg(rec.get(key))

    return names, regs


def _enforce_signer_claim(data_dir, viewer, takkenshi_name: str, license_no: str) -> None:
    # フォームへ氏名・番号を入力できるだけでは記名資格としない。
    # HTTPでは viewer は users.json から復元されるため、この2値がサーバ側の
    # 管理済みプロフィールであることを先に要求する。未登録を空集合として扱うと
    # 下の membership 検査が素通りするため、明示的に fail-closed にする。
    profile_name = _claim_key(getattr(viewer, "display_name", ""))
    profile_reg = _claim_key(getattr(viewer, "registration_no", ""))
    if not profile_name or not profile_reg:
        raise BridgeError(
            403,
            "宅建士プロフィールの氏名と登録番号が未登録のため記名確定できません。",
        )
    names, regs = _signer_claims(data_dir, viewer)
    if names and _claim_key(takkenshi_name) not in names:
        raise BridgeError(403, "記名者名がログイン中の宅建士として保存された氏名と一致しません。")
    if regs and _claim_key(license_no) not in regs:
        raise BridgeError(403, "宅建士登録番号がログイン中の宅建士として保存された登録番号と一致しません。")


def finalize(data_dir, target_id: str, takkenshi_name: str, license_no: str,
             content, viewer, confirm: bool = False, doc_id: str = "", version=None) -> dict:
    """記名済み本文の版とhashを監査へ束縛し、人間確認後だけ確定する。"""
    role = getattr(viewer, "role", None)
    if role not in FINALIZE_ROLES:
        raise BridgeError(403, "記名確定は本人確認済みの宅地建物取引士だけが実行できます。")
    doc_id = (doc_id or "").strip()
    name = (takkenshi_name or "").strip()
    lic = (license_no or "").strip()
    if not name or not lic:
        raise BridgeError(400, "宅地建物取引士名と登録番号が必要です。")
    _enforce_signer_claim(data_dir, viewer, name, lic)

    def check_gate(body: str) -> None:
        # 氏名・登録番号はこの操作で記入するため、記名後の交付本文を法定ゲートへ渡す。
        try:
            from hub_core import deal_taxonomy as _tax
            gate = _tax.finalize_type_gate(body)
        except Exception as exc:  # noqa: BLE001 検査不能を確定可へ読み替えない
            raise BridgeError(
                409,
                "法定必須項目を安全に検査できなかったため、記名確定を中止しました。",
            ) from exc
        if gate.get("blocked"):
            raise BridgeError(400, gate.get("message")
                              or f"法定必須項目が未記入です: {gate.get('missing')}")

    def audit_event(final_target: str, content_hash: str, case_id: str = "",
                    company_profile_hash: str | None = None) -> dict:
        try:
            from hub_core import branding as _branding
            from hub_core.auth import is_configured
            if company_profile_hash is None:
                profile_hash = _branding.snapshot_profile(data_dir)
            else:
                profile_hash = str(company_profile_hash or "").strip().lower()
                if profile_hash:
                    profile = _branding.load_snapshot(data_dir, profile_hash)
                    if not profile or _branding.profile_hash(profile) != profile_hash:
                        raise ValueError("source company profile snapshot mismatch")
        except Exception as exc:  # noqa: BLE001 確定証跡を欠いたまま進めない
            raise BridgeError(
                409, "会社情報の確定時スナップショットを保存できないため記名確定を中止しました。"
            ) from exc
        if is_configured(data_dir) and not profile_hash:
            raise BridgeError(
                409, "会社情報の確定時スナップショットを保存できないため記名確定を中止しました。")
        event = {
            "event_id": "BRIDGE-FINAL-" + hashlib.sha256(
                f"{final_target}|{lic}|{content_hash}".encode("utf-8")).hexdigest()[:16],
            "actor": getattr(viewer, "user", "?"),
            "action": "finalized_with_signature",
            "target": final_target,
            "gate_status": "finalized",
            "takkenshi_name": name,
            "license_no": lic,  # 既存監査schema名。値は宅建士の登録番号。
            "registration_no": lic,
            "content_hash": content_hash,
            "company_profile_hash": profile_hash,
            "timestamp": _now(),
            "source_ref": "chat_bridge/finalize",
        }
        if case_id:
            event["case_id"] = case_id
        return event

    log = Path(data_dir) / "audit_log.jsonl"
    if doc_id:
        from hub_core.access import document_access_allowed
        if not document_access_allowed(data_dir, viewer, doc_id, version):
            raise BridgeError(403, "担当案件に紐付かない書類は記名確定できません。")
        with documents.document_transaction(data_dir, doc_id):
            try:
                stored = documents.get_version(data_dir, doc_id, version)
            except documents.DocError as exc:
                raise BridgeError(exc.code, exc.msg)
            meta = stored.get("meta") or {}
            source_profile_hash = str(meta.get("company_profile_hash") or "").strip().lower()
            if meta.get("sample") is True or str(meta.get("author") or "").endswith("(seed)"):
                raise BridgeError(
                    409,
                    "この書類はお試し用の見本（サンプル）のため記名確定できません。"
                    "実際の物件から新しい書類を作成してください。",
                )
            if content is not None and str(content) and str(content) != stored["body"]:
                raise BridgeError(409, "保存済みの対象版と確定本文が一致しません。画面を開き直してください。")
            signed_body = documents.signature_body(stored["body"], name, lic)
            check_gate(signed_body)
            prospective_hash = hashlib.sha256(signed_body.encode("utf-8")).hexdigest()
            source_target = (target_id or "").strip() or f"{doc_id}#v{meta['version']}"
            if not confirm:
                return {"status": "needs_confirmation", "action": "finalize",
                        "target_id": source_target, "content_hash": prospective_hash,
                        "reason": "記名確定は人間の確認が必須です(AIは自動実行できません)。"}

            doc_meta_path = Path(data_dir) / "documents" / documents._safe_doc_id(doc_id) / "meta.json"
            previous_meta = doc_meta_path.read_bytes() if doc_meta_path.is_file() else None
            try:
                saved = documents.save_version(
                    data_dir, doc_id, signed_body,
                    kind=meta.get("kind") or "juusetsu", fmt=meta.get("fmt") or "md",
                    author=f"記名確定({name})", sample=False,
                    case_id=str(meta.get("case_id") or ""),
                    company_profile_hash=source_profile_hash,
                )
            except documents.DocError as exc:
                raise BridgeError(exc.code, exc.msg)
            final_target = f"{doc_id}#v{saved['version']}"
            try:
                event = audit_event(
                    final_target, saved["content_sha256"], str(saved.get("case_id") or ""),
                    company_profile_hash=str(saved.get("company_profile_hash") or ""))
                append_events(log, [event])
                broken = verify_audit_chain(log)
                if broken:
                    raise AuditChainError(f"監査ログ検証に失敗しました: {broken}")
            except Exception as exc:  # noqa: BLE001 証跡生成から検証までを同じrollback境界に置く
                try:
                    documents.rollback_uncommitted_version(
                        data_dir, doc_id, saved, previous_meta)
                except documents.DocError as rollback_exc:
                    raise BridgeError(409, rollback_exc.msg) from exc
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError(409, "監査記録に失敗したため記名版を保存しませんでした。"
                                       "監査台帳を保全して確認してください。") from exc
            return {"status": "finalized", "target_id": final_target,
                    "version": saved["version"], "content_hash": saved["content_sha256"],
                    "actor": getattr(viewer, "user", "?"), "audit_chain_ok": True}

    target_id = (target_id or "").strip()
    if not target_id or content is None or str(content) == "":
        raise BridgeError(400, "保存済みdoc_id、またはtarget_idと確定本文が必要です。")
    signed_body = documents.signature_body(str(content), name, lic)
    check_gate(signed_body)
    content_hash = hashlib.sha256(signed_body.encode("utf-8")).hexdigest()
    if not confirm:
        return {"status": "needs_confirmation", "action": "finalize", "target_id": target_id,
                "content_hash": content_hash,
                "reason": "記名確定は人間の確認が必須です(AIは自動実行できません)。"}
    try:
        event = audit_event(target_id, content_hash)
        append_events(log, [event])
        broken = verify_audit_chain(log)
    except BridgeError:
        raise
    except (AuditChainError, OSError) as exc:
        raise BridgeError(409, "監査記録に失敗しました。監査台帳を保全して確認してください。") from exc
    if broken:
        raise BridgeError(409, f"監査ログ検証に失敗しました: {broken}")
    return {"status": "finalized", "target_id": target_id, "content_hash": content_hash,
            "actor": getattr(viewer, "user", "?"), "audit_chain_ok": True}


def execute(data_dir, action: str, params: dict, viewer, confirm: bool = False) -> dict:
    """会話AIのツール呼び出しの単一エントリ。LLM 層はここを呼ぶ。"""
    params = params or {}
    if action == "read":
        return read(data_dir, params.get("source"), viewer)
    if action == "operate":
        op = params.get("op")
        op_params = params.get("params") or {k: v for k, v in params.items() if k != "op"}
        return operate(data_dir, op, op_params, viewer, confirm=confirm)
    if action == "save_document":
        return save_document(data_dir, params.get("doc_id"), params.get("body"), viewer,
                             kind=params.get("kind", ""), fmt=params.get("fmt", "md"))
    if action == "create_juusetsu":
        return create_juusetsu(data_dir, params, viewer)
    if action == "finalize":
        return finalize(data_dir, params.get("target_id"), params.get("takkenshi_name"),
                        params.get("license_no"), params.get("content"), viewer, confirm=confirm,
                        doc_id=params.get("doc_id", ""), version=params.get("version"))
    raise BridgeError(400, f"未知のアクションです: {action}")
