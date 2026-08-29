"""M-liff-export — 公開可の物件を LIFF 内見予約アプリの properties.json ＋ 写真へ書き出す。

LINE の中で顧客に実物件を並べるための書出層。出力を Cloudflare Pages のデプロイ
ディレクトリに同梱すると、顧客のトーク内 LIFF に実物件が出る。

設計（実コード／DBで確認した事実に基づく・推測しない）:
- **物件マスタ = properties テーブル**（operations.property_register の書込先）。列は
  property_id/address/deal_type/rent_or_price/layout/area/built_year/structure/station/
  walk_min/pet/source/ad_fee/lat/lon/risk_scores/status のみ。**物件名・号室・沿線・階・
  総階数・管理費・特徴・公開フラグは持たない**（schema_cols.COLS["properties"] と一致確認済）。
- そこで LIFF 向けの顧客表示フィールド＋**公開フラグ（opt-in）**は物件ごとの overlay
  `{data_dir}/liff_publish/{property_id}.json` に持つ（property_info.py と同型の JSON sidecar・
  「フォルダ/ファイルが正本、hub.db は派生」の Vault 思想と一致）。**既定=非公開**＝
  勝手に全物件を公開しない。
- **捏造しない**: LIFF Property の必須フィールド（name/rent/layout/area/station/address/
  builtYear/walkMinutes/floor/floorsTotal）が埋まった公開物件だけ export。0/空は「不明」を
  意味するので出さずスキップし skipped に正直に開示する。roomNumber/managementFee/line/
  highlights は空/0 が真実の値（号室なし・管理費0・沿線省略・特徴未記載）なので既定で通す。
- **写真は Vault**（vault.py が `物件/<物件名>/素材/*.jpg` を kind=写真 として vault_assets へ
  索引）が正本。documents/ は書類本文（text）専用で画像を持たない（documents.py で確認）。
  overlay に写真の asset_key を明示（推測パス禁止）するか vault_property（物件フォルダ名）を
  指せば vault_assets から kind=写真 を引く。無ければ photos 省略（LIFF側が間取り線画fallback）。
- 可逆・読取→書出のみ・ネットワーク非接触・stdlib のみ。

出力契約（apps/liff/src/lib/viewing.ts の Property interface）:
  id, name, roomNumber, rent, managementFee, layout, area, line, station, walkMinutes,
  address, builtYear, floor, floorsTotal, dealType, highlights, published, photos?
photos は "/property-photos/{property_id}/{n}.jpg" 形式の相対URL配列。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

# 公開の判定に使う overlay 保存先（物件IDごとの JSON sidecar）
_OVERLAY_DIR = "liff_publish"
# 書出先（Cloudflare Pages 等へそのまま同梱できる相対構成）
_EXPORT_DIR = "liff-export"
_PROPERTIES_JSON = "properties.json"
_PHOTOS_DIR = "property-photos"

# LIFF Property の必須フィールド（0/空は「不明」＝捏造になるので出さずスキップ）。
# roomNumber/managementFee/line/highlights は空/0 が真実（号室なし・管理費0・沿線省略・特徴なし）
# なので必須に含めない。
# dealType（取引態様）は宅建業法34条の明示事項。LINEカードもポータルも広告なので、
# 空のまま公開できてはいけない＝必須に含めて fail-closed にする（2026-08-07）。
_REQUIRED_LIFF = ("name", "rent", "layout", "area", "station", "address",
                  "builtYear", "walkMinutes", "floor", "floorsTotal", "dealType")

# 写真として採用する拡張子（vault.py の kind=写真 と整合）。URLに載せる際に小文字化。
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

_LOCK = threading.Lock()   # threading サーバでの overlay read-modify-write を直列化（lost update 防止）


# --------------------------------------------------------------------------
# overlay 保存（opt-in の公開フラグ＋顧客表示フィールド）
# --------------------------------------------------------------------------

def _safe_id(property_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー_.-]", "_", str(property_id or "")) or "unknown"
    return safe


def _overlay_path(data_dir, property_id: str) -> Path:
    return Path(data_dir) / _OVERLAY_DIR / (_safe_id(property_id) + ".json")


def _atomic_write_json(path: Path, obj) -> None:
    """JSON をアトミックに書く（temp→fsync→os.replace）。truncate 中クラッシュでの全損を防ぐ。"""
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


def load_overlay(data_dir, property_id: str) -> dict:
    """物件の LIFF overlay（公開フラグ＋表示フィールド）を返す（無ければ空）。"""
    p = _overlay_path(data_dir, property_id)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def set_publish(data_dir, property_id: str, *, published: bool, fields: dict | None = None) -> dict:
    """物件の公開フラグ＋顧客表示フィールドを overlay に保存（opt-in・可逆）。
    published=False で非公開へ戻せる（overlay は残す＝再公開で表示フィールドを再入力しないで済む）。
    fields のうち None/未指定の値は既存を保持（last-wins マージ）。アトミック・直列化。"""
    pid = str(property_id or "").strip()
    if not pid:
        raise ValueError("property_id が必要です。")
    with _LOCK:
        ov = load_overlay(data_dir, pid)
        ov["property_id"] = pid
        ov["published"] = bool(published)
        for k, v in (fields or {}).items():
            if v is not None:
                ov[k] = v
        _atomic_write_json(_overlay_path(data_dir, pid), ov)
    return ov


# --------------------------------------------------------------------------
# 型変換（捏造しない＝解釈できない値は「不明」として欠落扱い）
# --------------------------------------------------------------------------

def _to_int(raw):
    """文字列/数値 → 正の int（カンマ・円・全角・空白を許容）。解釈不能/0以下は None。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw > 0 else None
    s = str(raw).strip().replace(",", "").replace("，", "").replace("円", "").replace(" ", "")
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.match(r"^\d+", s)
    if not m:
        return None
    v = int(m.group(0))
    return v if v > 0 else None


