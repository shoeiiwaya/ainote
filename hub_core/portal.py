"""M-portal — 顧客/オーナー/入居者ポータル（Wave4 G7・マジックリンク self-host）。

顧客が自分の案件の状況・活動報告を見て、セルフで内見予約/申込ができる限定ポータル。
- 認証=**マジックリンク**（HMAC-SHA256 署名・時限トークン。パスワード不要）。トークンは
  案件IDと有効期限を署名で束縛＝改竄不能。**実際のリンク配布は送信ゲート（M-sender）経由=人間**。
- 顧客のセルフ入力（内見希望/申込）は**ドラフト/依頼として業者側の台帳に載る**（自動確定しない＝
  業者が確認）。個人情報は最小限。
- **公開デプロイは人間ゲート**。ここではトークンとポータル描画のロジック（¥0）まで。
- 署名鍵は audit と同じ別保管鍵を流用（同期フォルダ外）。stdlib（hmac/hashlib/base64/time非依存）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path


class PortalError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _key() -> bytes:
    from hub_core.audit import _load_chain_key
    return _load_chain_key()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64u_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_token(case_id: str, expires_at: str, *, scope: str = "customer") -> str:
    """案件ID＋有効期限(YYYY-MM-DD)＋scope を署名したマジックリンク・トークン。
    expires_at は日付文字列（サーバの Date.now を使わない＝決定的・呼出側が期限を渡す）。"""
    if not case_id or not expires_at:
        raise PortalError(400, "case_id と expires_at は必須")
    import datetime as _dt
    try:
        _dt.date.fromisoformat(str(expires_at)[:10])
    except ValueError:
        raise PortalError(400, "expires_at は YYYY-MM-DD 形式で指定してください。")
    payload = {"case_id": case_id, "exp": expires_at, "scope": scope}
    body = _b64u(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = _b64u(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + sig


def verify_token(token: str, *, today: str) -> dict:
    """トークン検証（署名一致＋未失効）。today(YYYY-MM-DD)を呼出側が渡す＝決定的。
    改竄/期限切れは PortalError。戻り: payload。"""
    if not token or token.count(".") != 1:
        raise PortalError(401, "不正なトークン")
    body, sig = token.split(".", 1)
    expected = _b64u(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise PortalError(401, "署名が一致しません（改竄の可能性）")
    try:
        payload = json.loads(_b64u_dec(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        raise PortalError(401, "トークンの内容が壊れています")
    exp = str(payload.get("exp") or "")
    import datetime as _dt
    try:
        exp_d = _dt.date.fromisoformat(exp[:10])
        today_d = _dt.date.fromisoformat(str(today)[:10])
    except ValueError:
        raise PortalError(401, "リンクの有効期限が不正です")
    if exp_d < today_d:      # 日付として比較（文字列辞書順でない）
        raise PortalError(401, f"リンクの有効期限が切れています（{exp}）")
    return payload


def portal_view_data(data_dir, case_id: str, *, scope: str = "customer",
                     today: str | None = None) -> dict:
    """ポータルに出す最小限データ。scope で内容を変える（PIIは最小）。
    - customer/tenant: 案件進捗＋申込状況。
    - owner(売主): 活動報告（掲載期間の接触/内見/反響の実績＝法定報告の共有面）。"""
    from hub_core.store import SqliteStore
    from hub_core.operations import applications_for_case
    db = None
    try:
        p = Path(data_dir) / "hub.db"
        db = SqliteStore(p) if p.exists() else None
    except Exception:
        db = None
    case = {}
    if db is not None:
        rows = db.query("cases", "case_id = ?", (case_id,))
        case = rows[0] if rows else {}
    out = {
        "case_id": case_id, "scope": scope,
        "property": case.get("property_name") or "",
        "status": case.get("status") or "",
        "applications": [],
        "activity": None,
        "contracts": None,
    }
    if scope == "tenant":
        # 入居者向け=自分の案件の契約と更新期限（管理物件の更新案内の共有面）。PIIは最小。
        try:
            from hub_core import contract as _ct
            import datetime as _dt
            today2 = today or _dt.date.today().isoformat()
            rows = db.query("contract_register", "case_id = ?", (case_id,)) if db is not None else []
            active = [c for c in rows if (c.get("status") or "").strip() not in ("解約", "完了", "終了")]
            out["contracts"] = [{
                "contract_type": c.get("contract_type") or "",
                "end_date": c.get("end_date") or "",
                "days_left": _ct.days_until(c.get("end_date"), today2),
            } for c in active]
        except Exception:
            out["contracts"] = []
    elif scope == "owner":
        # 売主向け=活動報告（直近60日を既定期間）。数値は台帳実績のみ。
        try:
            from hub_core import activity_report as _ar
            import datetime as _dt
            to = today or _dt.date.today().isoformat()
            frm = (_dt.date.fromisoformat(to) - _dt.timedelta(days=60)).isoformat()
            rep = _ar.build_activity_report(data_dir, case_id, period_from=frm, period_to=to,
                                            mediation_type=case.get("mediation_type") or "専任媒介")
            # 売主向けは date+channel のみ（買主の氏名/連絡先を含み得る summary は出さない=PII遮断）
            out["activity"] = {"counts": rep["counts"], "period": f"{frm}〜{to}",
                               "activities": [{"date": a["date"], "channel": a["channel"]}
                                              for a in rep["activities"][-10:]]}
        except Exception:
            out["activity"] = None
    else:
        try:
            apps = applications_for_case(data_dir, case_id)
            out["applications"] = [{"status": a.get("status")} for a in apps]
        except Exception:
            out["applications"] = []
    return out
