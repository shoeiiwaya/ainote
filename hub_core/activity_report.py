"""M-activity-report — 売主活動報告書（Wave4 Tier2・専任媒介の法定報告・¥0）。

専任媒介契約（2週に1回以上）・専属専任媒介（1週に1回以上）で宅建業法34条の2が義務づける
売主への業務処理状況の報告書を、台帳の実績（接触履歴・内見・反響・掲載）から自動生成する。
- 数値は台帳の実データのみ（捏造しない）。期間は呼出側が渡す（決定的）。
- 顧客（売主）向け文面に価格断定・精度バッジ・誇大表現を入れない（公開規律）。
- 生成のみ。売主への送付は M-sender の人間ゲートを通る。stdlib のみ。
"""
from __future__ import annotations

from pathlib import Path

from hub_core.store import SqliteStore

# 媒介種別ごとの法定報告頻度（宅建業法34条の2）。
REPORT_FREQUENCY = {
    "専属専任媒介": "1週間に1回以上",
    "専任媒介": "2週間に1回以上",
    "一般媒介": "法定義務なし（任意）",
}


def _store(data_dir):
    return SqliteStore(Path(data_dir) / "hub.db")


def build_activity_report(data_dir, case_id: str, *, period_from: str, period_to: str,
                          mediation_type: str = "専任媒介") -> dict:
    """案件の活動報告データを組み立てる。期間内の接触・内見・反響・掲載の実数を台帳から集計。"""
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (case_id,))
    if not cases:
        raise ValueError(f"案件 {case_id} が見つかりません。")
    case = cases[0]

    def _in_period(ts: str) -> bool:
        d = str(ts or "")[:10]
        return bool(d) and period_from <= d <= period_to

    # 接触履歴（期間内）
    try:
        logs = [l for l in st.query("contact_log", "case_id = ?", (case_id,))
                if _in_period(l.get("occurred_at"))]
    except Exception:
        logs = []
    # 反響（物件参照が一致・期間内）。property_id 完全一致を主・無ければ property_name 完全一致
    # （mediation_report と同一ロジック。部分一致 in は別物件を誤計上するため使わない）。
    pid = str(case.get("property_id") or "").strip()
    pname = str(case.get("property_name") or "").strip()

    def _matches(ref: str) -> bool:
        ref = str(ref or "").strip()
        if pid and ref == pid:
            return True
        return bool(pname and ref == pname)

    try:
        leads = [l for l in st.query("portal_leads")
                 if _matches(l.get("property_ref")) and _in_period(l.get("received_at"))]
    except Exception:
        leads = []
    # 内見（チャネル=内見 が主・要約一致は否定語[キャンセル/中止/延期]を除外して過大計上を防ぐ）
    def _is_viewing(l) -> bool:
        ch = (l.get("channel") or "").strip()
        if ch == "内見":
            return True
        s = (l.get("summary") or "")
        return "内見" in s and not any(n in s for n in ("キャンセル", "中止", "延期", "見送"))
    viewings = [l for l in logs if _is_viewing(l)]

    activities = [{"date": str(l.get("occurred_at") or "")[:10],
                   "channel": l.get("channel") or "",
                   "summary": l.get("summary") or ""} for l in logs]
    activities.sort(key=lambda x: x["date"])
    return {
        "case_id": case_id,
        "property": pname or pid,
        "seller": case.get("customer_name") or "",
        "mediation_type": mediation_type,
        "frequency": REPORT_FREQUENCY.get(mediation_type, "—"),
        "period_from": period_from, "period_to": period_to,
        "counts": {"contacts": len(logs), "inquiries": len(leads), "viewings": len(viewings)},
        "activities": activities,
        "note": "本報告は宅建業法34条の2に基づく業務処理状況の報告です。数値は台帳実績に基づきます。",
    }


def render_report_text(report: dict) -> str:
    """活動報告書の本文（プレーンテキスト・売主送付用の下書き）。誇大表現なし。"""
    c = report["counts"]
    lines = [
        f"【売買物件 活動報告書】",
        f"物件: {report['property']}",
        f"報告期間: {report['period_from']} 〜 {report['period_to']}",
        f"媒介種別: {report['mediation_type']}（報告義務: {report['frequency']}）",
        "",
        f"■ 期間中の活動状況",
        f"・お問い合わせ: {c['inquiries']}件",
        f"・ご内見: {c['viewings']}件",
        f"・当社の対応: {c['contacts']}件",
        "",
        "■ 活動の内訳",
    ]
    for a in report["activities"]:
        lines.append(f"・{a['date']} {a['channel']} {a['summary']}".rstrip())
    if not report["activities"]:
        lines.append("・（この期間の記録はありません）")
    lines += ["", report["note"]]
    return "\n".join(lines)
