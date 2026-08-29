"""hub_core/property_info.py — 物件情報の完成度＋不足＝タスク＋取得先への直リンク。

「全部の情報を楽に埋める」の管理層。合流済みレコード(ocr_structure.merge_property_fields)を受け、
マイソク/重説を埋めるのに要る情報のうち何が揃い何が足りないかを判定し、不足を「取得先リンク付きタスク」
として返す。取得先は公的サービスの**静的URL**（ユーザーがクリックして飛ぶ＝自動取得・自動送信はしない・
間接インジェクション非該当）。捏造しない＝実在する値だけ「揃っている」に数える。stdlibのみ。
"""
from __future__ import annotations

import urllib.parse

# 公的サービスの取得先（静的・全国共通の入口）。自動アクセスはしない＝ユーザーがクリックする直リンク。
GETSITE_HAZARD = "https://disaportal.gsi.go.jp/"          # ハザードマップポータルサイト(国土地理院)
GETSITE_TOUKI = "https://www1.touki.or.jp/"               # 登記情報提供サービス
GETSITE_TOSHIKEIKAKU = "https://www2.wagmap.jp/"          # 自治体都市計画GISの一般的入口(用途地域・可変)

# マイソク/重説を埋めるのに要る情報。(field, ラベル, 取得先URL or None, 分類)。
# url=None は「現地・売主・管理会社に請求」等ウェブで取れないもの。分類=doc(書類)/loc(住所由来)/base(基本)。
_REQUIREMENTS = [
    ("property_name", "物件名", None, "base"),
    ("price", "価格", None, "base"),
    ("address", "所在地", None, "base"),
    ("area", "専有／建物面積", None, "base"),
    ("layout", "間取り", None, "base"),
    ("built", "築年月", None, "base"),
    ("structure", "構造", None, "base"),
    ("transit", "交通・最寄駅", None, "base"),
    # 住所から自動導出できる（B方針で自動化予定・当面は取得先リンク）
    ("youto", "用途地域", GETSITE_TOSHIKEIKAKU, "loc"),
    ("hazard", "洪水／土砂／津波ハザード", GETSITE_HAZARD, "loc"),
    # 別書類から取る
    ("touki", "登記（所有者・地番・登記面積・権利）", GETSITE_TOUKI, "doc"),
    ("management_fee", "管理費・修繕積立金", None, "doc"),      # 重要事項調査報告書
    ("total_units", "総戸数", None, "doc"),                     # 重要事項調査報告書
    ("genkyo", "現況・設備・瑕疵", None, "doc"),                # 物件状況報告書・設備表
]


def map_url(address: str) -> str:
    """所在地 → 地図（現地確認への直リンク）。住所を渡すだけ・自動取得はしない。"""
    a = (address or "").strip()
    if not a:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(a)


def property_completion(merged: dict) -> dict:
    """合流済みレコード → 完成度・揃っている項目(出典つき)・不足タスク(取得先リンク)。
    merged は ocr_structure.merge_property_fields の返り値（_sources 付き）を想定。捏造しない。"""
    sources = (merged or {}).get("_sources", {})
    have = {k for k, v in (merged or {}).items() if not str(k).startswith("_") and v}
    filled = [{"field": k, "label": _l, "value": merged[k], "source": sources.get(k, "")}
              for k, _l, _u, _c in _REQUIREMENTS if k in have]
    missing = [{"field": k, "label": lbl, "url": url, "kind": cat}
               for k, lbl, url, cat in _REQUIREMENTS if k not in have]
    got, total = len(filled), len(_REQUIREMENTS)
    return {
        "pct": round(100 * got / total) if total else 0,
        "have": got, "total": total,
        "filled": filled,
        "missing": missing,
        "map_url": map_url(str((merged or {}).get("address") or "")),
    }


def _store_path(data_dir, case_id: str):
    from pathlib import Path
    import re
    safe = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー_.-]", "_", str(case_id or "")) or "unknown"
    d = Path(data_dir) / "property_info"
    return d / (safe + ".json")


def load_property_info(data_dir, case_id: str) -> dict:
    """物件（案件）に保存済みの合流レコードを返す（無ければ空）。"""
    import json
    p = _store_path(data_dir, case_id)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


import threading as _threading
_PI_LOCK = _threading.Lock()   # Threadingサーバでの read-modify-write を直列化（lost update防止）


def _atomic_write_json(path, obj):
    """JSONをアトミックに書く（temp→fsync→os.replace）。truncate中クラッシュでの全損を防ぐ。"""
    import json
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _regroup_by_source(record: dict) -> list:
    """{field:value}＋_sources → [(実書類名, {field:value})] に再グループ化（合流の出典優先度[登記>図面]を効かせる）。"""
    src = (record or {}).get("_sources", {})
    groups: dict = {}
    for k, v in (record or {}).items():
        if str(k).startswith("_") or not v:
            continue
        groups.setdefault(src.get(k, ""), {})[k] = v
    return [(name, fields) for name, fields in groups.items()]


def save_property_info(data_dir, case_id: str, merged: dict) -> dict:
    """合流レコードを物件（案件）に保存。既存があれば**実書類名で再グループ化して合流**（登記等の法的真実源が
    図面の面積・所在地を上書きする優先度を、増分アップロードでも保つ）。捏造しない＝実在した値だけ。アトミック・直列化。"""
    from hub_core.ocr_structure import merge_property_fields
    with _PI_LOCK:
        existing = load_property_info(data_dir, case_id)
        if existing:
            combined = merge_property_fields(_regroup_by_source(existing) + _regroup_by_source(merged or {}))
            # 状態系など出典を持たない付随フィールド（bukkaku_status/reins_*）は保持
            for k, v in existing.items():
                if not str(k).startswith("_") and k not in combined and v:
                    combined[k] = v
        else:
            combined = dict(merged or {})
        _atomic_write_json(_store_path(data_dir, case_id), combined)
    return combined


def set_property_fields(data_dir, case_id: str, updates: dict) -> dict:
    """特定フィールドを直接セット（合流でなく上書き・REINS状態や物確状態の更新用・last-wins）。アトミック・直列化。"""
    with _PI_LOCK:
        info = load_property_info(data_dir, case_id)
        info.update({k: v for k, v in (updates or {}).items() if v is not None})
        _atomic_write_json(_store_path(data_dir, case_id), info)
    return info


def completion_tasks(merged: dict, *, case_id: str = "") -> list:
    """不足情報を『取得先リンク付きタスク』に変換（あいのてのタスク/／go に載せられる形）。
    url があるものは source_url に載る＝既存の /go(allowlist) で安全に飛べる。捏造しない。"""
    c = property_completion(merged)
    tasks = []
    for m in c["missing"]:
        title = {"loc": "住所から確認: ", "doc": "書類を取得: ", "base": "入力: "}.get(m["kind"], "") + m["label"]
        tasks.append({
            "title": title,
            "source_url": m["url"] or "",
            "kind": m["kind"],
            "field": m["field"],
            "case_id": case_id,
        })
    return tasks
