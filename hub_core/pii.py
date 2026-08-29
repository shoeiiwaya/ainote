"""個人情報(PII)検出・赤塗り(Stage0 S0-4)。

自由文(タイトル/理由/詳細 等)に埋め込まれた電話番号・メールを検出して伏字にする。
- 認可層(auth.authorize_rows)が「PII閲覧権の無い viewer(担当)」に対し自由文列へ適用し、
  render_cell の title 全文露出(レッドチームA-2)も値レベルで封じる。
- 取込時スキャン(将来)にも contains_pii を流用できる。

注意: 氏名はパターンが無く確実な機械検出が困難なため本v1では対象外。
氏名レベルの構造分離(pii_*テーブル/customer_id参照化)は Stage2 ハードニングへ(ONLINE_ARCHITECTURE §4)。
電話/メールという「連絡可能な実PII」を最優先で封じる。
"""
from __future__ import annotations

import re

# 日本の電話番号(ハイフン区切り or 0始まり10-11桁) と メール
PHONE_RE = re.compile(r"(?<!\d)(0\d{1,3}-\d{2,4}-\d{3,4}|0\d{9,10})(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
REDACTION = "〔伏字〕"


def contains_pii(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return bool(PHONE_RE.search(text) or EMAIL_RE.search(text))


def redact_text(text):
    """電話/メールを伏字化。文字列以外・該当なしはそのまま返す。"""
    if not isinstance(text, str) or not text:
        return text
    text = PHONE_RE.sub(REDACTION, text)
    text = EMAIL_RE.sub(REDACTION, text)
    return text
