"""hub_core/prs.py — PRS(物件災害リスクスコア)への薄いクライアント。

住所→GSI(国土地理院)ジオコード(鍵不要)→PRS /v2/assess。RISK_API_KEY/PRS_API_KEY 未設定や呼び出し失敗時は
graceful に「PRS未接続」を返し**スコアを捏造しない**(本番fallbackの『正直に降りる』設計を踏襲)。
金メッキ禁止: risk_summary は較正健全な種別(洪水等)のみを短文化し、未較正値は出さない。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

_NOT_CONNECTED = {"prs_status": "PRS未接続", "score": None}


def configured() -> bool:
    return bool((os.environ.get("RISK_API_KEY") or os.environ.get("PRS_API_KEY") or "").strip())


def _geocode_local(address: str):
    """ローカルジオコーダ（Geolonia等）。GEOCODER_LOCAL_CMD が設定されていれば subprocess で呼ぶ。
    未配備なら None（外部送信しない＝主権優先）。町丁目層データのライセンス確認は配備側の責任。"""
    import os
    cmd = str(os.environ.get("GEOCODER_LOCAL_CMD") or "").strip()
    if not cmd:
        return None
    try:
        import subprocess
        out = subprocess.run(cmd.split() + [address], capture_output=True, text=True,
                             timeout=float(os.environ.get("GEOCODE_TIMEOUT_SECONDS", "10")))
        if out.returncode != 0 or not out.stdout.strip():
            return None
        obj = json.loads(out.stdout)
        return float(obj["lat"]), float(obj["lon"]), obj.get("title", "")
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def _geocode(address: str):
    """住所→(lat, lon, matched_title)。経路は sovereignty.geocoder_mode() が決める:
    'off'=住所を外に出さない(None)・'local'=ローカルジオコーダ(未配備なら None)・'gsi'=国土地理院オンライン。
    既定は主権優先（ネットワーク未許可なら off）。失敗時 None。"""
    from hub_core import sovereignty
    mode = sovereignty.geocoder_mode()
    if mode == "off":
        # 主権優先: 住所を端末外に出さない（ローカルジオコーダ未配備・ネットワーク未許可）
        return None
    if mode == "local":
        # ローカルジオコーダ（Geolonia等）を差す枠。未配備なら None（外部送信しない）。
        return _geocode_local(address)
    try:
        url = "https://msearch.gsi.go.jp/address-search/AddressSearch?q=" + quote(address)
        req = urllib.request.Request(url, headers={"User-Agent": "ri-hub/geocode"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("GEOCODE_TIMEOUT_SECONDS", "10"))) as r:
            arr = json.loads(r.read().decode("utf-8"))
        if arr:
            lon, lat = arr[0]["geometry"]["coordinates"]
            return float(lat), float(lon), arr[0].get("properties", {}).get("title", "")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return None
    return None


def assess(address: str | None = None, lat=None, lon=None) -> dict:
    """物件のリスク評価。鍵が無ければ PRS未接続。捏造しない。"""
    key = (os.environ.get("RISK_API_KEY") or os.environ.get("PRS_API_KEY") or "").strip()
    if not key:
        return dict(_NOT_CONNECTED, reason="RISK_API_KEY 未設定")
    if lat is None or lon is None:
        if not address:
            return dict(_NOT_CONNECTED, reason="住所/座標なし")
        g = _geocode(address)
        if not g:
            return dict(_NOT_CONNECTED, reason=f"ジオコード失敗({address})")
        lat, lon = g[0], g[1]
    try:
        latf, lonf = float(lat), float(lon)
    except (TypeError, ValueError):
        return dict(_NOT_CONNECTED, reason="lat/lon が数値でない")
    base = (os.environ.get("RISK_API_BASE") or os.environ.get("PRS_API_BASE")
            or "https://api.propriskapi.com").rstrip("/")
    try:
        data = json.dumps({"lat": latf, "lon": lonf}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base + "/v2/assess", data=data, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key, "X-API-Key": key})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("PRS_TIMEOUT_SECONDS", "15"))) as r:
            res = json.loads(r.read().decode("utf-8"))
        return {"prs_status": "OK", "lat": latf, "lon": lonf, "result": res}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        return dict(_NOT_CONNECTED, reason=f"PRS呼出失敗: {type(e).__name__}")


def _flood_score(res: dict):
    """較正健全な洪水(現在)スコア 0-100 を取り出す。PRS実構造: risks.future_flood.facts.current_flood_score。
    未較正種別(heat=循環論法/future_flood投影=confidence LOW)は使わない=金メッキ禁止。"""
    r = (res or {}).get("result") or {}
    risks = r.get("risks") or {}
    ff = risks.get("future_flood") or {}
    facts = ff.get("facts") or {}
    sc = facts.get("current_flood_score")
    try:
        return int(round(float(sc))) if sc is not None else None
    except (TypeError, ValueError):
        return None


def _band(score) -> str:
    if score is None:
        return ""
    s = float(score)
    return "低" if s < 25 else ("中" if s < 50 else ("高" if s < 75 else "極高"))


def risk_summary(res: dict) -> str:
    """assess結果→短い risk_scores 文字列(較正健全な洪水のみ・未較正は出さない)。未接続は空。"""
    if not res or res.get("prs_status") != "OK":
        return ""
    sc = _flood_score(res)
    return f"洪水{sc}/100({_band(sc)})" if sc is not None else ""


def juusetsu_note(address=None, lat=None, lon=None) -> dict:
    """重説の水害ハザード欄向け『参考・事前スクリーニング』文。
    宅建業法上の法定ハザードマップ位置説明の**代替ではない**旨を必ず明記(過大表示・循環論法禁止)。"""
    res = assess(address=address, lat=lat, lon=lon)
    if res.get("prs_status") != "OK":
        return {"connected": False, "score": None,
                "text": "（PRS未接続：水害ハザードは当該自治体公表のハザードマップで確認・記載のこと）"}
    sc = _flood_score(res)
    if sc is None:
        return {"connected": True, "score": None,
                "text": "（PRS：較正済みの洪水スコアを取得できませんでした。自治体公表図で要確認）"}
    text = (f"【参考・PRS事前スクリーニング】洪水リスクスコア {sc}/100（{_band(sc)}）。"
            "出典: PRS（国土数値情報・浸水想定）。"
            "※本欄は事前スクリーニングであり、宅地建物取引業法施行規則16条の4の3に基づく"
            "水害ハザードマップ上の物件位置の説明は、当該市町村が公表する最新のハザードマップで"
            "確認のうえ記載すること。")
    return {"connected": True, "score": sc, "band": _band(sc), "text": text}
