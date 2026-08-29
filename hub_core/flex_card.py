"""hub_core/flex_card.py — 公開物件を LINE Flex カード（bubble / carousel）へ変換する純関数。

正本デザイン = `rios-liff-live/flex-property-card.json`（単一bubble）と
`flex-property-carousel.json`（カルーセル）を Python に移植（構造・色・文言をそのまま）:
- header 帯＝**利用する不動産会社の社名とブランド色**（company.json 由来）。
  お客様に届く面の主役は各社のブランドで、あいのて自身の名前・色は出さない。
- hero に物件写真1枚目＋バッジ（新着/オススメ等）。**写真無し物件は hero 無し bubble**
  （正本カルーセル3件目の形）＋body に「※この物件は写真を準備中です」注記。
- body は和紙色 #F5F1E8（物件名＋号室 / 間取り・専有 / 沿線・駅・徒歩 / 区切り / 賃料）。朱は不使用。
- footer は「内見予約」(primary) ＋「詳細を見る」(link・枠線ボックス) の2ボタン。

契約:
- 入力は liff_export.build_export が吐く LIFF Property（id/name/roomNumber/rent(円,int)/
  managementFee(円,int)/layout/area(㎡,float)/line/station/walkMinutes/address/... /photos?）。
- 写真 URL は `{base_url}{photos[0]}`（photos は "/property-photos/..." の相対）で絶対httpsにする。
- base_url 既定は env `LIFF_PAGES_URL`（無ければ本番 Pages）。
- ボタン URI は正本と同一の LIFF（内見予約は `&book=1`）。
- carousel は**最大10 bubble**（LINE 制約）。超過は切り捨て、返り値に `truncated:n` を付けて
  正直に報告する（黙って落とさない）。**送信時は wire を汚さぬよう truncated を除いてから LINE へ渡す。**
- 純関数・stdlib のみ・ネットワーク非接触・捏造しない（欠けは出さず埋めない）。
"""
from __future__ import annotations

import os

# デザイン正本の色・定数（rios-liff-live の Flex JSON と一致）。
DEFAULT_BRAND = "#1F4A38"  # 会社ブランド色 未設定時の既定（header帯・価格・ボタン・バッジ・枠線）
WASHI = "#F5F1E8"       # 和紙色（body 背景）
TITLE_COLOR = "#1E2621"  # 物件名
SUB_COLOR = "#5C6B63"   # 補足テキスト・区切り線
WHITE = "#FFFFFF"

# 内見予約 LIFF と、写真を置く公開サイト。**利用会社ごとに違う**ので既定値を持たない。
# ここに開発元の URL を焼き込むと、その会社のお客様が別会社のサイトへ飛ぶ
# （マイソクの帯・LINEの社名と同じ「お客様に届く面は各社のもの」の問題）。
# 未設定なら写真も予約リンクも出さない＝黙って他人の場所へ送らない。


def _liff_id() -> str:
    """内見予約 LIFF の ID。**呼ぶたびに読む**（import 時に固定すると設定変更が効かない）。"""
    return (os.environ.get("AINOTE_LIFF_ID") or os.environ.get("LIFF_ID") or "").strip()


DEFAULT_BASE_URL = ""
MAX_BUBBLES = 10                                       # LINE carousel の bubble 上限
_HERO_MISSING_NOTE = "※この物件は写真を準備中です"



def brand_of(company: dict | None) -> tuple[str, str]:
    """company.json から (社名, ブランド色) を取り出す。

    お客様に届く面（LINE カード）の主役は**利用する不動産会社**であって、あいのて自身ではない。
    社名が未設定なら空文字を返し、呼び出し側は帯を出さない
    ＝勝手に別の社名を出さない。色は #RRGGBB のみ受け付け、壊れた値は既定へ落とす。
    """
    c = company or {}
    name = str(c.get("name") or c.get("company_name") or "").strip()
    color = str(c.get("brand_color") or "").strip()
    if not (len(color) == 7 and color.startswith("#")
            and all(ch in "0123456789abcdefABCDEF" for ch in color[1:])):
        color = DEFAULT_BRAND
    return name, color


def _resolve_base_url(base_url: str) -> str:
    """写真を置く公開サイトの URL。明示指定 > env LIFF_PAGES_URL。未設定なら空。

    空のときは写真を出さない（開発元のサイトへ勝手に飛ばさない）。
    """
    base = (base_url or os.environ.get("LIFF_PAGES_URL") or DEFAULT_BASE_URL).strip()
    return base.rstrip("/")


