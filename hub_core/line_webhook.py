"""M-line — LINE公式アカウント Webhook 署名検証＋メッセージ正規化（Wave3）。

LINE Messaging API の Webhook を受ける入口。**実チャネルトークン/シークレットは人間ゲート**で、
本モジュールは署名検証とペイロード正規化のみ（送信＝reply/pushは実トークンが要るので未接続時は行わない）。
- 署名検証: X-Line-Signature = Base64(HMAC-SHA256(channel_secret, request_body))。定数時間比較。
- 正規化: LINEのeventリストを ri-hub の反響/接触の素材へ落とす（実登録は operation 経由）。
- stdlib only（hmac/hashlib/base64/json）・ネットワーク非接触（受信検証のみ）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


class LineError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """X-Line-Signature を検証（定数時間比較）。secret 未設定時は False（未接続＝受け付けない）。"""
    if not channel_secret or not signature or not isinstance(signature, str):
        return False   # 署名はstr（bytes等はTypeError化させず拒否・F4是正）
    body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("ascii")
    return hmac.compare_digest(expected, signature)


def parse_events(body: bytes) -> list[dict]:
    """Webhookボディ→eventリスト。壊れたJSONは LineError(400)。"""
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LineError(400, f"不正なペイロード: {e}")
    return data.get("events", []) if isinstance(data, dict) else []


def normalize_events(events: list[dict]) -> list[dict]:
    """LINE event → ri-hub の反響/接触素材。message(text) と follow を拾う。
    実台帳登録はしない（operation 経由・ここは正規化のみ）。"""
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue   # 非dict要素（数値/文字列）は無視（不正ペイロードでクラッシュしない）
        etype = ev.get("type")
        src = ev.get("source", {}) or {}
        user_id = src.get("userId", "")
        provider_id = str(ev.get("webhookEventId") or (ev.get("message", {}) or {}).get("id") or "")
        if not provider_id:
            canonical = json.dumps(ev, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), default=str)
            provider_id = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        base = {"line_user_id": user_id, "reply_token": ev.get("replyToken", ""),
                "timestamp": ev.get("timestamp", ""), "delivery_id": provider_id,
                "delivery_source": "line"}
        if etype == "message" and (ev.get("message", {}) or {}).get("type") == "text":
            out.append({**base, "kind": "message", "text": ev["message"].get("text", "")})
        elif etype == "follow":
            out.append({**base, "kind": "follow", "text": ""})
        elif etype == "unfollow":
            out.append({**base, "kind": "unfollow", "text": ""})
    return out


def handle_webhook(channel_secret: str, body: bytes, signature: str) -> dict:
    """検証つきの一括処理: 署名NG=401、OK=正規化イベントを返す（送信はしない）。"""
    if not verify_signature(channel_secret, body, signature):
        raise LineError(401, "署名検証に失敗（不正な送信元 or 未接続）")
    events = normalize_events(parse_events(body))
    return {"ok": True, "events": events,
            "note": "受信・正規化のみ。返信/push送信は実トークン接続時のみ（人間ゲート）。"}
