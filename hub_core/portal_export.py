"""M-portal-export — ポータル掲載書式の生成（Wave4 G5・自動出稿はしない・¥0）。

物件データを各ポータル（SUUMO/LIFULL HOME'S/athome/自社）の CSV 掲載書式に変換して出力する。
- **自動出稿・アップロードはしない**（業者間流通網の自動連携は「追わない」領域）。生成した CSV を
  人間が各ポータルの管理画面へ手動アップロードする＝乗換ゼロ摩擦の入口。
- 書式（列定義）は登録制（PORTAL_FORMATS）。公開情報に基づく**書式定義**であり、実掲載での列較正は
  掲載契約後（inbox パーサと同じ思想）。
- ネットワーク非接触・stdlib（csv）のみ。
"""
from __future__ import annotations

import csv
import io

# 各ポータルの CSV 列定義。(出力列名, 物件データのキー) のリスト。実掲載で列は要較正。
PORTAL_FORMATS = {
    "suumo": [
        ("物件名", "property_name"), ("所在地", "address"), ("賃料/価格", "rent_or_price"),
        ("間取り", "layout"), ("専有面積", "area"), ("築年", "built_year"),
        ("構造", "structure"), ("最寄駅", "station"), ("徒歩分", "walk_min"),
        ("取引態様", "deal_type"), ("ペット", "pet"),
    ],
    "homes": [
        ("建物名称", "property_name"), ("住所", "address"), ("賃料または価格", "rent_or_price"),
        ("間取タイプ", "layout"), ("面積(㎡)", "area"), ("築年月", "built_year"),
        ("建物構造", "structure"), ("沿線駅", "station"), ("駅徒歩", "walk_min"),
        ("取引形態", "deal_type"),
    ],
    "athome": [
        ("物件名", "property_name"), ("所在地", "address"), ("価格・賃料", "rent_or_price"),
        ("間取り", "layout"), ("面積", "area"), ("築年数", "built_year"),
        ("構造・工法", "structure"), ("交通", "station"), ("徒歩(分)", "walk_min"),
    ],
    "jibun": [   # 自社サイト用の汎用書式
        ("物件名", "property_name"), ("住所", "address"), ("金額", "rent_or_price"),
        ("間取り", "layout"), ("面積", "area"), ("築年", "built_year"),
        ("構造", "structure"), ("最寄駅", "station"), ("徒歩", "walk_min"),
        ("取引", "deal_type"), ("ペット可", "pet"),
    ],
}


def portals() -> list[str]:
    return list(PORTAL_FORMATS.keys())


def _csv_text(value) -> str:
    """Keep untrusted cells as text when the CSV is opened in a spreadsheet."""
    text = str(value or "")
    first = text.lstrip(" \t\r\n")[:1]
    if first in ("=", "+", "-", "@"):  # CSV/formula injection
        return "'" + text
    return text


def export_csv(properties: list[dict], portal: str) -> str:
    """物件リスト→指定ポータルの掲載書式 CSV（文字列）。未知ポータルは ValueError。
    自動出稿はしない＝生成のみ（人間が管理画面へ手動アップロード）。"""
    fmt = PORTAL_FORMATS.get(portal)
    if fmt is None:
        raise ValueError(f"未知のポータル書式: {portal}（{list(PORTAL_FORMATS)}）")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([col for col, _ in fmt])   # ヘッダ
    for p in properties or []:
        w.writerow([_csv_text(p.get(key, "")) for _, key in fmt])
    return buf.getvalue()


def export_bytes(properties: list[dict], portal: str) -> bytes:
    """Excel/各ポータルが読める CP932(Shift_JIS) の CSV バイト列（不動産ポータルは慣行 SJIS）。"""
    text = export_csv(properties, portal)
    return text.encode("cp932", errors="replace")
