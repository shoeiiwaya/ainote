"""hazard_links — 物件座標→外部公式ハザードマップへの deep-link builder（監査DZ-1）。

競合SaaSに無い「1クリックで法定ハザード確認」導線。物件の緯度経度から、国交省・自治体の
**公式**ハザード情報サイトへ座標つきで飛ぶURLを生成する。
- 全リンクは .go.jp / .lg.jp（既存の取得先allowlistと同一ドメイン規律）＝許可外URLは生成しない。
- 数値は生成しない（外部の公式図を人間が見る導線＝金メッキと無縁・施行規則16条の4の3の確認をあいのて内で起点化）。
- ネットワーク非接触（URLを組むだけ・fetchしない）。stdlib only。
"""
from __future__ import annotations

from urllib.parse import quote

# 生成先として許可する公的ドメイン（.go.jp/.lg.jp と一致。他は生成しない）。
_ALLOWED_SUFFIX = (".go.jp", ".lg.jp")


def _ok(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(_ALLOWED_SUFFIX)


def hazard_links(lat: float, lon: float, *, zoom: int = 16) -> list[dict]:
    """緯度経度→公式ハザードマップ deep-link のリスト。
    各要素= {key, title, url, source, kind}。許可外ドメインは除外（fail-safe）。"""
    try:
        latf, lonf = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    z = int(zoom)
    # 国土交通省ハザードマップポータル「重ねるハザードマップ」（洪水・土砂・津波・高潮を1画面で）
    kasaneru = (f"https://disaportal.gsi.go.jp/maps/index.html"
                f"?ll={latf},{lonf}&z={z}")
    # 国土地理院 地理院地図（標高・治水地形分類・土地条件）
    gsi = f"https://maps.gsi.go.jp/#{z}/{latf}/{lonf}/"
    # 防災科研 J-SHIS 地震ハザードステーション（確率論的地震動）
    jshis = f"https://www.j-shis.bosai.go.jp/map/?lat={latf}&lon={lonf}&z={z}"
    candidates = [
        {"key": "kasaneru", "title": "重ねるハザードマップ（洪水・土砂・津波・高潮）",
         "url": kasaneru, "source": "国土交通省", "kind": "総合"},
        {"key": "gsi", "title": "地理院地図（標高・治水地形・土地条件）",
         "url": gsi, "source": "国土地理院", "kind": "地形"},
        {"key": "jshis", "title": "J-SHIS 地震ハザードステーション",
         "url": jshis, "source": "防災科研", "kind": "地震"},
    ]
    return [c for c in candidates if _ok(c["url"])]


def municipality_hint(address: str) -> dict:
    """自治体ハザードマップは市区町村ごとに個別サイト＝住所から検索導線を出す（座標では飛べない）。
    公式に飛ぶ前段として、政府の検索起点（ハザードマップポータル 市区町村検索）へ誘導。"""
    addr = str(address or "").strip()
    search = ("https://disaportal.gsi.go.jp/hazardmap/index.html"
              + (("?q=" + quote(addr)) if addr else ""))
    return {"key": "municipality", "title": "自治体ハザードマップを探す（市区町村公表の法定図）",
            "url": search if _ok(search) else "", "source": "ハザードマップポータル",
            "kind": "自治体", "address": addr,
            "note": "自治体公表の最新ハザードマップが施行規則16条の4の3の法定確認対象。"}
