"""M-keisan — 不動産取引の費用計算エンジン（Wave1 #9・PRO-06）。

純関数・stdlib only・LLM不使用・全ローカル。すべて**参考値**であり確定額ではない
（金額・税額は士業/金融機関で要確認＝理ツール標準の「確定しない」規律）:
- 仲介手数料は宅建業法の報酬告示に基づく**上限**（それ以下で合意可能）。
- 印紙税は金額帯のテーブル。軽減措置は時限のため `stamp_table` を差し替え可能にし、
  既定は本則。適用可否・期限は個別確認前提（過大主張しない）。
- ローン返済は元利均等の数式（金融機関の実条件で変わる参考値）。
- 諸費用概算は慣行係数レンジ（実額でない・目安）。

金額はすべて円・整数（四捨五入でなく法令慣行に沿い切り上げ/切り捨てを関数ごとに明記）。
"""
from __future__ import annotations

import math

CONSUMPTION_TAX = 0.10  # 消費税率（報酬・手数料にかかる）


def brokerage_fee_cap(price: int, *, tax: float = CONSUMPTION_TAX) -> dict:
    """媒介報酬の**法定上限**（宅建業法46条・報酬告示）。売買代金 price（税抜・円）に対し:
      200万円以下      : 5%
      200万超400万以下 : 4% + 2万円
      400万円超        : 3% + 6万円
    に消費税を加える。※これは上限であり、実際の報酬はこれ以下で合意される。
    2024年施行の低廉空家特例（800万円以下=最大30万円+税）は別途 low_value_cap で。"""
    p = int(price)
    if p < 0:
        raise ValueError("price は0以上")
    if p <= 2_000_000:
        base = p * 0.05
    elif p <= 4_000_000:
        base = p * 0.04 + 20_000
    else:
        base = p * 0.03 + 60_000
    with_tax = base * (1 + tax)
    return {
        "price": p,
        "fee_cap_excl_tax": int(base),
        "fee_cap_incl_tax": int(with_tax),
        "note": "宅建業法の報酬告示に基づく上限（これ以下で合意可）。確定額ではない。",
    }


def low_value_brokerage_cap(price: int, *, tax: float = CONSUMPTION_TAX,
                            ceiling_excl_tax: int = 300_000) -> dict:
    """低廉な空家等（売買代金800万円以下）の媒介報酬の上限特例。
    通常上限と ceiling_excl_tax（既定30万円）の**高い方**まで請求可能（現地調査費等を含む）。
    適用は要件（800万円以下・空家等・事前説明合意）を満たす場合のみ＝可否は個別確認。"""
    p = int(price)
    normal = brokerage_fee_cap(p, tax=tax)["fee_cap_excl_tax"]
    cap = max(normal, int(ceiling_excl_tax))
    return {
        "price": p,
        "eligible_hint": p <= 8_000_000,
        "fee_cap_excl_tax": cap,
        "fee_cap_incl_tax": int(cap * (1 + tax)),
        "note": "低廉空家等の特例上限（要件充足時のみ・事前説明合意が前提）。確定でない。",
    }


# 印紙税（不動産譲渡契約書・本則）: (契約金額上限, 税額)。上限None=それ超。
# 軽減措置は時限のため既定は本則。呼出側で軽減テーブルを渡せる。
STAMP_TABLE_HONSOKU = [
    (10_000, 0), (500_000, 400), (1_000_000, 1_000), (5_000_000, 2_000),
    (10_000_000, 10_000), (50_000_000, 20_000), (100_000_000, 60_000),
    (500_000_000, 100_000), (1_000_000_000, 200_000), (5_000_000_000, 400_000),
    (None, 600_000),
]


def stamp_duty(price: int, *, table=None) -> dict:
    """不動産譲渡契約書の印紙税（既定=本則テーブル）。price=契約金額（円）。
    軽減措置は時限適用のため、適用する場合は軽減テーブルを table で渡す。"""
    p = int(price)
    tbl = table or STAMP_TABLE_HONSOKU
    for upper, amount in tbl:
        if upper is None or p <= upper:
            return {"price": p, "stamp_duty": int(amount),
                    "note": "本則。軽減措置は時限適用のため別途確認（table差替可）。"}
    return {"price": p, "stamp_duty": int(tbl[-1][1]), "note": "本則。"}


def loan_monthly_payment(principal: int, annual_rate: float, years: int) -> dict:
    """元利均等返済の月々返済額（円・切り上げ）。annual_rate=年利（0.015=1.5%）。
    参考値（金融機関の実条件・保証料・団信で変動）。"""
    P = int(principal)
    n = int(years) * 12
    if P <= 0 or n <= 0:
        raise ValueError("principal と years は正")
    r = float(annual_rate) / 12.0
    if r == 0:
        monthly = P / n
    else:
        monthly = P * r * (1 + r) ** n / ((1 + r) ** n - 1)
    monthly = math.ceil(monthly)
    total = monthly * n
    return {
        "principal": P, "annual_rate": annual_rate, "years": int(years),
        "monthly_payment": int(monthly),
        "total_payment": int(total),
        "total_interest": int(total - P),
        "note": "元利均等の参考値。保証料・団信・手数料は含まず。金融機関で要確認。",
    }


# 購入諸費用の慣行係数（売買代金に対する概算レンジ・目安）。実額ではない。
BUYER_COST_RATE = {"中古": (0.06, 0.09), "新築": (0.03, 0.07)}


def buyer_cost_estimate(price: int, *, kind: str = "中古") -> dict:
    """購入時諸費用の**概算レンジ**（仲介手数料・登記・ローン・保険・税等の合計目安）。
    慣行係数によるざっくり見積であり実額でない（内訳は別途積上げ）。"""
    p = int(price)
    lo, hi = BUYER_COST_RATE.get(kind, BUYER_COST_RATE["中古"])
    return {
        "price": p, "kind": kind,
        "estimate_low": int(p * lo), "estimate_high": int(p * hi),
        "rate_low": lo, "rate_high": hi,
        "note": "慣行係数による概算レンジ（目安）。実額は内訳の積上げで算出のこと。",
    }


def summary(price: int, *, kind: str = "中古", annual_rate: float = 0.015,
            years: int = 35, down_payment: int = 0) -> dict:
    """1画面向けの参考サマリ（上限手数料・印紙・諸費用概算・ローン月々）。すべて参考。"""
    p = int(price)
    loan_principal = max(0, p - int(down_payment))
    out = {
        "price": p, "kind": kind,
        "brokerage": brokerage_fee_cap(p),
        "stamp": stamp_duty(p),
        "buyer_cost": buyer_cost_estimate(p, kind=kind),
        "loan": (loan_monthly_payment(loan_principal, annual_rate, years)
                 if loan_principal > 0 else None),
        "disclaimer": "全て参考値・確定額ではありません。税額・報酬・ローン条件は"
                      "士業／金融機関で必ず確認してください。",
    }
    return out