def _liff_uri(property_id: str, *, book: bool = False) -> str:
    """内見予約 LIFF の URI。LIFF ID 未設定なら空（ボタンごと出さない）。"""
    from urllib.parse import quote
    lid = _liff_id()
    if not lid:
        return ""
    pid = quote(str(property_id or ""), safe="")   # 予期せぬ文字が URI を壊さぬようエンコード
    uri = f"https://liff.line.me/{lid}?property={pid}"
    return uri + "&book=1" if book else uri


def _price_man(rent) -> str:
    """賃料/価格（円・int）→ 万円表記の値部（82000→'8.2'・138000→'13.8'・80000→'8'）。
    解釈できなければ空（捏造しない）。"""
    try:
        v = float(rent)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return f"{v / 10000:g}"


def _spec_line(prop: dict) -> str:
    """間取り・専有面積の1行（'1K ・ 専有 26.4㎡'）。欠けた要素は落とす。"""
    layout = str(prop.get("layout") or "").strip()
    area = prop.get("area")
    parts = []
    if layout:
        parts.append(layout)
    try:
        a = float(area)
        if a > 0:
            parts.append(f"専有 {a:g}㎡")
    except (TypeError, ValueError):
        pass
    return " ・ ".join(parts)


def _access_line(prop: dict) -> str:
    """沿線・駅・徒歩分の1行（'京王線 明大前駅 徒歩7分'）。沿線省略や徒歩不明でも駅は出す。"""
    line = str(prop.get("line") or "").strip()
    station = str(prop.get("station") or "").strip()
    walk = prop.get("walkMinutes")
    head = " ".join(x for x in (line, station) if x)
    try:
        w = int(walk)
        if w > 0:
            return (head + f" 徒歩{w}分").strip()
    except (TypeError, ValueError):
        pass
    return head


def _title(prop: dict) -> str:
    """物件名＋号室（'さくら台レジデンス 301'）。号室が空なら名前だけ。"""
    name = str(prop.get("name") or "").strip()
    room = str(prop.get("roomNumber") or "").strip()
    return f"{name} {room}".strip() if room else name


def _header(*, company_name: str, brand: str) -> dict:
    """header 帯＝取扱会社の社名とブランド色。社名が空なら帯を出さない（他社名を騙らない）。"""
    if not company_name:
        return {}
    return {
        "type": "box", "layout": "horizontal", "backgroundColor": brand,
        "paddingTop": "8px", "paddingBottom": "8px", "paddingStart": "14px", "paddingEnd": "14px",
        "contents": [
            {"type": "text", "text": company_name, "color": WHITE,
             "size": "sm", "weight": "bold", "gravity": "center"},
        ],
    }


def _hero(prop: dict, *, base_url: str, badge: str, brand: str) -> dict:
    """hero（写真1枚目を全幅・タップで詳細へ）＋バッジ（badge が空ならバッジ無し）。写真ありのみ。"""
    photos = prop.get("photos") or []
    url = _resolve_base_url(base_url) + str(photos[0])
    contents = [
        {"type": "image", "url": url, "size": "full", "aspectRatio": "20:13", "aspectMode": "cover",
         "action": {"type": "uri", "label": "詳細を見る", "uri": _liff_uri(prop.get("id"))}},
    ]
    if badge:
        contents.append({
            "type": "box", "layout": "vertical", "position": "absolute",
            "offsetTop": "12px", "offsetStart": "12px", "backgroundColor": brand,
            "cornerRadius": "2px", "paddingTop": "4px", "paddingBottom": "4px",
            "paddingStart": "10px", "paddingEnd": "10px",
            "contents": [{"type": "text", "text": badge, "color": WHITE, "size": "xs", "weight": "bold"}],
        })
    return {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": contents}


def _body(prop: dict, *, has_photo: bool, brand: str) -> dict:
    """body（和紙色・物件名/間取り/アクセス/区切り/価格）。写真無しなら準備中の注記を末尾に足す。"""
    contents: list = [
        {"type": "text", "text": _title(prop), "weight": "bold", "size": "lg",
         "color": TITLE_COLOR, "wrap": True},
    ]
    spec = _spec_line(prop)
    if spec:
        contents.append({"type": "text", "text": spec, "size": "sm", "color": SUB_COLOR, "wrap": True})
    access = _access_line(prop)
    if access:
        contents.append({"type": "text", "text": access, "size": "sm", "color": SUB_COLOR, "wrap": True})
    contents.append({"type": "separator", "margin": "md", "color": SUB_COLOR})

    man = _price_man(prop.get("rent"))
    if man:
        price_row = [
            {"type": "text", "text": man, "weight": "bold", "size": "xxl", "color": brand, "flex": 0},
            {"type": "text", "text": "万円", "size": "sm", "color": brand,
             "gravity": "bottom", "margin": "xs", "flex": 0},
        ]
        try:
            mgmt = int(prop.get("managementFee"))
        except (TypeError, ValueError):
            mgmt = 0
        if mgmt > 0:
            price_row.append({"type": "text", "text": f"管理費 {mgmt:,}円", "size": "xs",
                              "color": SUB_COLOR, "gravity": "bottom", "margin": "md"})
        contents.append({"type": "box", "layout": "baseline", "margin": "md", "contents": price_row})

    if not has_photo:
        contents.append({"type": "text", "text": _HERO_MISSING_NOTE, "size": "xxs",
                         "color": SUB_COLOR, "margin": "md", "wrap": True})
    return {"type": "box", "layout": "vertical", "backgroundColor": WASHI,
            "paddingAll": "16px", "spacing": "xs", "contents": contents}


class CardComplianceError(Exception):
    """広告として必要な表示が欠けているカードは組み立てない（送る前に止める）。"""

    def __init__(self, missing: list):
        self.missing = missing
        super().__init__("広告に必要な表示が足りません: " + "、".join(missing))


def _compliance_contents(prop: dict, company: dict | None) -> list:
    """カードの末尾に刻む取引態様と取扱業者。

    LINEで送る物件カードは広告なので、取引態様の明示（宅建業法34条）と
    取扱業者が分かる表示が要る。ここが空のカードは組み立てない。
    """
    taiyo = str((prop or {}).get("dealType") or "").strip()
    c = company or {}
    name = str(c.get("name") or c.get("company_name") or "").strip()
    lic = str(c.get("license_no") or c.get("license") or "").strip()
    missing = []
    if not taiyo:
        missing.append("取引態様")
    if not name:
        missing.append("取扱業者の商号")
    if not lic:
        missing.append("免許証番号")
    if missing:
        raise CardComplianceError(missing)
    return [
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": f"取引態様：{taiyo}", "size": "xxs",
         "color": SUB_COLOR, "margin": "md", "wrap": True},
        {"type": "text", "text": f"{name}　{lic}", "size": "xxs",
         "color": SUB_COLOR, "wrap": True},
    ]


