"""マイソクの売り文句を、物件の事実から組み立てる。

考え方。「魅力的に書いて」と丸投げすると、出てくるのは「日当たり抜群」「厳選の
掘出し物件」のような、表示規約が名指しで制限している言葉になる。だからここでは
**言葉を思いつく**のではなく、**手元にある事実を並べ替える**。

主張には裏付けを要求する。「陽当たりのよい」と書けるのは方位の欄に南（東南・南西）が
入っているときだけ、「駅近」と書けるのは徒歩分数が実際に短いときだけ。裏付けの欄が
空なら、その言い回しは候補に出てこない。出した候補が何を根拠にしているかは
`Draft.basis` で返すので、画面で示せる。

生成した候補は必ず `ad_rules.review` を通してから返す。指摘の付いた候補は捨てる
（自分が作った文が自分の検査に落ちる、という事態を外に出さない）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from hub_core import ad_rules

MAX_LEN = 34  # A4販売図面の見出しに収まる目安


@dataclass(frozen=True)
class Draft:
    text: str
    basis: tuple      # この文を書ける根拠にした (欄名, 値) の並び
    kind: str         # どの切り口か（立地/広さ/築年/設備）

    def as_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind,
                "basis": [{"field": f, "value": v} for f, v in self.basis]}


def _n(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


_SOUTH = ("南", "南東", "南西", "東南", "西南")
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_WALK_RE = re.compile(r"徒歩\s*(?:約)?\s*(\d+)\s*分")


def _walk_min(fields: dict, distance_m=None):
    """表示してよい徒歩分数。道路距離があればそこから計算し、無ければ
    既に書かれている分数を読む。どちらも無ければ None（＝立地の切り口は出さない）。"""
    if distance_m not in (None, "", 0):
        try:
            return ad_rules.walk_minutes_for(distance_m)
        except (TypeError, ValueError):
            pass
    m = _WALK_RE.search(_n(fields.get("access")) + " " + _n(fields.get("nearest_station")))
    return int(m.group(1)) if m else None


def _station(fields: dict) -> str:
    s = _n(fields.get("nearest_station"))
    s = _WALK_RE.sub("", s).strip("／/・ 　")
    return s


def _area_sqm(fields: dict):
    for k in ("building_area", "land_area"):
        m = _NUM_RE.search(_n(fields.get(k)))
        if m:
            try:
                return float(m.group(1)), k
            except ValueError:
                continue
    return None, ""


def _built_year(fields: dict):
    m = re.search(r"(\d{4})", _n(fields.get("built")))
    return int(m.group(1)) if m else None


def _setsubi_items(fields: dict) -> list:
    raw = _n(fields.get("setsubi"))
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[、,／/\n・]+", raw) if p.strip()]
    return [p for p in parts if 2 <= len(p) <= 12][:4]


def suggest(fields: dict, *, distance_m=None, today=None, limit: int = 4) -> list:
    """売り文句の候補を返す。根拠の無い言い回しは最初から作らない。"""
    f = dict(fields or {})
    drafts: list = []

    station = _station(f)
    walk = _walk_min(f, distance_m)
    layout = _n(f.get("floor_plan"))
    area, area_key = _area_sqm(f)
    year = _built_year(f)
    direction = _n(f.get("direction"))
    structure = _n(f.get("structure"))
    parking = _n(f.get("parking"))
    setsubi = _setsubi_items(f)
    ptype = _n(f.get("property_type"))

    def add(text, basis, kind):
        t = re.sub(r"\s+", " ", text).strip(" ・、")
        if not t or len(t) > MAX_LEN:
            return
        drafts.append(Draft(text=t, basis=tuple(basis), kind=kind))

    # 立地 — 駅と徒歩分数が両方そろっているときだけ
    if station and walk:
        head = f"{station} 徒歩{walk}分"
        if layout:
            add(f"{head}・{layout}", [("nearest_station", station),
                                      ("access", f"徒歩{walk}分"),
                                      ("floor_plan", layout)], "立地")
        if area:
            add(f"{head}・{area:g}㎡", [("nearest_station", station),
                                       ("access", f"徒歩{walk}分"),
                                       (area_key, f"{area:g}㎡")], "立地")
        # 「駅から近い」と書けるのは実際に近いときだけ（10分以内を目安）
        if walk <= 10 and ptype:
            add(f"駅から徒歩{walk}分の{ptype}",
                [("access", f"徒歩{walk}分"), ("property_type", ptype)], "立地")

    # 採光 — 方位の欄に南系が入っているときだけ「陽当たり」に触れる
    if any(d == direction or direction.startswith(d) for d in _SOUTH):
        if layout:
            add(f"{direction}向きの{layout}。陽当たりのよい住まい",
                [("direction", direction), ("floor_plan", layout)], "採光")
        elif area:
            add(f"{direction}向き・{area:g}㎡。陽当たりのよい住まい",
                [("direction", direction), (area_key, f"{area:g}㎡")], "採光")

    # 築年 — 「新築」は規約の定義を満たすときだけ。満たさなければ年を書く
    if year:
        this_year = None
        if today:
            try:
                this_year = int(str(today)[:4])
            except (TypeError, ValueError):
                this_year = None
        parts = [f"{year}年築"]
        if structure:
            parts.append(structure)
        if layout:
            parts.append(layout)
        add("・".join(parts), [("built", f"{year}年"),
                              ("structure", structure), ("floor_plan", layout)], "築年")
        if this_year and this_year - year >= 2 and this_year - year <= 7 and layout:
            add(f"築{this_year - year}年の{layout}",
                [("built", f"{year}年"), ("floor_plan", layout)], "築年")

    # 設備 — 欄に書かれているものだけを並べる
    if setsubi:
        head = "・".join(setsubi[:2])
        if layout:
            add(f"{head}。{layout}の{ptype or '住まい'}",
                [("setsubi", head), ("floor_plan", layout)], "設備")
        else:
            add(f"{head}のある{ptype or '住まい'}", [("setsubi", head)], "設備")

    # 駐車場 — 欄に書いてあるときだけ
    if parking and not re.search(r"無|なし|不可", parking):
        base = layout or ptype
        if base:
            add(f"駐車場{parking}・{base}",
                [("parking", parking), ("floor_plan", layout)], "設備")

    # 自分の検査に落ちる候補は外へ出さない
    kept, seen = [], set()
    for d in drafts:
        if d.text in seen:
            continue
        if ad_rules.check_text(d.text, "title_copy"):
            continue
        if ad_rules.check_shinchiku(d.text, f.get("built"), None, today, "title_copy"):
            continue
        seen.add(d.text)
        kept.append(d)
    # 切り口が偏らないよう、種類ごとに1本ずつ拾ってから残りを足す
    ordered, used_kind = [], set()
    for d in kept:
        if d.kind not in used_kind:
            ordered.append(d)
            used_kind.add(d.kind)
    ordered += [d for d in kept if d not in ordered]
    return ordered[:limit]


def missing_for_copy(fields: dict) -> list:
    """売り文句を作るのに足りていない欄を、人が読める形で返す。

    候補が0本のときに「作れません」だけ出すのは不親切なので、何を入れれば
    作れるようになるかを言う。
    """
    f = dict(fields or {})
    want = [("nearest_station", "最寄駅"), ("access", "交通（徒歩分数）"),
            ("floor_plan", "間取り"), ("building_area", "面積"),
            ("built", "築年月"), ("setsubi", "設備状況")]
    return [label for key, label in want if not _n(f.get(key))]
