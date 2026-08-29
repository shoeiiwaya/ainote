"""両方向マッチングの単一正本(CRMギャップp1・2026-07-30)。

顧客→物件(match_properties)と物件→顧客(match_customers)が同じ重み・同じ判定で動く。
重み定数はこのモジュールだけに定義する(serve.py側に重複実装を残さない=乖離防止)。
NG条件(customer_attributes field_key="物件NG")は減点でなく除外(除外理由が返る)。
"""
from __future__ import annotations

import re
from pathlib import Path

# 重み(与信→地理→スペックの優先順位・設計§5)。ここが唯一の定義場所。
AREA_W = 3
BUDGET_W = 3
BUDGET_NEAR_W = 1
BUDGET_NEAR_RATIO = 1.05   # 予算の5%超過までは「ほぼ内」
LAYOUT_W = 2
TOP_N = 3                  # 先行提案候補は上位3件
NG_FIELD_KEY = "物件NG"    # 例: 坂道;浸水;1階(トークン列)

_SPLIT = r"[／/、,;；\s]+"


def _num(s) -> float:
    m = re.search(r"[\d,\.]+", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else 0


def _prop_hay(prop: dict) -> str:
    """NG・エリア照合に使う物件側の文字面(所在地+駅+間取り+構造+ペット)。"""
    return "".join(str(prop.get(k) or "") for k in
                   ("address", "station", "layout", "structure", "pet"))


def _norm(s) -> str:
    """NFKC正規化+小文字化。全角NG「１階」と半角物件「1階」の素通りを防ぐ(敵対verify 2026-07-30指摘)。"""
    import unicodedata
    return unicodedata.normalize("NFKC", str(s or "")).lower()


def ng_tokens(attrs: dict) -> list:
    """顧客属性から物件NGトークン列(2文字以上・NFKC正規化済み)を取り出す。"""
    raw = attrs.get(NG_FIELD_KEY, "") or ""
    return [_norm(t.strip()) for t in re.split(_SPLIT, raw) if len(t.strip()) >= 2]


def ng_hit(attrs: dict, prop: dict) -> str:
    """NGトークンが物件の文字面に当たれば最初の該当トークンを返す(無ければ"")。
    数字始まりトークンは直前が数字でないことを要求(「1階」が「31階建」に誤爆する部分一致バグの封じ)。"""
    hay = _norm(_prop_hay(prop))
    for tok in ng_tokens(attrs):
        if not tok:
            continue
        if tok[0].isdigit():
            if re.search(r"(?<!\d)" + re.escape(tok), hay):
                return tok
        elif tok in hay:
            return tok
    return ""


def score_pair(attrs: dict, prop: dict):
    """顧客属性×物件のスコアと理由。両方向で共通(単一正本)。"""
    score, reasons = 0, []
    area = attrs.get("希望エリア", "") or ""
    hay = (prop.get("station") or "") + (prop.get("address") or "")
    for tok in re.split(_SPLIT, area):
        tok = tok.strip().replace("駅", "").replace("徒歩", "").replace("分", "")
        if len(tok) >= 2 and tok in hay:
            score += AREA_W
            reasons.append("エリア")
            break
    budget = _num(attrs.get("予算上限(家賃)") or attrs.get("予算上限") or attrs.get("予算上限(家賃/価格)") or "")
    price = _num(prop.get("rent_or_price") or "")
    if budget and price:
        if price <= budget:
            score += BUDGET_W
            reasons.append("予算内")
        elif price <= budget * BUDGET_NEAR_RATIO:
            score += BUDGET_NEAR_W
            reasons.append("予算ほぼ内")
    lay = prop.get("layout", "") or ""
    for tok in re.split(r"[\s/]+", attrs.get("希望間取り", "") or ""):
        tok = tok.strip()
        if len(tok) >= 2 and tok in lay:
            score += LAYOUT_W
            reasons.append("間取り")
            break
    return score, reasons


def _store(data_dir):
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    return SqliteStore(db) if db.exists() else None


def _attrs_by_customer(st, customer_id=None):
    where, args = ("customer_id = ?", (customer_id,)) if customer_id else (None, ())
    out = {}
    for a in (st.query("customer_attributes", where, args) or []):
        out.setdefault(a.get("customer_id"), {})[a.get("field_key")] = a.get("field_value")
    return out


def match_properties(data_dir, customer_id, deal_type="", top=TOP_N):
    """顧客→物件: 希望条件で物件をスコアリングし [(prop, reasons)] を返す。
    NG該当物件は除外(スコア0のフォールバック表示にも出さない)。"""
    st = _store(data_dir)
    if st is None:
        return []
    attrs = _attrs_by_customer(st, customer_id).get(customer_id, {})
    scored = []
    for p in (st.query("properties", None) or []):
        if deal_type and p.get("deal_type") and p.get("deal_type") != deal_type:
            continue
        if ng_hit(attrs, p):
            continue   # NGは減点でなく除外
        score, reasons = score_pair(attrs, p)
        scored.append((score, p, reasons))
    scored.sort(key=lambda x: -x[0])
    out = [(p, r) for s, p, r in scored if s > 0][:top]
    return out or [(p, []) for s, p, r in scored[:top]]


def match_customers(data_dir, prop: dict, top=TOP_N):
    """物件→顧客(逆マッチング): 新規物件の登録時に既存顧客の希望条件で逆検索し
    先行提案候補 [{customer_id, customer_name, score, reasons}] を返す(スコア>0のみ・上位top件)。
    NG該当顧客は候補から除外(excluded に理由付きで返す)。"""
    st = _store(data_dir)
    if st is None:
        return {"candidates": [], "excluded": []}
    names = {c.get("customer_id"): c.get("customer_name") or ""
             for c in (st.query("customers", None) or [])}
    candidates, excluded = [], []
    for cid, attrs in _attrs_by_customer(st).items():
        if not cid:
            continue
        tok = ng_hit(attrs, prop)
        if tok:
            excluded.append({"customer_id": cid, "customer_name": names.get(cid, ""),
                             "ng": tok})
            continue
        score, reasons = score_pair(attrs, prop)
        if score > 0:
            candidates.append({"customer_id": cid, "customer_name": names.get(cid, ""),
                               "score": score, "reasons": reasons})
    candidates.sort(key=lambda c: (-c["score"], c["customer_id"]))
    return {"candidates": candidates[:top], "excluded": excluded}