def _footer(prop: dict, *, brand: str, company: dict | None = None) -> dict:
    """footer（内見予約=primary ＋ 詳細を見る=link・枠線ボックス＋広告の必要表示）。"""
    pid = prop.get("id")
    return {
        "type": "box", "layout": "vertical", "spacing": "sm", "backgroundColor": WHITE,
        "paddingAll": "12px",
        "contents": [
            {"type": "button", "style": "primary", "color": brand, "height": "sm",
             "action": {"type": "uri", "label": "内見予約", "uri": _liff_uri(pid, book=True)}},
            {"type": "box", "layout": "vertical", "backgroundColor": WHITE, "borderColor": brand,
             "borderWidth": "1px", "cornerRadius": "4px",
             "contents": [
                 {"type": "button", "style": "link", "color": brand, "height": "sm",
                  "action": {"type": "uri", "label": "詳細を見る", "uri": _liff_uri(pid)}},
             ]},
        ] + _compliance_contents(prop, company),
    }


def build_property_bubble(prop: dict, *, base_url: str = "", badge: str = "",
                          company: dict | None = None) -> dict:
    """公開物件1件 → Flex bubble（header帯/hero/body/footer）。写真無しは hero 無し（正本3件目の形）。"""
    prop = prop or {}
    name, brand = brand_of(company)
    has_photo = bool(prop.get("photos"))
    bubble = {"type": "bubble", "size": "mega"}
    header = _header(company_name=name, brand=brand)
    if header:
        bubble["header"] = header
    if has_photo:
        bubble["hero"] = _hero(prop, base_url=base_url, badge=badge, brand=brand)
    bubble["body"] = _body(prop, has_photo=has_photo, brand=brand)
    bubble["footer"] = _footer(prop, brand=brand, company=company)
    return bubble


def build_property_carousel(props: list, *, base_url: str = "", badge: str = "",
                            company: dict | None = None) -> dict:
    """公開物件リスト → Flex carousel（最大10 bubble）。超過は切り捨て `truncated:n` で報告。
    返り値 = {"type":"carousel","contents":[bubble,...],"truncated":切り捨て件数}。
    **送信時は truncated を除いてから LINE へ渡す**（Flex コンテナに余分キーを載せない）。"""
    items = [p for p in (props or []) if isinstance(p, dict)]
    kept = items[:MAX_BUBBLES]
    truncated = len(items) - len(kept)
    contents = [build_property_bubble(p, base_url=base_url, badge=badge, company=company)
                for p in kept]
    return {"type": "carousel", "contents": contents, "truncated": truncated}
