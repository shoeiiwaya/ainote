"""hub_core/ocr_geom.py — bounding box を使った表組み（グリッド）復元による構造化。

なぜ必要か: 販売図面/マイソクは「ラベル列｜値列」の表組み。OS標準OCRは視覚位置順にテキストを吐くため、
ラベルと値が交互に混ざり、テキストだけでは価格(31,000)と管理費(26,200)を取り違える（実測で確認）。
各文字列の座標（Vision/Windows.Media.Ocr が持っている）を使い、「ラベルと同じ行の、右にある最近傍の値」を
対応づければグリッドが復元でき、混線が解ける。無料・ローカルのまま精度が上がる。

座標系: 正規化 [0,1]・原点は左下（Vision 準拠）。抽出できない項目は空＝捏造しない。
"""
from __future__ import annotations

import re

# フィールド → ラベル候補（表の見出し語）。表組みで「ラベルの右の値」を取る対象。
_LABELS = {
    "property_type": ["物件種目", "種目"],
    "property_name": ["建物名称", "物件名称", "マンション名"],
    "price": ["価格", "販売価格"],
    "management_fee": ["管理費"],
    "repair_fund": ["修繕積立金", "修繕積立"],
    "address": ["所在地", "住所"],
    "transit": ["交通", "最寄"],
    "land_area": ["敷地面積"],
    "land_rights": ["土地権利", "土地の権利"],
    "area": ["専有面積", "専有"],
    "layout": ["間取り", "間取"],
    "floor": ["所在階"],
    "built": ["築年月", "建築年月"],
    "structure": ["建物構造", "構造"],
    "total_units": ["総戸数"],
    "youto": ["用途地域"],
    "parking": ["駐車場", "駐車"],
}

# 値を掃除する（末尾の単位ラベル混入などを軽く除く）。フィールド別。
_UNIT_APPEND = {"price": "万円", "management_fee": "", "repair_fund": ""}


def _cy(b):
    return float(b["y"]) + float(b["h"]) / 2.0


def _cx(b):
    return float(b["x"]) + float(b["w"]) / 2.0


# 数値を期待するフィールド（最近傍の非数値セル＝測定基準語「壁芯」等を飛ばして数値セルを取る）。
_WANT = {
    "price": r"\d", "management_fee": r"\d", "repair_fund": r"\d",
    "area": r"\d+\.\d", "land_area": r"\d", "total_units": r"\d", "floor": r"\d",
    "parking": r"[\d円]",
    # 構造は「部分」等の断片でなく構造語を含む値を選ぶ（近さだけで断片を拾う誤りを避ける）
    "structure": r"造|鉄|木|コンクリート|SRC|RC",
}


def _same_row(a, b):
    """縦方向の範囲が重なれば同じ行（中心距離でなく range-overlap＝背の高い値セルのズレに強い）。"""
    a0, a1 = float(a["y"]), float(a["y"]) + float(a["h"])
    b0, b1 = float(b["y"]), float(b["y"]) + float(b["h"])
    overlap = min(a1, b1) - max(a0, b0)
    return overlap > 0.25 * min(float(a["h"]), float(b["h"]))


def _find_label_box(boxes, names):
    """ラベル語を含む箱を返す。優先: ①「ラベル：値」で値が実在する箱（同箱内に値がある様式）
    ②短い箱（値を巻き込まないラベル単体＝別箱に値がある様式）。両レイアウトを1関数で扱う。"""
    matches = []
    for b in boxes:
        tx = str(b.get("text") or "")
        for nm in names:
            if tx == nm or tx.startswith(nm) or (nm in tx and len(tx) <= len(nm) + 6):
                matches.append(b)
                break
    if not matches:
        return None

    def _score(b):
        tx = str(b.get("text") or "")
        has_inline = any(s in tx and tx.partition(s)[2].strip() for s in ("：", ":"))
        return (0 if has_inline else 1, len(tx))

    matches.sort(key=_score)
    return matches[0]


def _inline_value(label_box, names):
    """ラベルと値が同じ箱に入っている様式で、同箱内の値を返す（右隣を探すと見つからない）。
    ①「ラベル：値」コロン区切り（所在地：東京都…） ②区切り無しでラベル語に値が直結（総戸数650戸…）。"""
    tx = str(label_box.get("text") or "")
    # ①コロン区切り
    for sep in ("：", ":"):
        if sep in tx:
            head, _, tail = tx.partition(sep)
            if any(nm in head for nm in names) and tail.strip():
                return tail.strip()
    # ②区切り無しでラベル語に数値が直結（「総戸数650戸の…」→「650戸の…」）。
    # 値が数字で始まる時だけ＝別名が長いラベル語の頭に一致して続き（専有→面積）を誤採用するのを防ぐ。
    for nm in names:
        if tx.startswith(nm) and len(tx) > len(nm):
            rest = tx[len(nm):].lstrip("：: 　")
            if rest and rest[0].isdigit():
                return rest
    return ""


