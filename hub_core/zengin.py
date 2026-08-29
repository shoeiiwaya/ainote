"""M-money（全銀）— 全銀協フォーマットの入出金明細パース＋案件請求との消し込み（Wave3）。

全国銀行協会の全銀フォーマット（入出金取引明細・固定長120バイト・レコード区分）を自前パースし、
案件の請求（billing_create で作った金額）と突合して消し込む。
- **実送金・実口座接続は一切しない**（ファイル取込のみ）。実銀行フィード接続は人間ゲート。
- 突合は「金額一致＋名義の正規化一致」で自動消し込み、部分一致（金額のみ/名義ゆらぎ）は
  「要確認」に落とす（誤消し込みを避ける＝fail-safe）。金額は確定額として扱わない表現。
- stdlib only・ネットワーク非接触。

全銀フォーマット（入金明細の主要フィールド・簡易版）:
  データレコード（区分'2'）: 金額・振込依頼人名（半角カナ）・入金日 等を固定位置から抽出。
  ※実運用は銀行/勘定系で桁位置が異なるため、桁位置は BANK_LAYOUT で差替可能にする。
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# 全銀データレコード（区分2）の桁位置（0始まり[start:end]）。銀行により差異あり=差替可能。
DEFAULT_LAYOUT = {
    "record_type": (0, 1),      # '1'ヘッダ '2'データ '8'トレーラ '9'エンド
    "amount": (48, 58),          # 取引金額（10桁・円・ゼロ埋め）
    "payer_name": (58, 106),     # 振込依頼人名（半角カナ48桁）
}


def _z2h_kana_normalize(s: str) -> str:
    """名義正規化: 全角/半角カナ・記号・空白を吸収して比較キーにする（名寄せの弱版）。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"[\s　・.,-]", "", s)
    # 株式会社/(株)/カ) 等の会社種別表記を除去
    s = re.sub(r"(株式会社|有限会社|\(株\)|\(有\)|カ\)|ﾕｳ\)|ｶ\))", "", s)
    return s.upper()


def parse_zengin(text: str, *, layout: dict | None = None) -> list[dict]:
    """全銀テキスト（改行区切りの固定長行）→ 入金データレコードのリスト。
    区分'2'の行のみ抽出。金額は整数（円）、payer は原文＋正規化キーを持つ。"""
    lay = layout or DEFAULT_LAYOUT
    out = []
    for i, line in enumerate((text or "").splitlines()):
        if not line:
            continue
        rt = line[lay["record_type"][0]:lay["record_type"][1]]
        if rt != "2":
            continue
        a0, a1 = lay["amount"]
        p0, p1 = lay["payer_name"]
        raw_amount = line[a0:a1].strip()
        try:
            amount = int(raw_amount or "0")
        except ValueError:
            amount = 0
        payer = line[p0:p1].strip()
        out.append({"line_no": i + 1, "amount": amount, "payer_raw": payer,
                    "payer_key": _z2h_kana_normalize(payer)})
    return out


def reconcile(deposits: list[dict], invoices: list[dict]) -> dict:
    """入金（parse_zengin結果）と請求（{invoice_id, payer, amount}）を消し込む。
    - matched: 金額一致＋名義正規化一致 → 自動消し込み候補
    - amount_only: 金額は一致するが名義が合わない → 要確認
    - unmatched_deposit / unmatched_invoice: 相手のいない入金/請求
    ※誤消し込みを避けるため name一致だけ・金額違いは matched にしない（fail-safe）。"""
    inv = [{**x, "_key": _z2h_kana_normalize(x.get("payer", "")), "_amt": int(x.get("amount", 0))}
           for x in invoices]
    used_inv = set()
    matched, amount_only, unmatched_dep = [], [], []
    for d in deposits:
        # 名義キーが空/短すぎ（会社種別のみ等）は同定不能＝自動消し込みしない（F2是正）
        dep_key_ok = len(d["payer_key"]) >= 2
        # F2c是正: list.index()は値ベースで重複invoiceで先頭を返す→enumerate の idx を保持する
        exact_idx = next((idx for idx, i in enumerate(inv)
                          if idx not in used_inv and i["_amt"] == d["amount"]
                          and dep_key_ok and len(i["_key"]) >= 2 and i["_key"] == d["payer_key"]), None)
        if exact_idx is not None:
            used_inv.add(exact_idx)
            ei = inv[exact_idx]
            matched.append({"invoice_id": ei.get("invoice_id"), "amount": d["amount"],
                            "payer": d["payer_raw"]})
            continue
        amt_idx = next((idx for idx, i in enumerate(inv)
                        if idx not in used_inv and i["_amt"] == d["amount"]), None)
        if amt_idx is not None:
            used_inv.add(amt_idx)
            ai = inv[amt_idx]
            amount_only.append({"invoice_id": ai.get("invoice_id"), "amount": d["amount"],
                                "payer": d["payer_raw"], "invoice_payer": ai.get("payer"),
                                "reason": "金額一致・名義相違（要確認）"})
            continue
        unmatched_dep.append({"amount": d["amount"], "payer": d["payer_raw"]})
    unmatched_inv = [{"invoice_id": i.get("invoice_id"), "amount": i["_amt"], "payer": i.get("payer")}
                     for idx, i in enumerate(inv) if idx not in used_inv]
    return {
        "matched": matched, "amount_only_review": amount_only,
        "unmatched_deposit": unmatched_dep, "unmatched_invoice": unmatched_inv,
        "note": "自動消し込みは金額＋名義一致のみ。金額のみ一致は要確認へ。"
                "入金額は取込値であり、確定・実送金を表すものではありません。",
    }


def load_zengin_file(data_dir, rel_path: str, *, layout: dict | None = None) -> list[dict]:
    """Vault内の全銀ファイルを読んでパース（トラバーサル封じ・is_relative_to）。"""
    root = Path(data_dir).resolve()
    p = (root / rel_path).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise ValueError(f"Vault外/不在のファイル: {rel_path}")
    # 全銀はShift_JISが一般的（半角カナ）。cp932で読み、失敗時utf-8フォールバック。
    try:
        text = p.read_text(encoding="cp932")
    except UnicodeDecodeError:
        text = p.read_text(encoding="utf-8", errors="replace")
    return parse_zengin(text, layout=layout)
