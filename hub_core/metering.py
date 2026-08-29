"""M-metering — PRS等の従量利用メータリング＋課金明細（Wave3・収益本命の核）。

「OSは無料・課金はPRSデータ＋金融アタッチ」の課金基盤。利用を記録して期間集計し、
**テストモードの課金明細**を出す。実際の請求（Stripe等での課金発火）は**人間ゲート**で、
このモジュールは実請求を一切発火しない（明細の計算のみ）。

- 利用記録は append-only JSONL（<data_dir>/usage_ledger.jsonl）＝ファイルが正本。
  各行は前行の**HMAC**（別保管の秘密鍵）で連結＝行改竄は検知。末尾削除は seq high-water
  （usage_ledger.hw）で検知。完全なオフホスト・アンカー（両ファイル同時削除への耐性）は繰延。
  金額でなく**利用イベント**を刻む（単価は明細生成時に適用）。
- 単価表（PRICE_BOOK）はコード定数（後で契約単価に差替）。金額は「テストモード試算・確定でない」。
- stdlib only・LLM不使用・ネットワーク非接触。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
LEDGER = "usage_ledger.jsonl"
HIGHWATER = "usage_ledger.hw"   # 末尾削除検知用の単調seq高水位
GENESIS = "0" * 64

# 従量単価（円・テストモード・契約時に差替）。product → 1回あたり単価。
PRICE_BOOK = {
    "prs_assess": 30,        # PRSリスク評価1回
    "prs_assess_full": 50,   # 全種別評価
    "maisoku_generate": 100,  # マイソク生成
    "juusetsu_finalize": 0,   # 重説確定はOS無料
}


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def _norm_tenant(tenant) -> str:
    """テナントキーの統一正規化（F5是正: NFKC+casefold+ゼロ幅除去）。
    'AcmeCorp'/'ＡｃｍｅＣｏｒｐ'(全角)/'Acme​Corp'(ゼロ幅) を同一キーへ＝変種で請求から秘匿できない。"""
    import unicodedata
    s = unicodedata.normalize('NFKC', str(tenant or ''))
    s = ''.join(ch for ch in s if ch not in ('\u200b', '\u200c', '\u200d', '\ufeff'))
    return s.strip().casefold()


def _chain_key() -> bytes:
    """audit.py と同じ別保管の秘密鍵（32byte・chmod600）。鍵無しでは再チェーン偽造不能（F1是正）。"""
    from hub_core.audit import _load_chain_key
    return _load_chain_key()


def _row_hash(prev: str, payload: dict, key: bytes | None = None) -> str:
    body = prev + "|" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    k = key if key is not None else _chain_key()
    return hmac.new(k, body.encode("utf-8"), hashlib.sha256).hexdigest()


def _ledger_path(data_dir) -> Path:
    return Path(data_dir) / LEDGER


def _read_ledger(data_dir) -> list[dict]:
    p = _ledger_path(data_dir)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def record_usage(data_dir, tenant: str, product: str, *, quantity: int = 1,
                 ref: str = "", ts: str | None = None) -> dict:
    """利用イベントを1件追記（append-only・ハッシュ連結）。金額は刻まない（単価は明細時に適用）。
    tenant=課金対象（顧客/店舗ID）、product=PRICE_BOOK のキー相当（未知でも記録はする＝取りこぼさない）。"""
    tenant = _norm_tenant(tenant)
    product = str(product or "").strip()
    if not tenant or not product:
        raise ValueError("tenant と product は必須")
    if int(quantity) <= 0:
        raise ValueError("quantity は正の整数")
    rows = _read_ledger(data_dir)
    key = _chain_key()
    prev = rows[-1]["row_hash"] if rows else GENESIS
    seq = (rows[-1]["seq"] + 1) if rows else 1
    payload = {"seq": seq, "tenant": tenant, "product": product,
               "quantity": int(quantity), "ref": str(ref or ""),
               "ts": ts or _now(), "prev_hash": prev}
    payload["row_hash"] = _row_hash(prev, {k: payload[k] for k in
                                           ("seq", "tenant", "product", "quantity", "ref", "ts", "prev_hash")}, key)
    with open(_ledger_path(data_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _bump_highwater(data_dir, seq)
    return payload


def _hw_path(data_dir):
    return Path(data_dir) / HIGHWATER


def _bump_highwater(data_dir, seq: int) -> None:
    """seq高水位を単調に記録（下げない）。末尾削除の検知に使う（F1b是正）。"""
    cur = _read_highwater(data_dir)
    if seq > cur:
        _hw_path(data_dir).write_text(str(int(seq)), encoding="utf-8")


def _read_highwater(data_dir) -> int:
    p = _hw_path(data_dir)
    if not p.is_file():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def verify_ledger(data_dir) -> int | None:
    """利用台帳のHMAC連結を検証。破損があれば最初の破損 seq を返す（無ければ None）。
    鍵は別保管のため、台帳に書けても鍵無しでは再チェーン偽造できない（F1是正）。"""
    key = _chain_key()
    prev = GENESIS
    rows = _read_ledger(data_dir)
    for r in rows:
        core = {k: r.get(k) for k in ("seq", "tenant", "product", "quantity", "ref", "ts", "prev_hash")}
        if r.get("prev_hash") != prev or _row_hash(prev, core, key) != r.get("row_hash"):
            return r.get("seq")
        prev = r["row_hash"]
    # 末尾トランケーション検知（F1b是正）: 記録済みhigh-waterより現在の最大seqが小さい=削られた。
    hw = _read_highwater(data_dir)
    max_seq = rows[-1]["seq"] if rows else 0
    if max_seq < hw:
        return hw   # 破損（末尾削除）扱い
    return None


def _in_period(ts: str, start: str | None, end: str | None) -> bool:
    d = (ts or "")[:10]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def billing_statement(data_dir, tenant: str, *, start: str | None = None,
                      end: str | None = None, price_book: dict | None = None) -> dict:
    """テナントの期間課金明細（**テストモード試算・実請求は発火しない**）。
    price_book を渡さなければ PRICE_BOOK（テスト単価）。改竄検知に失敗したら明細を出さない（fail-closed）。"""
    broken = verify_ledger(data_dir)
    if broken is not None:
        raise ValueError(f"利用台帳が破損しています（seq={broken}）。明細を発行しません。")
    pb = price_book or PRICE_BOOK
    tenant = _norm_tenant(tenant)
    lines: dict[str, dict] = {}
    for r in _read_ledger(data_dir):
        if r.get("tenant") != tenant or not _in_period(r.get("ts", ""), start, end):
            continue
        prod = r.get("product", "")
        li = lines.setdefault(prod, {"product": prod, "quantity": 0,
                                     "unit_price": pb.get(prod), "amount": 0, "priced": prod in pb})
        li["quantity"] += int(r.get("quantity", 0))
    total = 0
    for li in lines.values():
        if li["unit_price"] is not None:
            li["amount"] = li["unit_price"] * li["quantity"]
            total += li["amount"]
    unpriced = [p for p, li in lines.items() if not li["priced"]]
    return {
        "tenant": tenant, "start": start, "end": end,
        "lines": sorted(lines.values(), key=lambda x: x["product"]),
        "total": total, "currency": "JPY",
        "mode": "test",   # 実請求は発火しない
        "unpriced_products": unpriced,
        "note": "テストモードの試算です。実際の請求は行いません（課金の発火は人間ゲート）。"
                "単価は暫定で、契約単価で再計算されます。",
    }
