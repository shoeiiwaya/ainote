"""M-analytics — 媒体別実績・活動サマリ・媒介活動報告（Wave1 #9・FAC-12/13, PRO-13/15）。

台帳の**実データのみ**を集計する（数値は捏造しない・LLM不使用・stdlib only）:
- 媒体別実績: portal_leads を媒体でグルーピング→反響数、うち成約（cases.status が成約系）、成約率。
  ※広告費データを持たない（PRSゼロ円マーケ方針）ため ROI（費用対効果）は算出しない。
  spend を明示的に与えられた媒体のみ CPA（反響単価）を出す（それ以外は「費用データなし」）。
- 活動サマリ: contact_log を期間・チャネルで集計（追客の可視化）。
- 媒介活動報告: 1案件の反響数・内見数・掲載媒体・期間を売主報告向けに束ねる（媒介契約の
  法定報告=専任は2週に1回以上／専属専任は1週に1回以上。頻度判定のヒントを添える）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from .store import SqliteStore

JST = timezone(timedelta(hours=9))
_SOLD = ("成約", "契約", "決済", "完了", "クローズ")


def _store(data_dir):
    db = Path(data_dir) / "hub.db"
    return SqliteStore(db) if db.exists() else None


def _rows(st, table, where=None, args=()):
    if st is None:
        return []
    try:
        return st.query(table, where, args)
    except Exception:
        return []


def media_performance(data_dir, *, spend: dict | None = None) -> list[dict]:
    """媒体別実績。spend={媒体: 円} を渡した媒体のみ CPA を算出（他は None＝費用不明）。
    返り: [{media, leads, conversions, conv_rate, spend, cpa}]（反響数降順）。"""
    st = _store(data_dir)
    leads = _rows(st, "portal_leads")
    cases = _rows(st, "cases")
    # 反響ID → 成約かどうか（cases.source_ref が反響IDを指す）
    sold_refs = {c.get("source_ref") for c in cases
                 if (c.get("status") or "").strip() in _SOLD and c.get("source_ref")}
    agg: dict[str, dict] = {}
    for L in leads:
        media = (L.get("platform_id") or "不明").strip() or "不明"
        a = agg.setdefault(media, {"media": media, "leads": 0, "conversions": 0})
        a["leads"] += 1
        if L.get("portal_lead_id") in sold_refs:
            a["conversions"] += 1
    out = []
    spend = spend or {}
    for media, a in agg.items():
        rate = (a["conversions"] / a["leads"]) if a["leads"] else 0.0
        sp = spend.get(media)
        cpa = (int(sp / a["leads"]) if (sp and a["leads"]) else None)
        out.append({**a, "conv_rate": round(rate, 4),
                    "spend": (int(sp) if sp is not None else None), "cpa": cpa})
    out.sort(key=lambda x: (-x["leads"], x["media"]))
    return out


def activity_summary(data_dir, *, days: int = 30, today: str | None = None) -> dict:
    """contact_log を直近 days 日で集計（チャネル別・担当別）。today=基準日（テスト注入用）。"""
    st = _store(data_dir)
    logs = _rows(st, "contact_log")
    base = datetime.fromisoformat(today).date() if today else datetime.now(JST).date()
    since = base - timedelta(days=days)
    by_channel: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    total = 0
    for r in logs:
        occ = (r.get("occurred_at") or "")[:10]
        try:
            d = datetime.fromisoformat(occ).date() if occ else None
        except ValueError:
            d = None
        if d is None or d < since or d > base:
            continue
        total += 1
        by_channel[r.get("channel") or "不明"] = by_channel.get(r.get("channel") or "不明", 0) + 1
        by_actor[r.get("actor") or "不明"] = by_actor.get(r.get("actor") or "不明", 0) + 1
    return {"days": days, "total": total,
            "by_channel": dict(sorted(by_channel.items(), key=lambda x: -x[1])),
            "by_actor": dict(sorted(by_actor.items(), key=lambda x: -x[1]))}


def mediation_report(data_dir, case_id: str, *, today: str | None = None) -> dict:
    """1案件の媒介活動報告データ（売主報告向け）。反響数・内見数・掲載媒体・活動期間。
    法定報告頻度（専任=2週に1回以上／専属専任=1週に1回以上）の判定ヒントを添える。"""
    st = _store(data_dir)
    cases = _rows(st, "cases", "case_id = ?", (case_id,))
    if not cases:
        return {"case_id": case_id, "found": False}
    case = cases[0]
    # この案件に紐づく反響（source_ref or property_ref 経由）
    prop = case.get("property_id") or ""
    leads = [L for L in _rows(st, "portal_leads")
             if L.get("property_ref") == prop and prop]
    media = sorted({(L.get("platform_id") or "不明") for L in leads})
    # 内見イベント（events.event_type=viewing 相当・無ければ0）
    viewings = _rows(st, "events", "case_id = ? AND event_type = ?", (case_id, "viewing"))
    contacts = _rows(st, "contact_log", "case_id = ?", (case_id,))
    dates = [(c.get("occurred_at") or "")[:10] for c in contacts if c.get("occurred_at")]
    dates = sorted(d for d in dates if d)
    return {
        "case_id": case_id, "found": True,
        "property": case.get("property_name") or prop,
        "customer": case.get("customer_name") or "",
        "leads": len(leads), "viewings": len(viewings),
        "media": media, "contacts": len(contacts),
        "period_from": dates[0] if dates else "", "period_to": dates[-1] if dates else "",
        "note": "媒介活動報告の素材（実台帳集計）。報告頻度は媒介種別（専任=2週/専属専任=1週）"
                "に応じて確認。数値は台帳の実績で、成約を保証するものではありません。",
    }


# 転換ファネルの段階（集約5段階を反響→内見→申込→契約→管理の順で）。
_FUNNEL_STAGES = ["反響", "内見", "申込", "契約", "管理"]


def conversion_funnel(data_dir) -> dict:
    """反響→内見→申込→契約の転換ファネル。各段階の到達件数と、直前段階からの転換率。
    到達=その段階以降にある案件（前進すると前段階もカウント＝ファネル単調性を保証）。数値は台帳実績のみ。"""
    from hub_core.operations import aggregate_status
    st = _store(data_dir)
    cases = _rows(st, "cases")
    # 各案件の現在の集約段階のindex
    idx_of = {s: i for i, s in enumerate(_FUNNEL_STAGES)}
    reached = [0] * len(_FUNNEL_STAGES)
    for c in cases:
        agg = aggregate_status((c.get("status") or "").strip())
        ci = idx_of.get(agg)
        if ci is None:
            continue
        for i in range(ci + 1):   # その段階以前は全て「到達」（単調）
            reached[i] += 1
    stages = []
    for i, s in enumerate(_FUNNEL_STAGES):
        prev = reached[i - 1] if i > 0 else reached[i]
        rate = (reached[i] / prev) if (i > 0 and prev) else (1.0 if reached[i] else 0.0)
        stages.append({"stage": s, "count": reached[i],
                       "rate_from_prev": round(rate, 4) if i > 0 else None})
    return {"total": len(cases), "stages": stages}


def income_summary(data_dir) -> dict:
    """請求台帳から収支サマリを集計（管理業務・青色申告前段）。数値は台帳実績のみ。
    返り: {billed(請求合計), collected(消込済合計), outstanding(未回収), by_kind, cases}。"""
    st = _store(data_dir)
    try:
        bills = _rows(st, "billing_register")
    except Exception:
        bills = []

    def _amt(b):
        s = (str(b.get("amount") or "0").replace(",", "").replace("，", "")
             .replace("円", "").replace(" ", ""))
        s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        try:
            v = int(s)
        except ValueError:
            return 0
        return max(v, 0)   # 負値は0扱い（収支の不変条件 collected<=billed を守る）

    billed = collected = 0
    by_kind = {}
    for b in bills:
        kind = (b.get("kind") or "請求").strip()
        a = _amt(b)
        by_kind[kind] = by_kind.get(kind, 0) + a
        if kind == "請求":
            billed += a
            if (b.get("status") or "").strip() in ("消込済", "入金消込"):
                collected += a
    collected = min(collected, billed)   # 回収は請求を超えない（表示の不変条件）
    return {
        "billed": billed, "collected": collected, "outstanding": max(billed - collected, 0),
        "collection_rate": round(collected / billed, 4) if billed else 0.0,
        "by_kind": by_kind, "count": len(bills),
    }


def lost_summary(data_dir) -> dict:
    """失注理由の分析(理由別・失注時ステージ別・deal_type別)。lost_records の実行行のみ集計
    (ゼロ埋め・推定をしない)。理由の蓄積が仕入れ・掲載改善と自社動線改善の源泉。"""
    st = _store(data_dir)
    try:
        rows = _rows(st, "lost_records")
    except Exception:
        rows = []

    def _count(key):
        c = {}
        for r in rows:
            k = (str(r.get(key) or "")).strip() or "(未記録)"
            c[k] = c.get(k, 0) + 1
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))

    return {"total": len(rows), "by_reason": _count("lost_reason"),
            "by_stage": _count("lost_stage"), "by_deal": _count("deal_type")}
