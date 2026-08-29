"""M-contract — 契約台帳・期限管理（Wave4 Tier2・更新期限アラート・¥0）。

賃貸借・保証委託・火災保険等の契約を台帳化し、更新期限が近い契約を検出する（管理業務の中核）。
- 期限判定の基準日は呼出側が渡す（today）＝決定的・テスト可能。
- 実際の更新手続き・通知送付は M-sender の人間ゲートを通る（ここは検出と台帳のみ）。
- stdlib のみ・ネットワーク非接触。
"""
from __future__ import annotations

import datetime as _dt

CONTRACT_TYPES = ("賃貸借", "保証委託", "火災保険", "売買", "管理委託", "その他")


def days_until(end_date: str, today: str) -> int | None:
    """終了日までの残日数（today基準）。不正日付は None。"""
    try:
        e = _dt.date.fromisoformat((end_date or "")[:10])
        t = _dt.date.fromisoformat((today or "")[:10])
        return (e - t).days
    except ValueError:
        return None


def expiring_contracts(contracts: list[dict], *, today: str, within_days: int = 60) -> list[dict]:
    """更新期限が within_days 日以内（または既に超過）の契約を残日数昇順で返す。
    完了/解約済みは除外。各要素に days_left を付与。"""
    out = []
    for c in contracts or []:
        status = (c.get("status") or "").strip()
        if status in ("解約", "完了", "終了"):
            continue
        d = days_until(c.get("end_date"), today)
        if d is None:
            continue
        if d <= within_days:
            out.append({**c, "days_left": d})
    out.sort(key=lambda x: x["days_left"])
    return out


def renewal_note(contract: dict) -> str:
    """更新案内の下書き（誇大表現なし・人間が校正）。"""
    ct = contract.get("contract_type") or "契約"
    prop = contract.get("property_name") or ""
    end = contract.get("end_date") or ""
    auto = str(contract.get("auto_renewal") or "").strip() in ("1", "true", "はい", "自動更新")
    base = f"{prop}の{ct}が{end}に満了予定です。"
    if auto:
        return base + "自動更新の対象です。条件の変更がなければ更新手続きを進めます。"
    return base + "更新のご意向を確認のうえ、更新手続きのご案内をいたします。"
