"""M-screening — 申込〜審査＋保証会社BYOアダプタ（監査SHINSA/金融アタッチの土台）。

契約クローズ後半の入口。入居申込を受け付け、審査ステートを進め、家賃保証会社へ審査を出す。
- **保証会社の実接続・実審査・実与信は人間ゲート**（協定・APIキー）。未接続時は MockGuarantor で
  業務フロー（申込→書類確認→保証審査依頼→結果反映→契約可）を回せる。Mockは**実与信判断をしない**
  （常に「保証会社の判断待ち」を返す＝勝手に承認/否認しない）。
- 申込・審査の状態は監査イベントで永続化（reindex で replay 復元＝snooze/inbox と同型）。
- 収益: 承認された保証契約は送客/レベニューシェアの対象（実請求は metering/人間ゲート）。
- stdlib only・ネットワーク非接触（Mock）。実 provider は BYOアダプタ実装（tool-scout）。

申込ステート機械（許可遷移のみ）:
  received → docs_ok → screening → approved / declined。approved のみ contract_ready。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

APP_TRANSITIONS = {
    "received": {"docs_ok", "withdrawn"},
    "docs_ok": {"screening", "withdrawn"},
    "screening": {"approved", "declined", "withdrawn"},
    "approved": set(),    # 終端（契約へ）
    "declined": set(),    # 終端
    "withdrawn": set(),   # 終端
}
APP_TERMINAL = {"approved", "declined", "withdrawn"}


class ScreeningError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


class Guarantor:
    """保証会社アダプタの抽象。実装は request（審査依頼）と connected を提供。"""
    name = "abstract"
    connected = False

    def request(self, application: dict) -> dict:
        raise NotImplementedError


class MockGuarantor(Guarantor):
    """未接続の既定。**実与信判断をしない**（勝手に承認/否認しない）＝常に保証会社側の
    人間/実API判断待ちを返す。業務フローの設計検証用。"""
    name = "mock"
    connected = False

    def request(self, application: dict) -> dict:
        return {"guarantor": self.name, "external_id": "MOCK-SCR-" + application["application_id"],
                "decision": "pending", "connected": False,
                "note": "モック: 実際の保証審査は行っていません（保証会社は未接続・実接続は人間ゲート）。"}


def new_application(application_id: str, applicant: str, property_ref: str,
                    *, monthly_rent: int = 0, case_id: str = "") -> dict:
    if not application_id or not applicant:
        raise ScreeningError(400, "application_id と applicant は必須")
    return {
        "application_id": application_id, "applicant": applicant,
        "property_ref": property_ref, "case_id": case_id,
        "monthly_rent": int(monthly_rent or 0),
        "status": "received", "created_at": _now(),
        "guarantor": "", "guarantor_external_id": "", "guarantor_decision": "",
        "history": [("received", _now())],
    }


def advance(application: dict, to: str) -> dict:
    """申込ステートを進める（許可外遷移は拒否）。screening/approved/declined は
    request_screening / apply_screening_result を通す（直接approvedにしない）。"""
    cur = application.get("status")
    if to not in APP_TRANSITIONS.get(cur, set()):
        raise ScreeningError(409, f"許可されない遷移: {cur} → {to}")
    if to in ("approved", "declined"):
        raise ScreeningError(409, "審査結果は apply_screening_result で反映してください（保証会社判断）。")
    application["status"] = to
    application["history"].append((to, _now()))
    return application


def request_screening(application: dict, guarantor: Guarantor | None = None) -> dict:
    """保証会社へ審査依頼（docs_ok → screening）。既定は MockGuarantor（実審査しない）。
    Mock/未接続では decision=pending 固定＝勝手に承認しない。"""
    if application.get("status") != "docs_ok":
        raise ScreeningError(409, f"審査依頼は書類確認後（docs_ok）から。現在: {application.get('status')}")
    g = guarantor or MockGuarantor()
    res = g.request(application)
    application["guarantor"] = res.get("guarantor", g.name)
    application["guarantor_external_id"] = res.get("external_id", "")
    application["guarantor_decision"] = res.get("decision", "pending")
    application["status"] = "screening"
    application["history"].append(("screening", _now()))
    return application


def apply_screening_result(application: dict, decision: str, *, connected: bool = False) -> dict:
    """保証会社の審査結果を反映（screening → approved/declined）。
    **connected=True（実保証会社の実判断）でなければ承認/否認を確定させない**＝
    Mockの結果で契約可にしない（fail-closed・実与信は人間/実providerゲート）。"""
    if application.get("status") != "screening":
        raise ScreeningError(409, "審査中（screening）でのみ結果を反映できます。")
    if decision not in ("approved", "declined"):
        raise ScreeningError(400, "decision は approved / declined")
    if not connected:
        raise ScreeningError(403, "未接続の保証会社の結果では確定できません（実与信は実保証会社の接続が必要）。")
    application["status"] = decision
    application["guarantor_decision"] = decision
    application["history"].append((decision, _now()))
    return application


def is_contract_ready(application: dict) -> bool:
    """契約に進めるのは、実接続の保証会社が承認したときのみ。"""
    return application.get("status") == "approved"


def guarantor_status(*, connected: bool = False, name: str = "mock") -> dict:
    """UIの正直表示: 保証会社が接続済みか（未接続=Mock・実与信しない）。"""
    return {"connected": bool(connected), "guarantor": name,
            "note": ("保証会社 接続済み。" if connected
                     else "未接続（モック）: 実際の保証審査・与信は行いません。連携・キー投入は人間ゲート。")}
