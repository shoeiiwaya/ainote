"""hub_core/zoning.py — 用途地域の判定（座標→用途地域名／建ぺい率・容積率）。

思想: **ローカル完結**。国土数値情報 A29（用途地域）の GeoJSON を手元に置き、点in面（point-in-polygon）で
判定する。住所を外部に送らない（ジオコードは sovereignty ゲート）。データ取込は配備側の人間ゲート
（A29 の DL／ライセンス確認）。データが無ければ None を返し、UIは自治体の都市計画図への導線に落ちる。

stdlib のみ（json・math）。GeoJSON は FeatureCollection（Polygon/MultiPolygon）を想定。
プロパティのキーは A29 準拠（用途地域名/建ぺい率/容積率）だが env で差し替え可能。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def geojson_path() -> str:
    """用途地域 GeoJSON のパス（env ZONING_GEOJSON）。未設定なら空＝データ未取込。"""
    return str(os.environ.get("ZONING_GEOJSON") or "").strip()


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """点(lon,lat)が単一リング(座標配列)の内側か。ray-casting。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """GeoJSON Polygon座標（[外環, 穴, ...]）に対する内外判定（穴を除外）。"""
    if not polygon:
        return False
    if not _point_in_ring(lon, lat, polygon[0]):
        return False
    for hole in polygon[1:]:                 # 穴の中なら外
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def _feature_contains(lon: float, lat: float, geom: dict) -> bool:
    if not geom:
        return False
    gt = geom.get("type")
    coords = geom.get("coordinates")
    if gt == "Polygon":
        return _point_in_polygon(lon, lat, coords)
    if gt == "MultiPolygon":
        return any(_point_in_polygon(lon, lat, poly) for poly in (coords or []))
    return False


# A29 のプロパティキー候補（データ提供元で表記揺れがあるため複数を許容）。env で上書き可。
_YOUTO_KEYS = ("用途地域", "youto", "A29_005", "yuto")
_KENPEI_KEYS = ("建ぺい率", "建蔽率", "kenpei", "A29_006")
_YOSEKI_KEYS = ("容積率", "yoseki", "A29_007")


def _first(props: dict, keys) -> str:
    for k in keys:
        v = props.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def lookup(lat, lon, *, path: str = "") -> dict | None:
    """座標→用途地域 dict {youto, kenpei, yoseki}。データ未取込/不在/圏外なら None。
    住所でなく座標を受ける（住所の外部送信を避ける＝主権）。"""
    p = (path or geojson_path()).strip()
    if not p or not Path(p).is_file():
        return None
    try:
        latf, lonf = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for feat in (data.get("features") or []):
        if _feature_contains(lonf, latf, feat.get("geometry") or {}):
            props = feat.get("properties") or {}
            youto = _first(props, _YOUTO_KEYS)
            kenpei = _first(props, _KENPEI_KEYS)
            yoseki = _first(props, _YOSEKI_KEYS)
            kv = ""
            if kenpei or yoseki:
                kv = (kenpei + "%" if kenpei else "") + ("／" + yoseki + "%" if yoseki else "")
            return {"youto": youto, "kenpei_yoseki": kv,
                    "source": "国土数値情報A29（用途地域）・ローカル点in面判定"}
    return None


def available() -> bool:
    """用途地域データが取り込まれているか（UIの導線分岐用）。"""
    p = geojson_path()
    return bool(p and Path(p).is_file())
