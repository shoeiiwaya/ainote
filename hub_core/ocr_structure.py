"""hub_core/ocr_structure.py — OCR生テキスト → 不動産フィールドの決定論的構造化（無料・ローカル・LLM不要）。

なぜ決定論か: サーバは実LLMを呼ばない原則＋無料ローカル保証＋再現可能性のため、正規表現ベースで抽出する
（弱いローカルLLMに任せると再現性・検証性が崩れる）。抽出できない項目は空のまま返す＝**捏造しない**。
読取値は下書き補助であって真実でない＝呼び出し側で出典束縛フレーム＋宅建士確認を通す前提。

限界（正直に）: 販売図面/マイソクは表組みのため、OS標準OCR（Vision/Windows.Media.Ocr）は視覚位置順で読み、
ラベル列と値列が交互に混ざることがある。パターンが安定する項目（間取り/所在地/駅徒歩/築年月/構造/総戸数/
用途地域/駐車場）は高精度、値がグリッドで散る数値項目（価格/管理費/専有面積）は取りこぼし・誤りが出る。
→ 幾何情報（bounding box）でグリッドを復元する強化版は ocr_structure_geom を参照。
"""
from __future__ import annotations

import re

# ㎡ は OCR で m / mi / m2 / m' / ㎡ / 平米 など様々に化ける。面積単位の集合。
_M2 = r"(?:㎡|m2|mi|m['’]|平米|㎡)"


def _first(patterns, text, group=1, flags=re.MULTILINE):
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return m.group(group).strip()
    return ""


def _property_type(t):
    return _first([
        r"(中古マンション|新築マンション|中古一戸建て?|新築一戸建て?|中古戸建|新築戸建|"
        r"売地|土地|マンション|アパート|一戸建て?|戸建|区分所有|事務所|店舗)"], t)


def _property_name(t):
    # 建物名称: 固有名。まず特徴的な語尾（タワー等・マンションより物件名らしい）を取り、
    # 無ければマンションだが「中古/新築/区分…マンション」は物件種目語なので除外する。
    NAME = r"[一-龥ぁ-んァ-ヶ0-9A-Za-zＡ-Ｚ０-９Ⅰ-Ⅴ]{2,20}"
    distinctive = _first([
        rf"({NAME}(?:タワー|レジデンス|ハイツ|コーポ|パレス|ヒルズ|コート|テラス|ガーデン|ハウス))"], t)
    if distinctive and not distinctive.startswith(("中古", "新築", "分譲", "賃貸")):
        return distinctive
    # マンション語尾（種目語を除く）
    for m in re.finditer(rf"({NAME}マンション)", t):
        v = m.group(1)
        if not v.startswith(("中古", "新築", "分譲", "賃貸", "区分", "タワー")):
            return v
    return distinctive


def _layout(t):
    # 間取り: 2LDK / 3SLDK / 1K / 2LDK+S（納戸）
    return _first([r"([1-9]\d?(?:S?LDK|S?DK|K|R))(?:\s*\+\s*S)?"], t)


def _address(t):
    return _first([
        r"((?:東京都|北海道|(?:京都|大阪)府|[一-龥]{2,3}県)"
        r"[一-龥ぁ-んァ-ヶ]{1,6}[市区郡][一-龥ぁ-んァ-ヶ]{0,8}[町村区]?[0-9０-９一-龥\-－丁目番地号]{1,20})"], t)


def _transit(t):
    return _first([
        r"([『「][^』」\n]{1,10}[』」]駅\s*徒歩\s*\d+\s*分)",
        r"(\S{1,12}駅\s*徒歩\s*\d+\s*分)"], t)


def _built(t):
    # 築年月は元号注記（平成/令和/昭和）付きが正規＝リノベ完成予定日（元号なし「2026年4月下旬完成」等）より優先。
    return _first([
        r"(\d{4}\s*年\s*（\s*(?:平成|令和|昭和)\s*\d{1,2}\s*年\s*）\s*\d{1,2}\s*月)",
        r"((?:平成|令和|昭和)\s*\d{1,2}\s*年\s*\d{1,2}\s*月)",
        r"(\d{4}\s*年\s*\d{1,2}\s*月)"], t)