def _value_right_of(boxes, label_box, want=""):
    """ラベルと同じ行で、右にある最近傍の箱（＝値セル）。want 指定時はそれに一致する最近傍を選ぶ
    （数値フィールドで「壁芯」等の非数値セルを飛ばして数値セルを取る）。"""
    cands = [b for b in boxes
             if b is not label_box and _same_row(b, label_box) and _cx(b) > _cx(label_box)]
    if not cands:
        return None
    cands.sort(key=lambda b: _cx(b) - _cx(label_box))
    if want:
        for b in cands:
            if re.search(want, str(b.get("text") or "")):
                return b
    return cands[0]


def _clean_value(field, text):
    v = str(text or "").strip()
    if not v:
        return ""
    # 値セルに単位ラベルが混じる場合の軽い除去（例: "80.00 mi" の末尾記号）
    if field == "price":
        m = re.search(r"([\d,]{3,7})", v)
        return (m.group(1) + "万円") if m else v
    if field in ("management_fee", "repair_fund"):
        m = re.search(r"([\d,]{2,7})", v)
        return (m.group(1) + "円/月") if m else v
    if field == "area":
        m = re.search(r"([\d]{2,3}\.\d{1,2})", v)
        return (m.group(1) + "㎡") if m else v
    if field == "total_units":
        m = re.search(r"([\d,]{2,6})", v)
        return (m.group(1) + "戸") if m else v
    if field == "floor":
        m = re.search(r"(\d{1,2})", v)
        return (m.group(1) + "階") if m else v
    if field == "structure":
        # 「鉄筋コンクリート造地下3階付43階建16階」等から構造種別だけ抜く（階建等の付随を落とす）
        m = re.search(r"(鉄骨鉄筋コンクリート造|鉄筋コンクリート造|鉄骨造|軽量鉄骨造|木造|SRC造?|RC造?)", v)
        if m:
            return m.group(1)
        return v if any(k in v for k in ("造", "鉄", "木", "コンクリート", "SRC", "RC")) else ""
    if field == "address":
        # 末尾の注記（（住居表示）等）を除く
        return re.sub(r"[（(][^）)]*[）)]\s*$", "", v).strip()
    if field == "property_name":
        return v.rstrip("：:／/・ 　")
    if field == "layout":
        m = re.search(r"([1-9]\d?(?:S?LDK|S?DK|K|R)(?:\s*\+\s*S)?)", v)
        return m.group(1) if m else v
    return v


def _recover_price(boxes):
    """価格ラベルが無い様式の価格回復。大きな装飾フォントの「万円」はVisionが似た漢字（細/組）に
    誤読し、カンマも落ちることがある（例「28,990万円」→「28990細」）。（税込）/（税抜）/価格 マーカー
    近傍の、価格帯(300万〜30億)の4-6桁数字を価格とみなす。捏造でなく実在する数字の回収（誤読補正）。"""
    markers = [b for b in boxes
               if any(m in str(b.get("text") or "") for m in ("税込", "税抜", "価格", "販売価格"))]
    if not markers:
        return ""
    best = None
    for mk in markers:
        for b in boxes:
            digits = re.sub(r"[^\d]", "", str(b.get("text") or ""))
            if not (4 <= len(digits) <= 6):
                continue
            n = int(digits)
            if not (300 <= n <= 300000):        # 300万〜30億（万円換算）
                continue
            dist = abs(_cy(b) - _cy(mk)) + abs(_cx(b) - _cx(mk))
            if best is None or dist < best[0]:
                best = (dist, n)
    if best is None:
        return ""
    return f"{best[1]:,}万円"


def structure_from_boxes(boxes: list) -> dict:
    """boxes（[{text,x,y,w,h}]）→ グリッド復元でフィールド抽出。抽出できた項目のみ・捏造なし。"""
    fields = {}
    matched_labels = []
    missed = []
    for field, names in _LABELS.items():
        lb = _find_label_box(boxes, names)
        if lb is None:
            missed.append(names[0])
            continue
        # ①同じ箱に「ラベル：値」で入っていればコロン以降を使う ②無ければ右隣の値セル
        inline = _inline_value(lb, names)
        if inline:
            raw = inline
        else:
            vb = _value_right_of(boxes, lb, want=_WANT.get(field, ""))
            if vb is None:
                missed.append(names[0])
                continue
            raw = vb.get("text")
        val = _clean_value(field, raw)
        if val:
            fields[field] = val
            matched_labels.append(names[0])
        else:
            missed.append(names[0])
    # 価格ラベルが無い様式のフォールバック（大きな装飾フォントの万円グリフ誤読を（税込）近傍から回収）
    if "price" not in fields:
        rp = _recover_price(boxes)
        if rp:
            fields["price"] = rp
            if "価格" in missed:
                missed.remove("価格")
    fields["_meta"] = {"extracted": [k for k in fields if k != "_meta"],
                       "missed": missed, "engine": "geometry-grid"}
    return fields
