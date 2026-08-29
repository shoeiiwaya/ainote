"""hub_core/telephony.py — 物確自動応答（着信IVR）。電話を取らずに物件の最新状態を返す＝ぶっかくん型。

なぜ: 不動産の物確（物件確認）電話は業務の主経路だが、毎回人が電話に出て「今この物件は取扱可能か」を
伝えるのが非常に手間。本モジュールは着信電話をIVRで自動応答する骨格（受け側＝自社物件の物確を自動化）:
- 着信正規化: プロバイダのwebhook → {call_id, from_number, digits(業者が入力した物確番号), ...}。
- 発信者特定（caller-ID→業者）: 電話番号→業者名。**信頼できる無料の逆引きAPIは日本に無い**ので自社台帳で
  蓄積する（宅建業者名簿の取込は人間ゲート・他社サイトのスクレイピングはしない）。
- 物確番号→物件: 業者向けマイソクに載せた物確番号を業者がダイヤル入力→物件を特定。
- 状態応答: あいのての物件レコードから現在の状態（取扱中/商談中/成約済）を**データから**返す（捏造しない・
  データが無ければ「担当が確認します」に落とす）。
- 詳細希望: 物件詳細（マイソク）を発信者のFAXへ自動送信（fax.py の送信ゲート経由・実送信は人間ゲート）。
- Provider抽象: MockTelephonyProvider（実発話/実架電しない）。実テレフォニー（Twilio/IVRy/MiiTel等）は人間ゲート。
- stdlib only・ネットワーク非接触・捏造しない。
"""
from __future__ import annotations

import datetime
import json
import re

JST = datetime.timezone(datetime.timedelta(hours=9))


def _now() -> str:
    return datetime.datetime.now(JST).replace(microsecond=0).isoformat()


class TelephonyError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def normalize_number(num: str) -> str:
    """電話番号を突合キーに正規化（数字のみ・日本の+81→0）。"""
    d = re.sub(r"[^0-9+]", "", str(num or ""))
    if d.startswith("+81"):
        d = "0" + d[3:]
    return re.sub(r"[^0-9]", "", d)


def normalize_inbound_call(payload) -> dict:
    """テレフォニー業者のwebhook（着信/IVR入力）→ 正規化。捏造しない（無い項目は空）。"""
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload if not isinstance(payload, (bytes, bytearray))
                                 else payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise TelephonyError(400, "webhookペイロードが不正なJSONです。")
    if not isinstance(payload, dict):
        raise TelephonyError(400, "webhookペイロードはオブジェクトである必要があります。")

    def g(*keys):
        for k in keys:
            v = payload.get(k)
            if v not in (None, ""):
                return str(v)
        return ""
    frm = g("from", "from_number", "caller", "src", "ani")
    call_id = g("call_id", "id", "sid", "CallSid")
    return {
        "call_id": call_id,
        "delivery_id": g("delivery_id", "event_id", "webhook_event_id") or call_id,
        "from_number": frm, "from_key": normalize_number(frm),
        "to_number": g("to", "to_number", "dst", "dnis"),
        "digits": re.sub(r"[^0-9#*]", "", g("digits", "Digits", "dtmf", "input")),
        "intent": g("intent"),   # 例: status / details（プロバイダ側のIVRメニュー選択）
        "received_at": g("received_at", "timestamp", "created_at") or _now(),
    }


# 物件の状態 → 自動応答の定型（データにある状態だけ。無ければ空＝「担当が確認します」に落とす＝捏造しない）。
_STATUS_SAY = {
    "取扱中": "この物件は現在お取り扱い中です。ご紹介いただけます。",
    "商談中": "この物件は現在商談中です。",
    "成約済": "この物件は成約済みです。ありがとうございました。",
    "取扱終了": "この物件はお取り扱いを終了しております。",
}


def status_answer(status: str) -> str:
    """物件状態 → 自動応答の読み上げ文。定型に無い/空なら空文字（担当確認にフォールバック・捏造しない）。"""
    return _STATUS_SAY.get((status or "").strip(), "")


def build_ivr_response(*, property_name: str = "", status: str = "",
                       want_details: bool = False, caller_has_fax: bool = False) -> dict:
    """着信IVRの応答を組む（決定論・データにある状態だけ話す）。
    返り値: {say: 読み上げ文, action: status|fax_details|to_staff, fax: bool}。捏造しない。"""
    pname = (property_name or "お問い合わせの物件").strip()
    ans = status_answer(status)
    if not ans:
        return {"say": f"{pname}について、担当者が折り返しご連絡いたします。", "action": "to_staff", "fax": False}
    if want_details:
        if caller_has_fax:
            return {"say": f"{pname}は{ans} 詳細をFAXでお送りします。", "action": "fax_details", "fax": True}
        return {"say": f"{pname}は{ans} 詳細は担当者よりご連絡します。", "action": "to_staff", "fax": False}
    return {"say": f"{pname}は{ans}", "action": "status", "fax": False}


# ============================ Provider（実架電/実発話は人間ゲート） ============================

class TelephonyProvider:
    """テレフォニー業者の抽象。実装は say（IVR発話）等と connected（実接続か）。"""
    name = "abstract"
    connected = False

    def speak(self, call: dict, text: str) -> dict:
        raise NotImplementedError


class MockTelephonyProvider(TelephonyProvider):
    """ローカルモック（実発話/実架電しない）。テレフォニー業者未接続時の既定。"""
    name = "mock"
    connected = False

    def speak(self, call: dict, text: str) -> dict:
        return {"provider": self.name, "spoken": False,
                "note": "モック: 実際の音声応答は行っていません（未接続）。", "text": text}
