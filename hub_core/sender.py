"""M-sender — アウトバウンド送信（Wave4 G4・BYO-SMTP/LINE・ドラフト/キュー先行）。

追客・一次返信・シナリオ配信の送信基盤。**ドラフト作成とキューは¥0**、実送信は BYO アカウント
（ユーザー自身のSMTP/LINE・無料枠可）＝人間ゲート。
- メッセージは draft→queued→sent の状態機械。draft/queued は可逆（自動生成OK）。
- **実送信は人間の明示承認が要る**（一括自動送信を禁止）。未接続(Mock)は実送信しない=delivered False。
- 個人情報（宛先）を勝手に外部へ出さない＝送信は接続済みプロバイダかつ明示承認のときのみ。
- stdlib のみ（SMTP実装は smtplib・実接続は provider 側・モック可）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

MSG_TRANSITIONS = {
    "draft": {"queued", "cancelled"},
    "queued": {"sent", "cancelled", "failed"},
    "sent": set(), "cancelled": set(), "failed": {"queued"},
}


class SendError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


class SenderProvider:
    """送信アダプタの抽象。実装は send(to, subject, body)->dict、connected で接続状態。"""
    name = "abstract"
    channel = "email"
    connected = False

    def send(self, to: str, subject: str, body: str, *, idempotency_key: str = "") -> dict:
        raise NotImplementedError


class MockSender(SenderProvider):
    """未接続の既定（BYO SMTP/LINE未設定時）。**実送信しない**＝delivered False。"""
    name = "mock"
    connected = False

    def send(self, to: str, subject: str, body: str, *, idempotency_key: str = "") -> dict:
        return {"provider": self.name, "delivered": False, "outcome": "mock",
                "note": "モック: 実際の送信は行っていません（BYO送信が未接続）。"}


def new_message(message_id: str, to: str, subject: str, body: str,
                *, channel: str = "email", ref: str = "") -> dict:
    if not message_id or not to or not body:
        raise SendError(400, "message_id・to・body は必須")
    return {
        "message_id": message_id, "to": to, "subject": subject or "",
        "body": body, "channel": channel, "ref": ref,
        "status": "draft", "created_at": _now(), "delivered": False,
        "provider": "", "history": [("draft", _now())],
    }


def transition(message: dict, to_state: str) -> dict:
    cur = message.get("status")
    if to_state not in MSG_TRANSITIONS.get(cur, set()):
        raise SendError(409, f"許可されない遷移: {cur} → {to_state}")
    message["status"] = to_state
    message["history"].append((to_state, _now()))
    return message


def send_message(message: dict, *, provider: SenderProvider | None = None,
                 allow_real_send: bool = False, idempotency_key: str = "") -> dict:
    """queued のメッセージを送信。**実送信は接続済みprovider かつ allow_real_send=True のときのみ**
    （未承認の実送信・個人情報の外部流出を防ぐ）。未接続/未承認は delivered False で queued のまま。"""
    if message.get("status") != "queued":
        raise SendError(409, f"送信は queued からのみ（現在: {message.get('status')}）。")
    prov = provider or MockSender()
    if getattr(prov, "connected", False) and not allow_real_send:
        raise SendError(403, "実送信には明示の承認（allow_real_send）が必要です（人間ゲート）。")
    if idempotency_key:
        message["idempotency_key"] = idempotency_key
    res = prov.send(message["to"], message["subject"], message["body"],
                    idempotency_key=idempotency_key)
    delivered = bool(res.get("delivered", False))
    message["provider"] = res.get("provider", prov.name)
    message["external_id"] = str(res.get("external_id") or res.get("message_id") or "")
    message["delivered"] = delivered
    message["provider_outcome"] = str(
        res.get("outcome") or ("accepted" if delivered else "unknown"))
    if delivered:
        message["status"] = "sent"
        message["history"].append(("sent", _now()))
    return {"delivered": delivered, "provider": message["provider"],
            "outcome": message["provider_outcome"],
            "status": message["status"], "note": res.get("note", "")}


def sender_status(*, connected: bool = False, name: str = "mock") -> dict:
    return {"connected": bool(connected), "provider": name,
            "note": ("送信プロバイダ接続済み。" if connected
                     else "未接続（モック）: 実際の送信は行いません。BYO SMTP/LINEの接続は人間ゲート。")}
