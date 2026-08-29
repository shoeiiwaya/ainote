"""M-followup — 追客エンジン（Wave4 Tier2・ステージ×経過日でドラフト自動生成・¥0）。

反響〜契約の各段階で「一定日数コンタクトが無い顧客」を検出し、追客メッセージの**ドラフトを
自動生成**する（KASIKA型の長期追客）。
- 生成はドラフト止まり（可逆）＝実送信は M-sender の人間ゲートを通る。自動で送らない。
- ルールベース（LLM不使用・決定的・台帳の実データのみ）。文面テンプレは差替え可能。
- 経過日の基準日は呼出側が渡す（today）＝決定的・テスト可能。stdlib のみ。
"""
from __future__ import annotations

from pathlib import Path

from hub_core.store import SqliteStore

# ステージ別の追客間隔（日）と定型文テンプレ。実運用で文面は差替え。
FOLLOWUP_RULES = {
    "反響": (2, "{name}様　お問い合わせありがとうございます。ご希望の条件を伺えればお物件をご提案します。"),
    "追客": (5, "{name}様　その後ご状況いかがでしょうか。新着のお物件が出ています。"),
    "内見": (3, "{name}様　先日の内見はいかがでしたか。ご不明点があればお知らせください。"),
    "申込": (2, "{name}様　お申込ありがとうございます。審査状況をご案内します。"),
}


def _store(data_dir):
    return SqliteStore(Path(data_dir) / "hub.db")


def _days_between(a: str, b: str) -> int:
    """YYYY-MM-DD 同士の日数差（b - a）。不正は大きい値（追客対象化しない側に倒さない=検出優先）。"""
    import datetime as _dt
    try:
        da = _dt.date.fromisoformat((a or "")[:10])
        db = _dt.date.fromisoformat((b or "")[:10])
        return (db - da).days
    except ValueError:
        return 10 ** 6   # 不正日付は検出優先＝大きい番兵値（追客対象から恒久脱落させない）


def due_followups(data_dir, *, today: str) -> list[dict]:
    """追客ドラフトを出すべき案件のリスト。各= {case_id, customer, stage, days_since, draft_body}。
    最終接触（contact_log）からステージ別間隔を超え、かつ完了系でない案件。"""
    st = _store(data_dir)
    try:
        cases = st.query("cases")
    except Exception:
        return []
    out = []
    for c in cases:
        stage = (c.get("status") or "").strip()
        rule = FOLLOWUP_RULES.get(stage)
        if rule is None:
            continue
        interval, template = rule
        cid = c.get("case_id") or ""
        # 最終接触日
        try:
            logs = st.query("contact_log", "case_id = ?", (cid,))
        except Exception:
            logs = []
        last = max((str(l.get("occurred_at") or "")[:10] for l in logs), default="")
        base = last or (str(c.get("created_at") or "")[:10] if c.get("created_at") else "")
        if not base:
            continue
        days = _days_between(base, today)
        if days < interval:
            continue
        name = c.get("customer_name") or "お客様"
        out.append({
            "case_id": cid, "customer": name, "stage": stage, "days_since": days,
            "draft_body": template.format(name=name),
        })
    out.sort(key=lambda x: -x["days_since"])
    return out
