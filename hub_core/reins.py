"""hub_core/reins.py — REINS(指定流通機構)登録の"人力必須"を尊重した準備・期限管理。

なぜ: 専任/専属専任媒介は宅建業法34条の2でREINS登録が**法定義務**。だがREINSは会員制の閉じたシステムで、
ToSがスクレイピング/自動アクセス/データ外部持出を禁じる。**登録操作そのものは必ず人が会員画面で行う**。
重要（2026-07-14 一次ソース検証で訂正）: **REINSには外部向けの公式APIが一切存在せず**（2024-04-25 衆院特別委
の国交省答弁・4機構公式サイトで確認）、**承認ベンダー制度も公開されていない**。**自社物件をREINSへ一括アップロード
登録する公式機能も確認できない**＝REINS登録は基本的に**会員がID/PWでログインして画面に手入力**する。ベンダーの
「レインズ自動出稿」は技術方式が非開示（規約は特殊プログラムによるアクセスを原則禁止）。

本モジュールは**REINSに一切アクセスしない**。あいのて側で:
- 入稿シート生成: 物件レコードからREINS登録項目を決定論生成＝**人が会員画面へ手入力する時の転記元**（再入力を減らす）。
- 法定登録期限: 媒介契約種別から期限（専任=契約日から7営業日/専属専任=5営業日）を算出しタスク・アラートに。
- 登録番号の記録: 人がREINS登録後、REINS番号をあいのてに記録し案件/マイソク/重説と紐付け（登録済管理）。
REINSデータの取得/転載はしない（ToS遵守・成約情報は会員限定）。公式APIや承認ベンダー制度は存在しない。
祝日は営業日計算に含めない（土日のみ考慮・祝日は人が確認＝期限を過小に断定しない）。stdlib only・捏造しない。
"""
from __future__ import annotations

import datetime

# 媒介契約種別 → REINS法定登録期限（営業日）。一般媒介は登録義務なし（任意）。
REINS_MEDIATION_DAYS = {"専属専任媒介": 5, "専任媒介": 7}

# REINS登録の標準項目（物件種目で増減。流通機構の実入稿テンプレ/CSVは会員が提供する前提）。
_REINS_FIELDS = [
    ("torihiki_taiyo", "取引態様"),
    ("baikai", "媒介契約種別"),
    ("property_type", "物件種目"),
    ("property_name", "物件名称"),
    ("address", "所在地"),
    ("transit", "交通（最寄駅・徒歩分）"),
    ("price", "価格"),
    ("land_area", "土地面積"),
    ("area", "建物／専有面積"),
    ("layout", "間取り"),
    ("built", "築年月"),
    ("structure", "構造・階建"),
    ("floor", "所在階"),
    ("youto", "用途地域"),
    ("kenpei_yoseki", "建ぺい率／容積率"),
]


def business_days_after(start_iso: str, days: int) -> str:
    """開始日（YYYY-MM-DD）から days 営業日後の日付。土日を飛ばす。
    ⚠️ 祝日は考慮しない（祝日があれば実際の期限は前倒し＝人が最終確認する前提・期限を過小断定しない）。"""
    try:
        d = datetime.date.fromisoformat((start_iso or "").strip())
    except (ValueError, TypeError):
        return ""
    added = 0
    while added < days:
        d = d + datetime.timedelta(days=1)
        if d.weekday() < 5:   # 月〜金
            added += 1
    return d.isoformat()


def reins_deadline(mediation: str, contract_date: str) -> dict:
    """媒介契約種別＋契約日 → REINS法定登録期限。一般媒介は義務なし。"""
    m = (mediation or "").strip()
    days = REINS_MEDIATION_DAYS.get(m)
    if days is None:
        return {"required": False, "note": "一般媒介はREINS登録義務なし（任意）。専任/専属専任は義務。"}
    deadline = business_days_after(contract_date, days)
    return {"required": True, "mediation": m, "days": days, "deadline": deadline,
            "note": f"{m}は契約日から{days}営業日以内にREINS登録が法定義務（宅建業法34条の2）。"
                    "祝日は未考慮のため実期限は前倒しの可能性。登録は会員が手動で行う。"}


def build_reins_sheet(fields: dict, *, mediation: str = "", contract_date: str = "",
                      today: str = "") -> str:
    """物件レコード → REINS入稿シート（markdown）。人が会員画面へ手入力する時の転記元。
    REINSには送信しない・必須項目の欠落は ☐ で残す（金メッキしない）・値は担当が確認。"""
    f = fields or {}
    dl = reins_deadline(mediation, contract_date)
    lines = [
        "# REINS 入稿シート（登録準備・会員が手入力）", "",
        "> 本シートは REINS 登録の**転記元**です。REINSへは送信していません（公式APIも一括入稿機能も無い）。"
        "宅地建物取引業者（会員）が REINS の会員画面にログインして手入力してください（スクレイピング・自動登録はしません）。", "",
        f"- 作成日: {today or '（　年　月　日）'}",
        f"- 媒介契約種別: {mediation or '（専任／専属専任／一般）'}",
    ]
    if dl.get("required"):
        lines.append(f"- **REINS登録期限（法定）: {dl.get('deadline') or '（契約日を入力）'}**（{dl['note']}）")
    elif mediation:
        lines.append(f"- {dl.get('note','')}")
    lines.append("")
    lines.append("## 登録項目")
    for key, label in _REINS_FIELDS:
        v = str(f.get(key) or "").strip()
        lines.append(f"- {label}：**{v}**" if v else f"- ☐ {label}")
    lines.append("")
    lines.append("## 会員による登録")
    lines.append("- ☐ REINS会員画面にログインし、上記を手入力して登録（公式API・一括入稿機能は無い）")
    lines.append("- ☐ 登録後、REINS物件番号をあいのてに記録（reins_record）して案件・マイソク・重説と紐付け")
    return "\n".join(lines)
