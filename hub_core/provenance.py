"""M-provenance — 素材の出所・許諾プロヴェナンス（Wave4磨き・著作権 fail-closed・¥0）。

マイソク等に焼く素材（写真/間取り図）を「正規に手に入れた素材だけ」に構造的に限定する。
設計 = `~/dev/ri-os-audit/ASSET_REUSE_DESIGN.md`（発散4案→敵対検証→統合）。

核心（load-bearing）:
- マイソクの写真スロットは **Vault 参照（asset_key or sha256:）のみ** 許可。外部URL/任意 data: URI/
  Vault外パスは publish 時に fail-closed で拒否＝素材ハブを通らないバイトは販売図面に焼けない。
- 各素材は隣に sidecar `<asset>.prov.json`（ファイル正本・reindex生存・hub.db非依存）を持つ。
  origin ∈ {self, received, generated} のみ（**scraped/portal という語彙が構造的に無い**＝無断転載を
  表現できない）。sidecar 無し/不一致は unknown＝公開不可。
- 公開ゲートは **render 出力から導出した manifest** を検証（呼び手申告でない）。1素材でも不可なら
  マイソク全体を公開拒否（all-or-nothing）。received は物件スコープ固定（A物件の許諾でB物件に使えない）。
- 正直な裾: origin=self の真正性（自分で撮った/作った）はサーバ検証不能＝記名責任＋監査で抑止。

stdlib のみ（hashlib/json/base64）・ネットワーク非接触。
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

ORIGINS = ("self", "received", "generated")   # scraped/portal は存在しない＝無断転載の語彙が無い
# rights: advertise=広告掲載, obi_swap=帯替え, portal=ポータル出稿, modify=加工
PURPOSE_RIGHT = {"display": "advertise", "advertise": "advertise",
                 "portal": "portal", "obi_swap": "obi_swap"}
_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


class ProvenanceError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def record_hash(rec: dict) -> str:
    """sidecar の内容ハッシュ（record_hash 自身と chain は除外して正規化JSON）。obi と同型。"""
    core = {k: v for k, v in rec.items() if k not in ("record_hash", "chain")}
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def sidecar_path(asset_file: Path) -> Path:
    return asset_file.with_name(asset_file.name + ".prov.json")


def write_sidecar(asset_file: Path, *, origin: str, actor: str, rights: list[str],
                  property: str = "", source_company: str = "", evidence_sha256: str = "",
                  derived_from: list[str] | None = None) -> dict:
    """素材に sidecar を発行。asset_sha256 は実ファイル内容から算出（内容束縛＝差替で失効）。"""
    if origin not in ORIGINS:
        raise ProvenanceError(400, f"origin は {ORIGINS} のいずれか（無断転載の origin は存在しません）。")
    if not asset_file.is_file():
        raise ProvenanceError(404, f"素材ファイルがありません: {asset_file}")
    rec = {
        "asset_sha256": _sha256_bytes(asset_file.read_bytes()),
        "origin": origin, "actor": actor, "rights": sorted(set(rights or [])),
        "property": property, "source_company": source_company,
        "evidence_sha256": evidence_sha256,
        "derived_from": sorted(set(derived_from or [])),
    }
    rec["record_hash"] = record_hash(rec)
    sidecar_path(asset_file).write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def read_sidecar(asset_file: Path) -> dict | None:
    sp = sidecar_path(asset_file)
    if not sp.is_file():
        return None
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def class_of(asset_file: Path) -> str:
    """素材の出所クラス。sidecar 無し/破損/内容不一致は unknown（＝公開不可）。"""
    rec = read_sidecar(asset_file)
    if not rec:
        return "unknown"
    if record_hash(rec) != rec.get("record_hash"):
        return "unknown"                       # 改竄痕跡
    if rec.get("asset_sha256") != _sha256_bytes(asset_file.read_bytes()):
        return "unknown"                       # 差替（内容束縛失効）
    o = rec.get("origin")
    return o if o in ORIGINS else "unknown"


def _resolve_vault_file(data_dir, ref: str) -> Path | None:
    """写真スロット参照（asset_key 相対パス or 'sha256:HEX'）→ Vault内の実ファイル。
    Vault(物件/…)の外は None（外部URL/data:/絶対パスは解決しない＝fail-closed の起点）。"""
    root = Path(data_dir).resolve()
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("sha256:"):
        want = ref[len("sha256:"):].strip().lower()
        bukken = root / "物件"
        if not bukken.is_dir():
            return None
        for f in bukken.rglob("*"):
            if f.is_file() and not f.name.endswith(".prov.json"):
                try:
                    if _sha256_bytes(f.read_bytes()) == want:
                        return f
                except OSError:
                    continue
        return None
    # asset_key = data_dir からの相対パス。Vault(物件/…)配下のみ許可（traversal封じ）。
    cand = (root / ref).resolve()
    if not cand.is_relative_to(root / "物件") or not cand.is_file():
        return None
    if cand.name.endswith(".prov.json"):
        return None
    return cand


def _data_uri(asset_file: Path) -> str:
    mime = _IMG_MIME.get(asset_file.suffix.lower(), "application/octet-stream")
    b64 = base64.b64encode(asset_file.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def check_asset(data_dir, asset_file: Path, prop: str, purpose: str) -> dict:
    """1素材が prop のマイソクに purpose で使えるか（fail-closed）。可なら {asset_sha256} を返す。"""
    cls = class_of(asset_file)
    if cls == "unknown":
        raise ProvenanceError(403, f"出所不明/未許諾の素材です（sidecar無し/改竄/差替）: {asset_file.name}")
    rec = read_sidecar(asset_file)
    need = PURPOSE_RIGHT.get(purpose, purpose)
    if need not in (rec.get("rights") or []):
        raise ProvenanceError(403, f"この用途({purpose})の権利がありません: {asset_file.name}")
    if cls == "received":
        # 元付受領=物件スコープ固定（A物件の許諾でB物件に使えない）＋根拠(obi許諾)実在
        if (rec.get("property") or "") != prop:
            raise ProvenanceError(403, f"受領素材の物件スコープ外です（{rec.get('property')}≠{prop}）: {asset_file.name}")
        if not rec.get("source_company") or not rec.get("evidence_sha256"):
            raise ProvenanceError(403, f"受領素材に元付・根拠がありません: {asset_file.name}")
    if cls == "generated":
        # 生成物=親を再帰検証（OCR/トレースで受領/unknownを洗浄できない）
        for parent in rec.get("derived_from") or []:
            pf = _resolve_vault_file(data_dir, parent)
            if pf is None:
                raise ProvenanceError(403, f"生成素材の親が解決できません: {asset_file.name}")
            check_asset(data_dir, pf, prop, purpose)
    return {"asset_sha256": rec.get("asset_sha256"), "origin": cls}


def resolve_photo_slot(data_dir, ref: str, prop: str, *, publish: bool, purpose: str = "advertise"):
    """写真スロット値 → (焼き込むsrc, asset_sha256 or None)。
    - 空 → ("", None)（空画像）。
    - Vault素材参照 かつ 許諾OK → (data:URI, sha256)。
    - 外部URL/任意data:/Vault外/未許諾 → publish時は ProvenanceError(fail-closed)、
      preview時はプレースホルダ（"", None）で描画（印刷抜けも封じる）。"""
    ref = (ref or "").strip()
    if not ref:
        return ("", None)
    f = _resolve_vault_file(data_dir, ref)
    if f is None:
        if publish:
            raise ProvenanceError(403, "写真は素材ハブ（Vault）の素材のみ使用できます。外部URL/貼付画像は"
                                       "公開に使えません（無断転載防止）。")
        return ("", None)   # preview: 未許諾素材は空（プレースホルダ）
    try:
        res = check_asset(data_dir, f, prop, purpose)
    except ProvenanceError:
        if publish:
            raise
        return ("", None)
    return (_data_uri(f), res["asset_sha256"])
