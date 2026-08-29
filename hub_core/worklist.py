"""ワークリスト（Wave1 M-ledger・KAS-08型「今週アプローチ」）。

台帳から「今週、人が手を動かすべきこと」を決定的ルールで抽出して1本のリストにする。
- ルールベース（LLM不使用・全ローカル・数値は台帳の実データのみ＝捏造しない）
- スヌーズ中のタスクは出さない（operations.snoozed_task_ids・クエリ時自然失効）
- 各行に「なぜ出したか」の理由文字列を必ず付ける（黙って並べない）

優先順位（上から）:
  0. P0 の未完了タスク（Hold含む＝止まっている売上/法令リスク）
  1. 一次返信が済んでいない反響（機会損失が時間で腐る）
  2. P1 の Today タスク
  3. 反響ステージのまま接触記録が無い案件（追客漏れ）
"""
from __future__ import annotations

from pathlib import Path

from .labels import jp as _jp_enum
from .operations import snoozed_task_ids
from .store import SqliteStore


def _store(data_dir):
    return SqliteStore(Path(data_dir) / "hub.db")


def build_worklist(data_dir, limit: int = 12, today: str | None = None) -> list[dict]:
    """今週アプローチすべき行を優先度順に返す。各行= {rank, kind, title, ref, who, reason, link}。
    hub.db が無い環境（CSVフォールバック運用）では空を返す＝**勝手に空dbを作って
    load_source のCSVフォールバックを毒さない**。"""
    if not (Path(data_dir) / "hub.db").exists():
        return []
    st = _store(data_dir)
    snoozed = snoozed_task_ids(data_dir, today)
    rows: list[dict] = []

    def _open_tasks():
        try:
            return [r for r in st.query("tasks")
                    if (r.get("status") or "").strip() not in ("done", "解決")]
        except Exception:
            return []

    tasks = [r for r in _open_tasks() if r.get("task_id") not in snoozed]

    # 0/2: P0全部 → P1のToday。副題は定型反復でなく行ごとの個別理由（日本語）に情報を持たせる
    def _task_reason(r, prio: str) -> str:
        hr = (r.get("hold_reason") or "").strip()
        if hr:
            return "止まっている理由: " + _jp_enum(hr)
        gate = (r.get("gate") or "").strip()
        if gate:
            return _jp_enum(gate) + "ゲートの対応待ち"
        return "今日のキュー" if prio == "P1" else "要対応"

    for prio, rank in (("P0", 0), ("P1", 2)):
        for r in tasks:
            if (r.get("priority") or "") != prio:
                continue
            if prio == "P1" and (r.get("queue") or "") != "Today":
                continue
            ref = r.get("property_id") or r.get("portal_lead_id") or ""
            rows.append({
                "rank": rank, "kind": "task",
                "title": r.get("title") or "(無題タスク)",
                "ref": ref, "who": r.get("assignee") or "",
                "reason": _task_reason(r, prio),
                "link": ("/case?id=" + ref) if ref else "/today",
                "task_id": r.get("task_id") or "",
            })

    # 1: 一次返信が済んでいない反響（reply_gate が空/cleared 以外 = pending系）
    try:
        leads = st.query("portal_leads")
    except Exception:
        leads = []
    converted = set()
    try:
        converted = {c.get("source_ref") for c in st.query("cases") if c.get("source_ref")}
    except Exception:
        pass
    for r in leads:
        gate = (r.get("reply_gate") or "").strip().lower()
        if gate in ("", "cleared", "sent", "done"):
            continue
        lid = r.get("portal_lead_id") or ""
        if lid in converted:
            continue
        rows.append({
            "rank": 1, "kind": "lead",
            "title": "一次返信: " + (r.get("customer_name") or lid or "反響"),
            "ref": lid, "who": "",
            "reason": f"未返信の反響（{r.get('platform_id') or '経路不明'}・{r.get('received_at') or '受信時刻不明'}）",
            "link": "/leads", "task_id": "",
        })

    # 3: 反響ステージのまま接触記録ゼロの案件（追客漏れ）
    try:
        cases = st.query("cases", "status = ?", ("反響",))
    except Exception:
        cases = []
    for c in cases:
        cid = c.get("case_id") or ""
        try:
            touched = st.query("contact_log", "case_id = ?", (cid,), limit=1)
        except Exception:
            touched = []
        if touched:
            continue
        rows.append({
            "rank": 3, "kind": "case",
            "title": "追客: " + (c.get("customer_name") or cid),
            "ref": cid, "who": "",
            "reason": "反響ステージのまま接触記録なし",
            "link": "/case?id=" + cid, "task_id": "",
        })

    rows.sort(key=lambda r: (r["rank"], r["title"]))
    return rows[:limit]
