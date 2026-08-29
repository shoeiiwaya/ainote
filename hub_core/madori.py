"""M-madori — 間取り図SVGの生成（Wave4磨き・stdlib・¥0）。

部屋の矩形リストから間取り図のSVG（テキスト）を生成する。マイソク/提案書に埋め込む用。
- 外部依存なし（SVGは文字列生成）・ネットワーク非接触。
- 寸法は入力の実データのみ（面積等を捏造しない）。SVGはXSSにならないよう文字列をエスケープ。
"""
from __future__ import annotations

import html

# 部屋種別ごとの淡い塗り（Ledger & Paper=無彩基調・機能色は使わない）。
_FILL = {"LDK": "#f2f4f7", "洋室": "#f8f9fb", "和室": "#f5f3ee",
         "水回り": "#eef1f5", "玄関": "#eef1f5", "バルコニー": "#f6f8fa"}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def floor_plan_svg(rooms: list[dict], *, width: int = 480, height: int = 360,
                   scale: float = 1.0) -> str:
    """rooms=[{name, kind, x, y, w, h}]（座標・寸法は任意単位）→ SVG文字列。
    x/y/w/h は 0〜 の数値。不正な部屋はスキップ（描画を落とさない）。"""
    cells = []
    for r in rooms or []:
        try:
            x = float(r.get("x", 0)) * scale
            y = float(r.get("y", 0)) * scale
            w = float(r.get("w", 0)) * scale
            h = float(r.get("h", 0)) * scale
        except (ValueError, TypeError):
            continue
        if w <= 0 or h <= 0:
            continue
        kind = str(r.get("kind") or "")
        fill = _FILL.get(kind, "#f8f9fb")
        name = _esc(r.get("name") or kind)
        cells.append(
            f'<g><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" stroke="#0a2540" stroke-width="1.5"/>'
            f'<text x="{x + w / 2:.1f}" y="{y + h / 2:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="12" fill="#0a2540" '
            f'font-family="sans-serif">{name}</text></g>')
    body = "".join(cells) or ('<text x="{}" y="{}" text-anchor="middle" font-size="13" '
                              'fill="#64748b">間取りデータがありません</text>'.format(width // 2, height // 2))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="間取り図">'
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
            f'{body}</svg>')


# 代表的な間取りコードのスケルトン配置（概略図・実寸ではない旨は呼出側が明示）。
_LAYOUTS = {
    "1R": [{"name": "居室", "kind": "洋室", "x": 20, "y": 20, "w": 200, "h": 200},
           {"name": "水回り", "kind": "水回り", "x": 20, "y": 230, "w": 90, "h": 90}],
    "1K": [{"name": "洋室", "kind": "洋室", "x": 20, "y": 20, "w": 200, "h": 180},
           {"name": "K", "kind": "LDK", "x": 20, "y": 210, "w": 120, "h": 110},
           {"name": "UB", "kind": "水回り", "x": 150, "y": 210, "w": 70, "h": 110}],
    "1LDK": [{"name": "LDK", "kind": "LDK", "x": 20, "y": 20, "w": 240, "h": 200},
             {"name": "洋室", "kind": "洋室", "x": 270, "y": 20, "w": 180, "h": 200},
             {"name": "水回り", "kind": "水回り", "x": 20, "y": 230, "w": 130, "h": 100}],
    "2LDK": [{"name": "LDK", "kind": "LDK", "x": 20, "y": 20, "w": 220, "h": 180},
             {"name": "洋室1", "kind": "洋室", "x": 250, "y": 20, "w": 200, "h": 130},
             {"name": "洋室2", "kind": "洋室", "x": 250, "y": 160, "w": 200, "h": 130},
             {"name": "水回り", "kind": "水回り", "x": 20, "y": 210, "w": 220, "h": 80}],
    "3LDK": [{"name": "LDK", "kind": "LDK", "x": 20, "y": 20, "w": 200, "h": 170},
             {"name": "洋室1", "kind": "洋室", "x": 230, "y": 20, "w": 220, "h": 110},
             {"name": "洋室2", "kind": "洋室", "x": 230, "y": 140, "w": 110, "h": 150},
             {"name": "洋室3", "kind": "洋室", "x": 350, "y": 140, "w": 100, "h": 150},
             {"name": "水回り", "kind": "水回り", "x": 20, "y": 200, "w": 200, "h": 90}],
}


def schematic_for(layout_code: str) -> str:
    """間取りコード(1R/1K/1LDK/2LDK/3LDK)→概略SVG。未知コードは空図。
    ※概略図であり実寸/実配置ではない（呼出側でその旨を明示すること）。"""
    rooms = _LAYOUTS.get((layout_code or "").strip().upper())
    return floor_plan_svg(rooms or [])


def layouts() -> list[str]:
    return list(_LAYOUTS.keys())