def _to_float(raw):
    """文字列/数値 → 正の float（㎡・m2・カンマ・全角を許容）。解釈不能/0以下は None。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    s = str(raw).strip().replace(",", "").replace("，", "").replace(" ", "")
    s = s.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))
    s = re.sub(r"(㎡|m2|平米|畳).*$", "", s)
    m = re.match(r"^\d+(\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    return v if v > 0 else None


def _parse_yen(raw):
    """賃料/価格テキスト → 正の int（円）。'82000' / '82,000円' / '8.2万' / '8万2000' を許容。
    解釈不能/0以下は None（捏造しない）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    s = str(raw).strip().replace(",", "").replace("，", "").replace(" ", "").replace("円", "")
    s = s.translate(str.maketrans("０１２３４５６７８９．万", "0123456789.万"))
    if "万" in s:
        head, _, tail = s.partition("万")
        try:
            man = float(head) if head else 0.0
        except ValueError:
            return None
        tail_m = re.match(r"^\d+", tail)
        extra = int(tail_m.group(0)) if tail_m else 0
        v = int(round(man * 10000)) + extra
        return v if v > 0 else None
    m = re.match(r"^\d+", s)
    if not m:
        return None
    v = int(m.group(0))
    return v if v > 0 else None


def _to_highlights(raw) -> list:
    """特徴 → 文字列リスト。list はそのまま、文字列は改行/読点/カンマで分割。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if raw is None:
        return []
    parts = re.split(r"[\n,、，・/]", str(raw))
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------
# master(properties 行) ＋ overlay → LIFF Property
# --------------------------------------------------------------------------

def to_liff_property(master: dict, overlay: dict) -> tuple[dict | None, list]:
    """物件マスタ行＋overlay → (LIFF Property dict, missing[]).
    必須フィールドが欠ければ (None, missing) を返す（export しない＝捏造しない）。
    photos は build_export が写真コピー後に付与する（ここでは付けない）。"""
    m = master or {}
    ov = overlay or {}
    pid = str(m.get("property_id") or "").strip()

    # overlay 優先で数値を解釈（overlay に明示があれば master テキストを上書き）。
    rent = _parse_yen(ov.get("rent")) if ov.get("rent") not in (None, "") else _parse_yen(m.get("rent_or_price"))
    area = _to_float(ov.get("area")) if ov.get("area") not in (None, "") else _to_float(m.get("area"))
    built = _to_int(ov.get("built_year")) if ov.get("built_year") not in (None, "") else _to_int(m.get("built_year"))
    walk = _to_int(ov.get("walk_min")) if ov.get("walk_min") not in (None, "") else _to_int(m.get("walk_min"))
    floor = _to_int(ov.get("floor"))
    floors_total = _to_int(ov.get("floors_total"))

    name = str(ov.get("name") or "").strip()
    layout = str(m.get("layout") or "").strip()
    station = str(m.get("station") or "").strip()
    address = str(m.get("address") or "").strip()

    # managementFee は 0 が真実（管理費なし）＝解釈不能なら 0 扱い。
    mgmt = _to_int(ov.get("management_fee"))
    prop = {
        "id": pid,
        "name": name,
        "roomNumber": str(ov.get("room_number") or "").strip(),
        "rent": rent,
        "managementFee": mgmt if mgmt is not None else 0,
        "layout": layout,
        "area": area,
        "line": str(ov.get("line") or "").strip(),
        "station": station,
        "walkMinutes": walk,
        "address": address,
        "builtYear": built,
        "floor": floor,
        "floorsTotal": floors_total,
        "highlights": _to_highlights(ov.get("highlights")),
        # 取引態様＝貸主／売主／代理／媒介の別。広告のたびに明示が要る（宅建業法34条）。
        "dealType": str(ov.get("deal_terms") or m.get("torihiki_taiyo")
                        or m.get("deal_terms") or "").strip(),
        "published": True,   # ここに来る＝公開opt-in済（build 側で published=True のみ通す）
    }

    missing = []
    for f in _REQUIRED_LIFF:
        v = prop.get(f)
        if v is None or (isinstance(v, str) and not v):
            missing.append(f)
    if missing:
        return None, missing
    return prop, []


# --------------------------------------------------------------------------
# 写真の解決（Vault が正本・推測パス禁止）
# --------------------------------------------------------------------------

def _resolve_asset(data_dir, key: str) -> Path:
    p = Path(str(key))
    return p if p.is_absolute() else Path(data_dir) / str(key)


def resolve_photo_sources(data_dir, property_id: str, overlay: dict) -> list:
    """物件写真の元ファイル群を返す（実在するものだけ・順序保持）。
    ①overlay['photos'] に asset_key を明示 → それを使う（推測パス禁止＝明示指定）。
    ②overlay['vault_property'] に物件フォルダ名 → vault_assets の kind=写真 を filename 昇順で引く。
    ③どちらも無ければ空（photos 省略）。"""
    ov = overlay or {}
    sources: list = []
    explicit = ov.get("photos")
    if isinstance(explicit, list) and explicit:
        for k in explicit:
            sources.append(_resolve_asset(data_dir, k))
    elif ov.get("vault_property"):
        try:
            from hub_core.store import SqliteStore
            st = SqliteStore(Path(data_dir) / "hub.db")
            rows = st.query("vault_assets", "property = ? AND kind = ?",
                            (str(ov.get("vault_property")), "写真"))
        except Exception:
            rows = []
        for r in sorted(rows, key=lambda x: str(x.get("filename") or "")):
            sources.append(_resolve_asset(data_dir, r.get("asset_key") or ""))
    # 実在＋写真拡張子のものだけ（欠落は正直に落とす＝捏造しない）
    return [p for p in sources if p.is_file() and p.suffix.lower() in _PHOTO_EXTS]


def _copy_photos(data_dir, property_id: str, sources: list) -> list:
    """写真を liff-export/property-photos/{id}/ に 1.ext,2.ext… でコピーし相対URL配列を返す。
    冪等: 呼出前に build_export が property-photos を全消去して決定的に再構築する。"""
    if not sources:
        return []
    dest_dir = Path(data_dir) / _EXPORT_DIR / _PHOTOS_DIR / _safe_id(property_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    urls = []
    for i, src in enumerate(sources, start=1):
        ext = src.suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        fname = f"{i}{ext}"
        shutil.copyfile(src, dest_dir / fname)
        urls.append(f"/{_PHOTOS_DIR}/{_safe_id(property_id)}/{fname}")
    return urls


# --------------------------------------------------------------------------
# エクスポート本体
# --------------------------------------------------------------------------

def build_export(data_dir) -> dict:
    """公開opt-in済の物件を走査し properties.json ＋ 写真を書き出す。
    返り値: {exported, properties[], skipped[{id,missing[]}], photos_copied, path, dir}。
    - 完全な公開物件 → properties.json に出す＋写真コピー。
    - 欠損のある公開物件 → skipped に開示（json には出さない）。
    - 非公開（overlay 無し or published=False）→ 対象外（skipped にも出さない）。
    冪等: property-photos を毎回全消去して決定的に再構築、properties.json は property_id 昇順・
    timestamp を含めない＝再実行でバイト同一。"""
    from hub_core.store import SqliteStore
    data_dir = Path(data_dir)
    st = SqliteStore(data_dir / "hub.db")
    masters = st.query("properties")

    export_root = data_dir / _EXPORT_DIR
    photos_root = export_root / _PHOTOS_DIR
    # 冪等・stale除去: 派生アーティファクトの写真ツリーを毎回ゼロから作り直す（正本は触らない）。
    if photos_root.exists():
        shutil.rmtree(photos_root)

    exported: list = []
    skipped: list = []
    photos_copied = 0
    for m in sorted(masters, key=lambda r: str(r.get("property_id") or "")):
        pid = str(m.get("property_id") or "").strip()
        if not pid:
            continue
        ov = load_overlay(data_dir, pid)
        if not ov.get("published"):
            continue   # 非公開＝対象外（勝手に公開しない）
        prop, missing = to_liff_property(m, ov)
        if prop is None:
            skipped.append({"id": pid, "missing": missing})
            continue
        photos = _copy_photos(data_dir, pid, resolve_photo_sources(data_dir, pid, ov))
        if photos:
            prop["photos"] = photos
            photos_copied += len(photos)
        exported.append(prop)

    export_root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(export_root / _PROPERTIES_JSON, exported)
    return {
        "exported": len(exported),
        "properties": exported,
        "skipped": skipped,
        "photos_copied": photos_copied,
        "path": str(export_root / _PROPERTIES_JSON),
        "dir": str(export_root),
    }