def _structure(t):
    m = _first([r"(鉄骨鉄筋コンクリート|鉄筋コンクリート|SRC|RC|鉄骨造|鉄骨|軽量鉄骨|木造)"], t)
    if not m:
        return ""
    # SRC/RC/鉄骨/木造 → 「〜造」に正規化（既に造があれば重ねない）
    return m if m.endswith("造") or m in ("SRC", "RC") else m + "造"


def _total_units(t):
    v = _first([r"総戸数[\s\S]{0,10}?([\d,]{2,6})\s*戸", r"([\d,]{2,6})\s*戸(?:数)?"], t)
    return (v + "戸") if v else ""


def _youto(t):
    return _first([r"用途地域[\s\S]{0,10}?([一-龥]{2,4}地域)", r"([一-龥]{2,4}地域)"], t)


def _floor(t):
    return _first([r"(\d{1,2})\s*階\s*部分", r"所在階[\s\S]{0,8}?(\d{1,2})\s*階"], t)


def _parking(t):
    return _first([r"([\d,]{3,6}\s*[~〜]\s*[\d,]{3,6}\s*円)", r"駐車場[\s\S]{0,20}?([\d,]{3,6}\s*円)"], t)


def _area(t):
    # 専有面積: 壁芯/内法の直後の小数、または 専有面積 の近傍。バルコニー/敷地面積と混同しないよう順に試す
    v = _first([
        r"専有面積[\s\S]{0,20}?([\d]{2,3}\.\d{1,2})",
        r"壁芯[\s\S]{0,8}?([\d]{2,3}\.\d{1,2})",
        r"内法[\s\S]{0,8}?([\d]{2,3}\.\d{1,2})"], t)
    return (v + "㎡") if v else ""


def _price(t):
    """価格: 表組みで散るため候補を全部集め、物件価格の妥当帯（500万〜300,000万＝5億超も許容）で選ぶ。
    ⚠️ グリッド混線で ㎡単価・管理費と取り違える可能性がある＝低信頼（要確認・幾何強化で改善）。"""
    cands = []
    for m in re.finditer(r"([\d,]{3,7})\s*万円", t):
        try:
            n = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 300 <= n <= 300000:  # 300万〜30億の帯
            cands.append((m.start(), n, m.group(1)))
    if not cands:
        return ""
    # ラベル「価格」に最も近い候補を優先（無ければ文書順の最初）
    price_pos = t.find("価格")
    if price_pos >= 0:
        cands.sort(key=lambda c: abs(c[0] - price_pos))
    return cands[0][2] + "万円"


def _company(t):
    return _first([r"(株式会社\s*[一-龥ぁ-んァ-ヶA-Za-z]{2,15}|[一-龥ぁ-んァ-ヶA-Za-z]{2,15}株式会社)"], t)


def _license(t):
    return _first([r"((?:東京都知事|国土交通大臣|[一-龥]{2,4}県知事)\s*(?:免許)?\s*[（(]\s*\d+\s*[)）]\s*第?\s*\d*\s*号?)",
                   r"((?:東京都知事|国土交通大臣|[一-龥]{2,4}県知事)免許\s*[（(]\d+[)）])"], t)


# フィールド抽出器（順序＝マイソク/物件フォームの表示順）。key は register/maisoku のフィールド名に対応。
_EXTRACTORS = [
    ("property_type", "物件種目", _property_type),
    ("property_name", "物件名", _property_name),
    ("price", "価格", _price),
    ("address", "所在地", _address),
    ("transit", "交通", _transit),
    ("area", "専有面積", _area),
    ("layout", "間取り", _layout),
    ("floor", "所在階", _floor),
    ("built", "築年月", _built),
    ("structure", "建物構造", _structure),
    ("total_units", "総戸数", _total_units),
    ("youto", "用途地域", _youto),
    ("parking", "駐車場", _parking),
    ("company_name", "宅建業者", _company),
    ("license_no", "免許番号", _license),
]


