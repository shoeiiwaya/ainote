"""M-esign — BYO電子契約アダプタ（Wave3）。

クラウドサイン/DocuSign/GMOサイン等の電子契約サービスを **BYO（顧客が自分の契約）** で差せる
アダプタ層。本モジュールは:
- プロバイダ非依存の封筒（envelope）状態機械: created→sent→signed / declined / voided。
- **実プロバイダSDK/APIキーは人間ゲート**。キー未設定時は MockProvider（ローカル・実送信しない）で
  設計と業務フロー（承認→送信→完了→監査）を回せる。実送信は本物のProviderが接続された時のみ。
- 状態遷移は監査可能（誰がいつ何を）。宅建の重説/契約は記名確定（既存finalize）と併存。

stdlib only・ネットワーク非接触（Mockは擬似）。実Provider実装は別途 SDK 採用（tool-scout）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# 封筒の状態機械（許可された遷移のみ）
TRANSITIONS = {
    "created": {"sent", "voided"},
    "sent": {"signed", "declined", "voided"},
    "signed": set(),      # 終端
    "declined": set(),    # 終端
    "voided": set(),      # 終端
}
TERMINAL = {"signed", "declined", "voided"}


class EsignError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


class Provider:
    """プロバイダの抽象。実装は send（実送信）と provider_name を提供。"""
    name = "abstract"
    connected = False

    def send(self, envelope: dict) -> dict:
        raise NotImplementedError


class MockProvider(Provider):
    """ローカルモック（実送信しない）。キー未設定時の既定。設計/フロー検証用。"""
    name = "mock"
    connected = False   # 実接続でない＝実送信していないことを表す

    def send(self, envelope: dict) -> dict:
        # 実際のメール送信・API呼出は行わない。擬似の外部IDだけ付与。
        return {"provider": self.name, "external_id": "MOCK-" + envelope["envelope_id"],
                "delivered": False, "note": "モック: 実際の送信は行っていません（未接続）。"}


def new_envelope(envelope_id: str, title: str, signers: list[dict]) -> dict:
    if not envelope_id or not title:
        raise EsignError(400, "envelope_id と title は必須")
    if not signers:
        raise EsignError(400, "signers（署名者）が必要")
    return {
        "envelope_id": envelope_id, "title": title,
        "signers": [{"name": s.get("name", ""), "email": s.get("email", ""),
                     "signed_at": ""} for s in signers],
        "status": "created", "created_at": _now(), "history": [("created", _now())],
        "provider": "", "external_id": "",
    }


def transition(envelope: dict, to: str, *, provider: Provider | None = None,
               signer_email: str = "", allow_real_send: bool = False) -> dict:
    """状態遷移。許可外遷移は拒否。sent には provider が必要（既定Mock）。
    **実送信の機械ゲート**: provider.connected（実プロバイダ）で送る場合は allow_real_send=True 必須。
    無ければ EsignError（＝人間ゲートを通さない実送信をモジュールが拒否・監査指摘対応）。"""
    cur = envelope.get("status")
    if to not in TRANSITIONS.get(cur, set()):
        raise EsignError(409, f"許可されない遷移: {cur} → {to}")
    if to == "sent":
        prov = provider or MockProvider()
        if getattr(prov, "connected", False) and not allow_real_send:
            raise EsignError(403, "実プロバイダでの送信は明示の承認（allow_real_send）が必要です"
                                  "（人間ゲート・未承認の実送信は拒否）。")
        res = prov.send(envelope)
        envelope["provider"] = res.get("provider", prov.name)
        envelope["external_id"] = res.get("external_id", "")
        envelope["delivered"] = bool(res.get("delivered", False))
    if to == "signed" and signer_email:
        for s in envelope["signers"]:
            if s["email"] == signer_email and not s["signed_at"]:
                s["signed_at"] = _now()
                break
    envelope["status"] = to
    envelope["history"].append((to, _now()))
    return envelope


def is_fully_signed(envelope: dict) -> bool:
    return (envelope.get("status") == "signed"
            and all(s["signed_at"] for s in envelope.get("signers", [])))


def provider_status(*, connected: bool = False, name: str = "mock") -> dict:
    """UIの正直表示用: 実プロバイダが接続されているか（未接続=モック・実送信しない）。"""
    return {"connected": bool(connected), "provider": name,
            "note": ("実プロバイダ接続済み。" if connected
                     else "未接続（モック）: 実際の送信・署名は行われません。キー投入は人間ゲート。")}
