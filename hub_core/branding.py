"""hub_core/branding.py — 会社ブランドの正本・版管理・取り込み。

設計の芯（お客様に届く面は各社ブランド）:
  管理画面（社内向け）        = あいのて のブランド
  マイソク・LINE・書類（お客様向け）= **利用する不動産会社**のブランド

会社が一度ブランドを決めたら、帯・LINEカード・重説の業者欄・提案書まで同じ値が回る。
そのため正本は1つ（company.json）に置き、ここはその**版管理と取り込み**だけを持つ。

版管理（戻せること）:
  - 保存のたびに `auth/brand_history.jsonl` へ1行追記（version・保存時刻・保存者・値・由来）。
  - `restore(version)` で過去の版を現在へ戻す。**戻す操作もまた1つの新しい版**として積む
    ＝履歴は消さない（消す設計だと「戻したつもりが消えていた」が起きる）。
  - 履歴は上限 MAX_HISTORY で古い順に落とす（容量が無制限に増えない）。

取り込み（無料 / 有料の枝分かれ）:
  - **無料（既定・ローカル完結・¥0）**: 手入力。ロゴ画像をアップロードした場合のみ、
    画素から代表色を1色取り出して色の候補にする（stdlib のみ・外部送信なし）。
  - **有料（外部API・人間ゲート）**: 会社のWebサイトURL やロゴから、色・書体・トーンまで
    推測して一式を提案する。品質は外部の専用モデルの方が高いので自前で持たない。
    ここでは `BrandSource` の口だけ定義し、実装は provider として差し込む
    （APIキー・課金・外部送信は利用者の明示操作が必要で、未設定なら無料経路に落ちる）。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - initial public target is macOS
    fcntl = None

JST = timezone(timedelta(hours=9))
MAX_HISTORY = 50          # 版の保持上限（古い順に落とす。容量を無制限に増やさない）
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 版管理する項目＝**お客様に見える会社プロファイル一式**。
# ブランド色だけを版にすると、restore で「過去の免許番号＋現在の住所」という
# 存在しない業者表示が合成される（2026-08-06 Codexレビュー指摘）。法定表示・連絡先・
# ブランドを同一版で戻すため、1版は完全スナップショットにする。
PROFILE_KEYS = ("name", "license_no", "business", "address", "tel", "fax", "email",
                "association", "fair_trade", "staff", "holiday", "takkenshi_reg",
                "brand_color", "display_font", "logo_path", "tagline")
BRAND_KEYS = PROFILE_KEYS          # 旧名は互換のため残す
FONTS = ("gothic", "condensed", "rounded")
_BRAND_LOCK = threading.RLock()
_BRAND_LOCAL = threading.local()


class BrandError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def history_path(data_dir) -> Path:
    """履歴は**正本 company.json と同じディレクトリ**に置く。

    別々の場所に分けると、バックアップ・復元・監査が「正本は戻したが履歴は古いまま」
    のような半端な状態を作る（2026-08-06 Codex レビュー指摘・実パスで確認して是正）。
    """
    from hub_core.auth import company_path
    return company_path(data_dir).parent / "brand_history.jsonl"


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


@contextlib.contextmanager
def _brand_transaction(data_dir):
    """Serialize the company profile and its version history as one commit."""
    auth_dir = history_path(data_dir).parent
    auth_dir.mkdir(parents=True, exist_ok=True)
    key = str(auth_dir.resolve())
    with _BRAND_LOCK:
        depths = getattr(_BRAND_LOCAL, "depths", {})
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        _BRAND_LOCAL.depths = depths
        handle = None
        try:
            if depth == 0:
                handle = (auth_dir / ".brand.lock").open("a+")
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if depth == 0 and handle is not None:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            if depth:
                depths[key] = depth
            else:
                depths.pop(key, None)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Persist the directory entry as well as the file contents.  APFS normally
        # makes rename durable quickly, but release-critical profile state should
        # not depend on that timing after a power loss.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, previous)


def _history_bytes(rows: list[dict]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def normalize(brand: dict) -> dict:
    """入力を検証して正規化する。壊れた値は既定へ落とす（画面を壊さない）。"""
    b = dict(brand or {})
    out = {}
    name = str(b.get("name") or b.get("company_name") or "").strip()
    if name:
        out["name"] = name
    lic = str(b.get("license_no") or b.get("license") or "").strip()
    if lic:
        out["license_no"] = lic
    color = str(b.get("brand_color") or "").strip()
    if color and not HEX_RE.match(color):
        raise BrandError(400, f"ブランド色は #RRGGBB の形式で指定してください（受領: {color!r}）。")
    if color:
        out["brand_color"] = color.lower()
    font = str(b.get("display_font") or "").strip()
    if font:
        if font not in FONTS:
            raise BrandError(400, f"見出しの書体は {list(FONTS)} のいずれかです（受領: {font!r}）。")
        out["display_font"] = font
    for k in PROFILE_KEYS:
        if k in out or k in ("name", "license_no", "brand_color", "display_font"):
            continue
        v = str(b.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def profile_hash(profile: dict) -> str:
    """会社プロファイルの内容ハッシュ。確定成果物に刻んで後日の変更と混ざらないようにする。"""
    import hashlib
    core = {k: str(profile.get(k) or "") for k in PROFILE_KEYS}
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def read_history(data_dir) -> list[dict]:
    p = history_path(data_dir)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 壊れた行は読み飛ばす（履歴表示のために全体を落とさない）
    return rows


def _next_version(data_dir) -> int:
    rows = read_history(data_dir)
    return 1 + max((int(r.get("version") or 0) for r in rows), default=0)


def _commit_profile(data_dir, merged: dict, actor: str, source: str) -> dict:
    """Atomically advance company.json and the bounded version history."""
    from hub_core.auth import company_path, save_company

    company_file = company_path(data_dir)
    history_file = history_path(data_dir)
    previous_company = company_file.read_bytes() if company_file.is_file() else None
    previous_history = history_file.read_bytes() if history_file.is_file() else None
    rows = read_history(data_dir)
    version = 1 + max((int(row.get("version") or 0) for row in rows), default=0)
    snapshot = {key: merged.get(key) for key in PROFILE_KEYS if merged.get(key)}
    rec = {
        "version": version,
        "saved_at": _now(),
        "saved_by": actor or "",
        "source": source,
        "profile_hash": profile_hash(merged),
        "brand": snapshot,
    }
    next_rows = (rows + [rec])[-MAX_HISTORY:]
    company_written = history_written = False
    try:
        save_company(data_dir, merged)
        company_written = True
        _atomic_write(history_file, _history_bytes(next_rows))
        history_written = True
    except Exception as exc:  # noqa: BLE001 both files must describe the same version
        rollback_errors = []
        for path, previous, was_written in (
            (history_file, previous_history, history_written),
            (company_file, previous_company, company_written),
        ):
            if not was_written:
                continue
            try:
                _restore_file(path, previous)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path.name}: {rollback_exc}")
        detail = (" ロールバックにも失敗しました: " + "; ".join(rollback_errors)) if rollback_errors else ""
        raise BrandError(500, "会社情報と変更履歴を同時に保存できませんでした。" + detail) from exc
    return rec


def save(data_dir, brand: dict, actor: str, *, source: str = "manual") -> dict:
    """ブランドを保存し、履歴に1版積む。company.json の他項目（連絡先等）は保持する。

    source は由来の記録（manual / logo / website:<provider> / restore:<version>）。
    どこから来た値かを残さないと、後で「これは自分で決めたのか自動提案か」が判らなくなる。
    """
    from hub_core.auth import load_company
    b = normalize(brand)
    if not b:
        raise BrandError(400, "保存する内容がありません。")
    with _brand_transaction(data_dir):
        current = load_company(data_dir, strict=True) or {}
        return _commit_profile(data_dir, {**current, **b}, actor, source)


def restore(data_dir, version: int, actor: str) -> dict:
    """過去の版を現在のブランドへ戻す。戻す操作もまた新しい版として積む（履歴を消さない）。"""
    from hub_core.auth import load_company
    with _brand_transaction(data_dir):
        rows = read_history(data_dir)
        if not rows:
            raise BrandError(404, "ブランドの履歴がまだありません。")
        target = next((r for r in rows if int(r.get("version") or 0) == int(version)), None)
        if target is None:
            avail = [r.get("version") for r in rows]
            raise BrandError(404, f"版 {version} は履歴にありません（ある版: {avail}）。")
        snap = normalize(target.get("brand") or {})
        # merge でなく**置換**する。merge だと当時の版に無かった項目が現在の値のまま残り、
        # 「過去の免許番号＋現在の住所」という実在しない業者表示になる。
        current = load_company(data_dir, strict=True) or {}
        replaced = {key: value for key, value in current.items() if key not in PROFILE_KEYS}
        replaced.update({key: value for key, value in snap.items() if value})
        return _commit_profile(data_dir, replaced, actor, f"restore:{version}")


# ----------------------------------------------------------------- 取り込み（無料）
def color_from_logo(png_path) -> str:
    """ロゴPNGから代表色を1色取り出す（無料・ローカル完結・外部送信なし）。

    最頻色ではなく「最も彩度の高い頻出色」を採る。ロゴは白地・黒文字の面積が大きく、
    最頻色を採ると白か黒になってブランド色にならないため。
    """
    p = Path(png_path)
    if not p.is_file():
        raise BrandError(404, f"ロゴ画像が見つかりません: {png_path}")
    try:
        from PIL import Image
    except ImportError:
        raise BrandError(501, "この端末では画像からの色抽出に対応していません。"
                              "ブランド色は画面から直接指定してください。")
    with Image.open(p) as im:
        im = im.convert("RGB").resize((64, 64))
        counts: dict[tuple[int, int, int], int] = {}
        for px in list(im.convert("RGB").tobytes()[i:i + 3]
                       for i in range(0, 64 * 64 * 3, 3)):
            q = (px[0] // 16 * 16, px[1] // 16 * 16, px[2] // 16 * 16)
            counts[q] = counts.get(q, 0) + 1
    best, best_score = None, -1.0
    for (r, g, b), n in counts.items():
        mx, mn = max(r, g, b), min(r, g, b)
        sat = 0.0 if mx == 0 else (mx - mn) / mx
        light = mx / 255
        if sat < 0.25 or light < 0.15 or light > 0.97:
            continue                      # 白・黒・灰はブランド色に採らない
        score = sat * (n ** 0.5)
        if score > best_score:
            best, best_score = (r, g, b), score
    if best is None:
        raise BrandError(422, "このロゴからは色を決められませんでした（白黒基調のようです）。"
                              "ブランド色は画面から直接指定してください。")
    return "#%02x%02x%02x" % best


# ----------------------------------------------------------------- 取り込み（有料）
class BrandSource:
    """Webサイト等からブランド一式を推測する外部プロバイダの口（有料層）。

    自前で実装しない。この種の推測は外部の専用モデルの方が明確に優秀で、
    APIキー・課金・外部送信は利用者の明示操作が必要だからである。
    実装を差し込むときは estimate() だけを満たせばよい。
    """

    name = "none"

    def estimate(self, *, website: str = "", logo_path: str = "") -> dict:
        raise BrandError(501, "自動でのブランド推測は未設定です。"
                              "無料の範囲では、ロゴ画像から色を取り込むか、画面から直接指定できます。")


def build_brand_source(data_dir) -> BrandSource:
    """設定済みの外部プロバイダを返す。未設定なら無料層（推測なし）の口を返す。

    有料プロバイダの設定（APIキー・課金）は人間ゲート。未設定を異常にせず、
    無料層で完結できる状態を既定にする。
    """
    return BrandSource()


def estimate_brand(data_dir, *, website: str = "", logo_path: str = "") -> dict:
    """ブランド推測の入口。有料プロバイダがあればそれを使い、無ければ無料の範囲で返す。

    無料の範囲＝ロゴ画像からの色抽出のみ（ローカル完結）。website だけの指定は
    外部取得が要るため、無料層では正直に 501 で断る（黙って空を返さない）。
    """
    src = build_brand_source(data_dir)
    if src.name != "none":
        return {**src.estimate(website=website, logo_path=logo_path), "_source": f"website:{src.name}"}
    if logo_path:
        return {"brand_color": color_from_logo(logo_path), "_source": "logo"}
    raise BrandError(501, "Webサイトからのブランド推測は、外部サービスの設定が要ります（有料）。"
                          "無料の範囲では、ロゴ画像を指定すれば色を取り込めます。")

def snapshot_dir(data_dir) -> Path:
    from hub_core.auth import company_path
    return company_path(data_dir).parent / "company_profiles"


def snapshot_profile(data_dir) -> str:
    """現在の会社プロファイルを内容ハッシュ名で**不変保存**し、そのハッシュを返す。

    確定済みの書類は「作った当時の会社情報」で読み直せなければならない。履歴 jsonl は
    MAX_HISTORY で古い版が落ちるので、確定成果物はそこを参照できない。ハッシュ名の
    実体ファイルを別に置き、書類の版メタからそれを指す（同じ内容なら再書き込みしない）。
    会社情報が未設定・破損なら空文字を返す（書類保存自体は止めない）。
    """
    from hub_core.auth import CompanyProfileError, load_company
    with _brand_transaction(data_dir):
        try:
            cur = load_company(data_dir, strict=True) or {}
        except CompanyProfileError:
            return ""
        if not any(cur.get(k) for k in PROFILE_KEYS):
            return ""
        h = profile_hash(cur)
        d = snapshot_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{h}.json"
        valid_existing = False
        if f.is_file():
            try:
                existing = json.loads(f.read_text(encoding="utf-8"))
                valid_existing = (existing.get("profile_hash") == h
                                  and profile_hash(existing.get("profile") or {}) == h)
            except (OSError, ValueError, json.JSONDecodeError):
                valid_existing = False
        if not valid_existing:
            snap = {k: cur.get(k) for k in PROFILE_KEYS if cur.get(k)}
            payload = json.dumps(
                {"profile_hash": h, "saved_at": _now(), "profile": snap},
                ensure_ascii=False, indent=2,
            ).encode("utf-8")
            _atomic_write(f, payload)
        return h


def load_snapshot(data_dir, profile_hash_value: str) -> dict:
    """確定当時の会社プロファイルを読む。無ければ {}（捏造しない）。"""
    if not profile_hash_value:
        return {}
    f = snapshot_dir(data_dir) / f"{profile_hash_value}.json"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("profile") or {}
    except (OSError, ValueError):
        return {}

def brand_of_company(company: dict | None) -> tuple[str, str]:
    """(社名, ブランド色) を返す。色が未設定なら**製品色でなく中立の graphite** に落とす。

    お客様に届く面で会社色が無いとき、製品の cobalt を出すと「あいのての色」で
    その会社の顧客に届いてしまう（境界違反）。中立色にして製品を出さない。
    """
    from hub_core import flex_card as _fc
    name, color = _fc.brand_of(company)
    if not (company or {}).get("brand_color"):
        color = "#2A2E37"
    return name, color