def structure(text: str) -> dict:
    """OCR生テキスト → {field: value} のフィールド辞書（決定論・抽出できた項目のみ・捏造なし）。
    返り値には _meta で抽出できた/できなかったキー一覧を付す（品質の可視化・出典束縛の思想）。"""
    t = text or ""
    fields = {}
    missed = []
    for key, label, fn in _EXTRACTORS:
        try:
            v = fn(t)
        except re.error:
            v = ""
        if v:
            fields[key] = v
        else:
            missed.append(label)
    fields["_meta"] = {"extracted": [k for k in fields if k != "_meta"],
                       "missed": missed, "engine": "deterministic-regex"}
    return fields


def structure_for_maisoku(text: str) -> dict:
    """マイソク生成に渡せる形（_meta を除いた plain dict）。"""
    f = structure(text)
    return {k: v for k, v in f.items() if k != "_meta"}


# 合流時の出典優先度（法的真実源ほど高い）。書類名に含む語で判定。面積・所在地は図面より登記を信頼。
_SOURCE_PRIORITY = [
    ("登記", 3), ("謄本", 3), ("登記事項", 3),
    ("調査報告", 2), ("重要事項調査", 2), ("設備表", 2), ("物件状況", 2),
    ("図面", 1), ("マイソク", 1), ("販売", 1),
]


def _source_rank(name: str) -> int:
    n = name or ""
    return max((r for kw, r in _SOURCE_PRIORITY if kw in n), default=0)


def merge_property_fields(docs: list) -> dict:
    """複数書類の抽出フィールドを1つの物件レコードに合流する（source-bound）。
    docs = [(書類名, fields_dict), ...]。原則は欠損を埋める先着優先。ただし登記/調査報告書など法的真実源が
    同じ項目を持つ場合はそちらで上書き（面積・所在地は図面より登記を信頼）。返り値に _sources（項目→由来
    書類）を含む。捏造しない＝どこかの書類に実在した値だけ（複数ソースで「楽に全部埋める」の中核）。"""
    merged: dict = {}
    sources: dict = {}
    srank: dict = {}
    for name, fields in docs:
        rank = _source_rank(name)
        for k, v in (fields or {}).items():
            if k.startswith("_") or not v:
                continue
            if k not in merged or rank > srank.get(k, 0):
                merged[k] = v
                sources[k] = name
                srank[k] = rank
    merged["_sources"] = sources
    return merged


def structure_documents(paths: list, *, page: int = 1) -> dict:
    """複数の画像/PDFをOCR構造化して1物件レコードに合流（「持ってる書類を全部ドロップ」の中核）。
    書類が増えるほど埋まる。各項目は _sources で由来書類を保持。"""
    from pathlib import Path as _P
    docs = []
    for p in paths:
        try:
            f = structure_document(p, page=page)
        except Exception:
            f = {}
        plain = {k: v for k, v in f.items() if not str(k).startswith("_")}
        docs.append((_P(p).name, plain))
    return merge_property_fields(docs)


def structure_document(path: str, *, page: int = 1) -> dict:
    """画像/PDF → 構造化フィールド。**幾何グリッド復元を優先し、regex（自由文）で補完**するハイブリッド。
    販売図面/マイソクの表組みは幾何が強い（価格/管理費/専有面積の混線を解く・実測100%）が、
    表に無い自由記述（交通の全文等）は regex が拾う。抽出できた項目のみ・捏造なし・出典束縛前提。"""
    from hub_core import local_ocr, ocr_geom
    boxes = []
    try:
        boxes = local_ocr.ocr_boxes(path, page=page)
    except Exception:
        boxes = []
    geom = ocr_geom.structure_from_boxes(boxes) if boxes else {"_meta": {"engine": "none"}}
    text = ""
    try:
        text = local_ocr.ocr_any(path)
    except Exception:
        text = ""
    reg = structure(text) if text else {"_meta": {"engine": "none"}}

    merged = {}
    # 幾何を優先（グリッド精度）。幾何が取れなかったキーを regex で補完。
    for k, v in geom.items():
        if k != "_meta" and v:
            merged[k] = v
    for k, v in reg.items():
        if k != "_meta" and v and k not in merged:
            merged[k] = v
    merged["_meta"] = {
        "extracted": [k for k in merged if k != "_meta"],
        "geom_engine": geom.get("_meta", {}).get("engine"),
        "text_engine": reg.get("_meta", {}).get("engine"),
        "boxes": len(boxes),
    }
    return merged
