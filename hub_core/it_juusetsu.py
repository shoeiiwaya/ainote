"""M-IT重説 — IT重要事項説明のセッション管理（Wave4磨き・BYO映像・¥0）。

宅建業法のIT重説（テレビ会議等）を運用するためのセッション記録と法定要件チェック。
- **実際の映像会議は外部（Zoom/Meet等・BYO）＝あいのては映像を行わない**。ここは日程・BYO映像URL・
  法定要件の充足チェック・実施記録のみ（記録は37条書面交付・IT重説の要件充足の証跡）。
- IT重説の法定4要件（国交省ガイドライン）を満たすまで「実施可能」にしない（fail-closed）。
- BYO映像URLは外部リンク（送信/接続は人間ゲート）。stdlib のみ・ネットワーク非接触。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# IT重説の法定要件（国交省 賃貸・売買IT重説の運用ガイドライン準拠の要件名）。
IT_REQUIREMENTS = (
    "宅建士証の提示（画面で相手が視認できる）",
    "重要事項説明書の事前送付（説明前に相手方へ交付済み）",
    "映像・音声の双方向性（相手方の状況を相互に確認できる）",
    "相手方の環境確認（映像音声が良好・書面を確認できる状態）",
)

SESSION_STATES = {
    "予約": {"要件確認", "中止"},
    "要件確認": {"実施可", "予約", "中止"},
    "実施可": {"実施済", "中止"},
    "実施済": set(), "中止": set(),
}

# 運用開始ゲート（設計 §7・実施前チェックリスト）。IT重説を「実施」する前に会社プロフィール
# （company.json）へ揃える3点。揃うまで it_advance→実施可 を fail-closed で 403（＝練習モード）。
# 開発・デモは免許前でも回せるが、実施は要件充足まで機械的に不可、を両立させる。
IT_GATE_FIELDS = ("license_no", "takkenshi_reg", "it_guideline_confirmed_at")
IT_GATE_LABELS = {
    "license_no": "宅建業免許番号",
    "takkenshi_reg": "宅建士登録番号",
    "it_guideline_confirmed_at": "現行ガイドライン（令和6年12月版）確認日",
}


def operational_gate_status(company: dict) -> dict:
    """実施前ゲートの充足状況。company（company.json）に3点が揃えば ready=True（本番モード）。
    欠けていれば missing にラベルを列挙（練習モード＝実施可への遷移を止める根拠）。"""
    c = company or {}
    missing = [IT_GATE_LABELS[f] for f in IT_GATE_FIELDS if not str(c.get(f) or "").strip()]
    return {"ready": not missing, "missing": missing,
            "mode": "本番" if not missing else "練習"}


class ItJuusetsuError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def new_session(session_id: str, case_id: str, *, scheduled_at: str = "",
                video_url: str = "") -> dict:
    if not session_id or not case_id:
        raise ItJuusetsuError(400, "session_id と case_id は必須")
    return {
        "session_id": session_id, "case_id": case_id,
        "scheduled_at": scheduled_at, "video_url": video_url,
        "state": "予約", "requirements": {r: False for r in IT_REQUIREMENTS},
        "history": [("予約", _now())],
    }


def check_requirement(session: dict, requirement: str, met: bool = True) -> dict:
    if requirement not in IT_REQUIREMENTS:
        raise ItJuusetsuError(400, f"未知の要件: {requirement}")
    session["requirements"][requirement] = bool(met)
    return session


def all_requirements_met(session: dict) -> bool:
    reqs = session.get("requirements") or {}
    return all(reqs.get(r, False) for r in IT_REQUIREMENTS)


def transition(session: dict, to_state: str) -> dict:
    cur = session.get("state")
    if to_state not in SESSION_STATES.get(cur, set()):
        raise ItJuusetsuError(409, f"許可されない遷移: {cur} → {to_state}")
    # 実施可へ進むには法定要件が全充足（fail-closed）
    if to_state == "実施可" and not all_requirements_met(session):
        raise ItJuusetsuError(403, "IT重説の法定要件が未充足です（全要件を満たすまで実施可にできません）。")
    if to_state == "実施済" and cur != "実施可":
        raise ItJuusetsuError(409, "実施は『実施可』からのみ。")
    session["state"] = to_state
    session["history"].append((to_state, _now()))
    return session
