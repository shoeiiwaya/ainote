"""hub_core/backup.py — 暗号化バックアップ＋鍵別保管 (Stage0 S0-7 / §4.1・§4.2)。

要件(ONLINE_ARCHITECTURE.md §4.2):
- バックアップは暗号化必須(平文PIIをディスクに残さない)。
- 鍵は §4.1 によりバックアップと別保管(別ディレクトリ・OS権限分離 chmod600・同一バックアップに含めない)。
- バックアップ/エクスポート操作は audit に action='export' で記録。

既存DB単体形式は後方互換のため、stdlibによる次の旧形式を読書きする:
  - 鍵導出: HMAC-SHA256(master, "enc"/"mac") で enc/mac サブ鍵を分離。
  - 機密性: HMAC-SHA256(enc_key, nonce||counter) のキーストリームを XOR(CTRモード)。
  - 完全性: Encrypt-then-MAC = HMAC-SHA256(mac_key, header||nonce||ct) を tag に付与・復号時に
    定数時間比較(改ざんは復号前に検知)。
一般配布用portable backupはこの旧形式を使用せず、cryptographyのAES-256-GCMだけを使う。
依存が無い環境では弱い方式へfallbackせず、書き出し・復元ともfail-closedにする。
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import stat
import struct
import tempfile
import threading
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

JST = timezone(timedelta(hours=9))
_LOCK = threading.Lock()
_SNAPSHOT_LOCKS: dict[str, threading.RLock] = {}
_SNAPSHOT_LOCKS_GUARD = threading.Lock()

MAGIC = b"RIH1"          # 4 bytes: フォーマット識別
_VER = 1
_NONCE_LEN = 16
_TAG_LEN = 32
_BLOCK = 32             # HMAC-SHA256 出力長
_HEADER = MAGIC + bytes([_VER])

PORTABLE_FORMAT = "ainote-portable-backup"
PORTABLE_VERSION = 2
PORTABLE_MANIFEST = "manifest.json"
RECOVERY_KEY_HEADER = b"AINOTE-RECOVERY-KEY-V1\n"
PORTABLE_MAGIC = b"AIB2"
_PORTABLE_NONCE_LEN = 12
_PORTABLE_HEADER = PORTABLE_MAGIC + bytes([PORTABLE_VERSION])
_PORTABLE_AAD = _PORTABLE_HEADER + b"\0" + PORTABLE_FORMAT.encode("ascii")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_TRANSIENT_SUFFIXES = (".db-wal", ".db-shm", ".lock", ".tmp", ".temp")
_TRANSIENT_DIRS = {"backups", "__pycache__", ".git"}
_MAX_MANIFEST_BYTES = 16 * 1024 ** 2
_MAX_FILE_COUNT = 100_000
_MAX_FILE_BYTES = 1024 ** 3
_MAX_EXPANDED_BYTES = 2 * 1024 ** 3


class BackupError(Exception):
    """バックアップ/復元の失敗(改ざん検知・鍵不一致・鍵とバックアップの同居違反など)。"""


class PortableCryptoUnavailable(BackupError):
    """標準AEAD実装が無いためportable backupを安全に処理できない。"""


@contextlib.contextmanager
def portable_snapshot_lock(data_dir):
    """同一プロセスの画面要求とportable snapshotを同じ順序で直列化する。"""
    key = str(Path(data_dir).resolve())
    with _SNAPSHOT_LOCKS_GUARD:
        lock = _SNAPSHOT_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


# --- 鍵管理(§4.1: DBと別ディレクトリ・chmod600) ---------------------------------
def default_key_path() -> Path:
    """既定の鍵置き場。out/ やバックアップとは別ディレクトリ・OS権限分離。"""
    explicit = os.environ.get("RI_HUB_KEY_PATH")
    if explicit:
        return Path(explicit)
    key_dir = Path(os.environ.get("RI_HUB_KEYS_DIR", str(Path.home() / ".ri-hub" / "keys")))
    return key_dir / "backup.key"


def load_or_create_key(key_path) -> bytes:
    """32byte マスター鍵を読み込む。無ければ os.urandom で生成し chmod600 で保存(ディレクトリは0700)。"""
    key_path = Path(key_path)
    if key_path.is_symlink():
        raise BackupError(f"鍵にシンボリックリンクは使えません: {key_path}")
    if key_path.exists():
        mode = key_path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise BackupError(f"鍵が通常ファイルではありません: {key_path}")
        if mode & 0o077:
            raise BackupError(f"鍵の権限が広すぎます(0600必須): {key_path}")
        raw = key_path.read_bytes()
        if len(raw) != 32:
            raise BackupError(f"鍵長が不正です(32byte必須): {key_path} = {len(raw)}byte")
        return raw
    key = os.urandom(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_path.parent, 0o700)
    except OSError:
        pass
    # 0600 で原子的に作成(他ユーザーから読めない)
    try:
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # バックアップ本体と復旧キーを同時取得した時の初回生成競合。
        return load_or_create_key(key_path)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


# --- 認証付き暗号(stdlib HMAC ベース AEAD) --------------------------------------
def _subkeys(master: bytes):
    enc = hmac.new(master, b"enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: bytes, master: bytes) -> bytes:
    """plaintext を AEAD で暗号化。出力 = MAGIC|ver|nonce|tag|ciphertext。"""
    enc_key, mac_key = _subkeys(master)
    nonce = os.urandom(_NONCE_LEN)
    ks = _keystream(enc_key, nonce, len(plaintext))
    ct = bytes(p ^ k for p, k in zip(plaintext, ks))
    tag = hmac.new(mac_key, _HEADER + nonce + ct, hashlib.sha256).digest()
    return _HEADER + nonce + tag + ct


def decrypt(blob: bytes, master: bytes) -> bytes:
    """AEAD blob を復号。改ざん(tag不一致)は復号前に BackupError。"""
    if len(blob) < len(_HEADER) + _NONCE_LEN + _TAG_LEN:
        raise BackupError("バックアップが短すぎます(破損)")
    if blob[:len(_HEADER)] != _HEADER:
        raise BackupError("フォーマット識別子が不正です(別形式 or 破損)")
    off = len(_HEADER)
    nonce = blob[off:off + _NONCE_LEN]; off += _NONCE_LEN
    tag = blob[off:off + _TAG_LEN]; off += _TAG_LEN
    ct = blob[off:]
    enc_key, mac_key = _subkeys(master)
    expected = hmac.new(mac_key, _HEADER + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise BackupError("認証タグ不一致: バックアップが改ざん or 鍵不一致(復号拒否)")
    ks = _keystream(enc_key, nonce, len(ct))
    return bytes(c ^ k for c, k in zip(ct, ks))


def _aesgcm_type():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        raise PortableCryptoUnavailable(
            "暗号化バックアップには cryptography（AES-256-GCM）が必要です") from exc
    return AESGCM


def portable_crypto_available() -> bool:
    """標準AEADを実際にimportできる時だけportable backupを有効にする。"""
    try:
        _aesgcm_type()
        return True
    except PortableCryptoUnavailable:
        return False


def _encrypt_portable(plaintext: bytes, master: bytes) -> bytes:
    """portable v2: AES-256-GCM、96bit nonce、format/versionをAADへ束縛。"""
    if len(master) != 32:
        raise BackupError("復旧キーの長さが不正です")
    nonce = os.urandom(_PORTABLE_NONCE_LEN)
    ciphertext_and_tag = _aesgcm_type()(master).encrypt(nonce, plaintext, _PORTABLE_AAD)
    return _PORTABLE_HEADER + nonce + ciphertext_and_tag


def _decrypt_portable(blob: bytes, master: bytes) -> bytes:
    """portable v2を認証後に復号。旧独自形式をportableとして受け入れない。"""
    minimum = len(_PORTABLE_HEADER) + _PORTABLE_NONCE_LEN + 16
    if len(blob) < minimum:
        raise BackupError("暗号化バックアップが短すぎます")
    if blob[:len(_PORTABLE_HEADER)] != _PORTABLE_HEADER:
        if blob.startswith(MAGIC):
            raise BackupError("旧暗号形式は一般配布バックアップとして復元できません")
        raise BackupError("暗号化バックアップの形式または版が不正です")
    if len(master) != 32:
        raise BackupError("復旧キーの長さが不正です")
    off = len(_PORTABLE_HEADER)
    nonce = blob[off:off + _PORTABLE_NONCE_LEN]
    ciphertext_and_tag = blob[off + _PORTABLE_NONCE_LEN:]
    aesgcm = _aesgcm_type()
    try:
        return aesgcm(master).decrypt(
            nonce, ciphertext_and_tag, _PORTABLE_AAD)
    except Exception as exc:  # InvalidTagの型をHTTP/CLIへ露出しない
        raise BackupError("認証に失敗しました。改ざん、破損、または復旧キー不一致です") from exc


# --- バックアップ/復元 ------------------------------------------------------------
def _audit_export(audit_dir, actor: str, target: str, detail: dict, *, required: bool = False):
    """エクスポート/バックアップ操作を view_audit.jsonl に action='export' で追記(§4.2)。"""
    try:
        from hub_core.viewlog import record_view
        record_view(audit_dir, actor, "system", f"{target} {json.dumps(detail, ensure_ascii=False)}",
                    action="export")
    except Exception as exc:
        if required:
            raise BackupError("書き出し監査を記録できないため、バックアップを中止しました") from exc
        # 旧DB単体バックアップは従来互換のbest-effort。一般配布用の全体書き出しはrequired。


def make_backup(db_path, backup_dir, key_path=None, actor: str = "system", audit_dir=None) -> Path:
    """db_path(SQLite正本)を暗号化して backup_dir に保存。鍵は別保管。戻り値=暗号化ファイルパス。

    §4.1: 鍵とバックアップを同一ディレクトリ/同一バックアップに含めない(同時流出防止)を物理強制。
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    key_path = Path(key_path) if key_path else default_key_path()
    if not db_path.exists():
        raise BackupError(f"バックアップ対象DBが存在しません: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    # 鍵がバックアップ配下にあると同時流出する → 禁止(§4.1)
    kp, bd = key_path.resolve(), backup_dir.resolve()
    if str(kp).startswith(str(bd) + os.sep) or kp.parent == bd:
        raise BackupError(
            f"鍵がバックアップディレクトリ内にあります(同時流出のため禁止・§4.1): key={kp} backup_dir={bd}")

    master = load_or_create_key(key_path)
    plaintext = db_path.read_bytes()
    blob = encrypt(plaintext, master)

    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    enc_path = backup_dir / f"{db_path.stem}-backup-{stamp}.db.enc"
    with _LOCK:
        # 0600 で書き込み(平文PIIは残さない=暗号文のみ)
        fd = os.open(str(enc_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)

    _audit_export(audit_dir or db_path.parent, actor, "backup",
                  {"src": db_path.name, "dst": enc_path.name, "bytes": len(blob)})
    return enc_path


def restore_backup(enc_path, dest_path, key_path=None) -> Path:
    """暗号化バックアップを復号して dest_path に書き出す(復元訓練用)。鍵不一致/改ざんは BackupError。"""
    enc_path, dest_path = Path(enc_path), Path(dest_path)
    key_path = Path(key_path) if key_path else default_key_path()
    if not key_path.exists():
        raise BackupError(f"復号鍵がありません: {key_path}")
    master = load_or_create_key(key_path)
    plaintext = decrypt(enc_path.read_bytes(), master)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(plaintext)
    return dest_path


# --- ポータブル全体バックアップ ---------------------------------------------------
def recovery_key_document(master: bytes) -> bytes:
    """32 byte の復旧鍵を、誤認しにくい単独配布用テキストへ変換する。"""
    if len(master) != 32:
        raise BackupError("復旧キーの長さが不正です")
    return RECOVERY_KEY_HEADER + base64.urlsafe_b64encode(master) + b"\n"


def parse_recovery_key_document(document: bytes | str) -> bytes:
    """単独配布した復旧キーテキストを厳格に読み戻す。"""
    raw = document.encode("ascii") if isinstance(document, str) else bytes(document)
    lines = raw.strip().splitlines()
    if len(lines) != 2 or lines[0] + b"\n" != RECOVERY_KEY_HEADER:
        raise BackupError("復旧キーの形式が不正です")
    try:
        key = base64.b64decode(lines[1], altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BackupError("復旧キーの形式が不正です") from exc
    if len(key) != 32:
        raise BackupError("復旧キーの長さが不正です")
    return key


def export_recovery_key(key_path=None) -> bytes:
    """責任者がバックアップとは別ファイルとして保管する復旧キーを返す。"""
    path = Path(key_path) if key_path else default_key_path()
    return recovery_key_document(load_or_create_key(path))


def _is_transient(rel: Path) -> bool:
    """処理途中・再帰バックアップを通常ファイル走査から除く。

    hub.db はここでは除外し、WALを含むSQLite online snapshotとして別途収載する。
    """
    parts = rel.parts
    name = rel.name
    lower = name.lower()
    if any(part in _TRANSIENT_DIRS for part in parts):
        return True
    if name.startswith(".hub.operation.") or name.startswith("~$"):
        return True
    if lower == "hub.db" or lower.endswith(_TRANSIENT_SUFFIXES):
        return True
    if lower.endswith(".db") or lower.endswith(".key"):
        return True
    return False


def _read_regular_file(source: Path) -> bytes:
    """symlinkへのすり替えを最終open時にも拒否して通常ファイルだけを読む。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(source), flags)
    except OSError as exc:
        raise BackupError(f"バックアップ対象を安全に開けません: {source}") from exc
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise BackupError(f"バックアップ対象が通常ファイルではありません: {source}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _sqlite_quick_check_path(path: Path) -> None:
    """隔離されたSQLiteファイルをread-onlyで検査し、壊れた断面を拒否する。"""
    path = Path(path)
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            rows = [str(row[0]) for row in con.execute("PRAGMA quick_check").fetchall()]
        finally:
            con.close()
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("バックアップ内のSQLite業務台帳を検査できません") from exc
    if rows != ["ok"]:
        raise BackupError("バックアップ内のSQLite業務台帳が壊れています")


def _sqlite_quick_check_bytes(payload: bytes) -> None:
    """復号したSQLite bytesを隔離ファイルへ置き、commit前に完全性を検査する。"""
    fd, raw_path = tempfile.mkstemp(prefix="ainote-sqlite-check-", suffix=".db")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _sqlite_quick_check_path(path)
    finally:
        path.unlink(missing_ok=True)


def _sqlite_snapshot(db_path: Path) -> bytes:
    """sqlite3 backup APIでWALを含む一貫した自己完結スナップショットを返す。"""
    db_path = Path(db_path)
    if db_path.is_symlink():
        raise BackupError(f"SQLite業務台帳にシンボリックリンクは使えません: {db_path}")
    if not db_path.is_file():
        raise BackupError(f"SQLite業務台帳が見つかりません: {db_path}")
    fd, raw_path = tempfile.mkstemp(prefix="ainote-sqlite-snapshot-", suffix=".db")
    os.close(fd)
    snapshot = Path(raw_path)
    source = target = None
    try:
        source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
        target = sqlite3.connect(str(snapshot), timeout=30.0)
        source.backup(target)
        target.commit()
        target.close()
        target = None
        source.close()
        source = None
        payload = _read_regular_file(snapshot)
        _sqlite_quick_check_bytes(payload)
        return payload
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("SQLite業務台帳の一貫スナップショットを作成できません") from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        snapshot.unlink(missing_ok=True)


def _portable_sources(data_dir: Path, recovery_key_path: Path) -> dict[str, bytes]:
    """現在の正本を、ポータブルアーカイブ内の論理パスへ写す。"""
    from hub_core.audit import _load_chain_key, default_chain_key_path
    from hub_core.auth import company_path, users_path
    from hub_core.branding import history_path, snapshot_dir

    data_dir = data_dir.resolve()
    recovery_key_path = recovery_key_path.resolve()
    audit_key_path = default_chain_key_path().resolve()
    if recovery_key_path == audit_key_path:
        raise BackupError("バックアップ復旧キーと監査鍵は同じファイルにできません")

    audit_key = _load_chain_key(audit_key_path)
    recovery_key = load_or_create_key(recovery_key_path)
    if hmac.compare_digest(audit_key, recovery_key):
        raise BackupError("バックアップ復旧キーと監査鍵は別の鍵が必要です")

    entries: dict[str, bytes] = {}
    for source in sorted(data_dir.rglob("*")):
        if source.is_symlink():
            raise BackupError(f"バックアップ対象にシンボリックリンクがあります: {source}")
        if not source.is_file():
            continue
        rel = source.relative_to(data_dir)
        if _is_transient(rel):
            continue
        entries[f"data/{rel.as_posix()}"] = _read_regular_file(source)

    # UI/MCP共通の業務操作はSQLiteへ直接記録される。hub.dbを単なる派生物として
    # 落とすと、案件・進捗・請求などCSVに未反映の確定状態が復元後に消える。
    db_path = data_dir / "hub.db"
    if db_path.exists() or db_path.is_symlink():
        entries["data/hub.db"] = _sqlite_snapshot(db_path)

    # 暗号化の内側なので、会社正本・履歴・利用者ハッシュも復元可能な状態で保持する。
    for source in (company_path(data_dir), history_path(data_dir), users_path(data_dir)):
        source = Path(source)
        if source.is_symlink():
            raise BackupError(f"認証正本にシンボリックリンクは使えません: {source}")
        if source.is_file():
            entries[f"auth/{source.name}"] = _read_regular_file(source)

    # 確定書類が参照する「作成当時の会社情報」も正本。履歴JSONだけでは復元できない。
    profiles = snapshot_dir(data_dir)
    if profiles.is_symlink():
        raise BackupError(f"会社プロファイル保存先にシンボリックリンクは使えません: {profiles}")
    if profiles.is_dir():
        for source in sorted(profiles.rglob("*")):
            if source.is_symlink():
                raise BackupError(f"会社プロファイルにシンボリックリンクがあります: {source}")
            if source.is_file():
                rel = source.relative_to(profiles)
                entries[f"auth/company_profiles/{rel.as_posix()}"] = _read_regular_file(source)

    # 別PCで監査ログを検証するために必要。ただし復旧キーそのものは絶対に含めない。
    entries["keys/audit_chain.key"] = audit_key
    return entries


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def _portable_zip(entries: dict[str, bytes]) -> bytes:
    files = [
        {"path": name, "bytes": len(entries[name]), "sha256": hashlib.sha256(entries[name]).hexdigest()}
        for name in sorted(entries)
    ]
    manifest_data = {"format": PORTABLE_FORMAT, "version": PORTABLE_VERSION, "files": files}
    if "data/hub.db" in entries:
        manifest_data["database"] = {
            "path": "data/hub.db",
            "engine": "sqlite3",
            "snapshot": "sqlite3_backup",
            "integrity": "quick_check",
        }
    manifest = json.dumps(
        manifest_data,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(_zip_info(PORTABLE_MANIFEST), manifest)
        for name in sorted(entries):
            zf.writestr(_zip_info(name), entries[name])
    return out.getvalue()


def make_portable_backup(data_dir, key_path=None, actor: str = "system") -> bytes:
    """data_dir と復元に必要なローカル正本を、単一の認証付き暗号文にする。

    復旧キーは返り値にもZIP内部にも含めず、 ``export_recovery_key`` で別取得する。
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise BackupError("バックアップ対象が見つかりません")
    _aesgcm_type()  # 監査追記や鍵生成より前に、標準AEAD不在をfail-closedにする。
    recovery_key_path = Path(key_path) if key_path else default_key_path()
    from hub_core.operations import _operation_lock
    from hub_core.viewlog import _LOCK as viewlog_lock
    with portable_snapshot_lock(data_dir), _operation_lock(data_dir):
        # この操作の監査記録も、直後に取るスナップショットへ含める。
        _audit_export(data_dir, actor, "portable_backup", {"format": PORTABLE_FORMAT}, required=True)
        # 通常GETは並行のまま、短いviewlog追記だけをsnapshot中は待たせる。
        with viewlog_lock:
            entries = _portable_sources(data_dir, recovery_key_path)
            # 外部プロセス等がロック契約を守らず書き換えた場合も、世代混在を黙って出さない。
            if entries != _portable_sources(data_dir, recovery_key_path):
                raise BackupError("バックアップ中にデータが変更されました。操作を止めて再実行してください")
        plaintext_zip = _portable_zip(entries)
        return _encrypt_portable(plaintext_zip, load_or_create_key(recovery_key_path))


def _valid_archive_path(name: str) -> bool:
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return (len(name.encode("utf-8")) <= 1024 and len(path.parts) <= 100
            and len(path.parts) >= 2 and path.as_posix() == name
            and all(len(part.encode("utf-8")) <= 255 for part in path.parts)
            and not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)
            and path.parts[0] in ("data", "auth", "keys"))


def _archive_path_key(name: str) -> tuple[str, ...]:
    """macOSの大小文字・Unicode正規化を越えて同じ復元先になる名前を揃える。"""
    return tuple(unicodedata.normalize("NFC", part).casefold()
                 for part in PurePosixPath(name).parts)


def read_portable_backup(blob: bytes, recovery_key: bytes) -> dict[str, bytes]:
    """復号後のZIPをマニフェストまで検証し、安全な論理パス→bytesで返す。"""
    plaintext = _decrypt_portable(blob, recovery_key)
    try:
        with zipfile.ZipFile(io.BytesIO(plaintext), "r") as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            if (len(names) > _MAX_FILE_COUNT + 1 or len(names) != len(set(names))
                    or PORTABLE_MANIFEST not in names):
                raise BackupError("バックアップ内のファイル一覧が不正です")
            if any(info.is_dir() for info in infos):
                raise BackupError("バックアップ内に不正なディレクトリ項目があります")
            manifest_info = next(info for info in infos if info.filename == PORTABLE_MANIFEST)
            if (manifest_info.file_size > _MAX_MANIFEST_BYTES
                    or manifest_info.compress_type != zipfile.ZIP_DEFLATED):
                raise BackupError("バックアップのマニフェストサイズまたは圧縮形式が不正です")
            try:
                manifest = json.loads(zf.read(PORTABLE_MANIFEST).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise BackupError("バックアップのマニフェストが壊れています") from exc
            if (not isinstance(manifest, dict) or manifest.get("format") != PORTABLE_FORMAT
                    or manifest.get("version") != PORTABLE_VERSION
                    or not isinstance(manifest.get("files"), list)):
                raise BackupError("バックアップの形式または版が不正です")

            database = manifest.get("database")
            if database is not None and database != {
                    "path": "data/hub.db", "engine": "sqlite3",
                    "snapshot": "sqlite3_backup", "integrity": "quick_check"}:
                raise BackupError("バックアップのSQLite台帳情報が不正です")

            expected: dict[str, tuple[int, str]] = {}
            normalized_paths: set[tuple[str, ...]] = set()
            for rec in manifest["files"]:
                if not isinstance(rec, dict):
                    raise BackupError("バックアップのマニフェストが不正です")
                name = rec.get("path")
                size = rec.get("bytes")
                digest = rec.get("sha256")
                if (not isinstance(name, str) or not _valid_archive_path(name)
                        or not isinstance(size, int) or size < 0
                        or not isinstance(digest, str) or len(digest) != 64
                        or any(ch not in "0123456789abcdef" for ch in digest)
                        or name in expected):
                    raise BackupError("バックアップのマニフェストが不正です")
                normalized = _archive_path_key(name)
                if normalized in normalized_paths:
                    raise BackupError("バックアップ内に同じ復元先となる名前が重複しています")
                normalized_paths.add(normalized)
                expected[name] = (size, digest)
            for normalized in normalized_paths:
                if any(normalized[:i] in normalized_paths for i in range(1, len(normalized))):
                    raise BackupError("バックアップ内でファイルとディレクトリの名前が衝突しています")
            if set(names) != {PORTABLE_MANIFEST, *expected.keys()}:
                raise BackupError("バックアップ内のファイル一覧がマニフェストと一致しません")
            if ("data/hub.db" in expected) != (database is not None):
                raise BackupError("バックアップのSQLite台帳情報とファイル一覧が一致しません")
            key_names = {name for name in expected if name.startswith("keys/")}
            if key_names != {"keys/audit_chain.key"} or expected["keys/audit_chain.key"][0] != 32:
                raise BackupError("バックアップ内の監査鍵が不足または不正です")
            # 復号後に小さなZIPから巨大データを展開させるzip bombを拒否する。
            total_size = sum(size for size, _digest in expected.values())
            if (total_size > min(_MAX_EXPANDED_BYTES,
                                 len(plaintext) * 200 + 64 * 1024 ** 2)
                    or any(size > _MAX_FILE_BYTES for size, _digest in expected.values())):
                raise BackupError("バックアップの展開サイズが不正です")
            zip_infos = {info.filename: info for info in infos}
            if any(zip_infos.get(name) is None or zip_infos[name].file_size != size
                   or zip_infos[name].compress_type != zipfile.ZIP_DEFLATED
                   for name, (size, _digest) in expected.items()):
                raise BackupError("バックアップの展開サイズがマニフェストと一致しません")

            contents: dict[str, bytes] = {}
            for name, (size, digest) in expected.items():
                raw = zf.read(name)
                if len(raw) != size or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
                    raise BackupError("バックアップ内のファイルが破損しています")
                contents[name] = raw
            if "data/hub.db" in contents:
                _sqlite_quick_check_bytes(contents["data/hub.db"])
            return contents
    except zipfile.BadZipFile as exc:
        raise BackupError("復号後のバックアップがZIP形式ではありません") from exc


def _atomic_restore(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    tmp = target.with_name(f".{target.name}.ainote-restore-{secrets.token_hex(6)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def restore_portable_backup(blob: bytes, dest_data_dir, *, key_path=None,
                            recovery_key: bytes | None = None, audit_key_path=None) -> dict:
    """検証済みの全体バックアップを未使用の出力先へ復元する。

    既存データへの上書き・mergeは、世代の混在を作るため意図的に許可しない。
    """
    if (key_path is None) == (recovery_key is None):
        raise BackupError("復元には復旧キーファイルまたは復旧キーのどちらか一方が必要です")
    if key_path is not None:
        key_file = Path(key_path)
        if not key_file.is_file():
            raise BackupError("復旧キーがありません")
        recovery_key = parse_recovery_key_document(key_file.read_bytes())
    assert recovery_key is not None
    contents = read_portable_backup(blob, recovery_key)

    from hub_core.audit import default_chain_key_path
    dest_data_dir = Path(dest_data_dir)
    auth_target = dest_data_dir.parent / "auth"
    audit_target = Path(audit_key_path) if audit_key_path else default_chain_key_path()
    if key_path is not None and Path(key_path).resolve() == audit_target.resolve():
        raise BackupError("復旧キーファイルと監査鍵の復元先は分けてください")
    invalid_keys = {name for name in contents if name.startswith("keys/")} - {"keys/audit_chain.key"}
    if invalid_keys or len(contents.get("keys/audit_chain.key", b"")) != 32:
        raise BackupError("バックアップ内の監査鍵が不足または不正です")
    data_resolved = dest_data_dir.resolve()
    auth_resolved = auth_target.resolve()
    audit_resolved = audit_target.resolve()
    def overlaps(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    if (overlaps(data_resolved, auth_resolved) or overlaps(data_resolved, audit_resolved)
            or overlaps(auth_resolved, audit_resolved)):
        raise BackupError("業務データ・認証情報・監査鍵の復元先は重ねられません")
    if dest_data_dir.exists() or auth_target.exists() or audit_target.exists():
        raise BackupError("復元先が既に存在します。空の新しい出力先を指定してください")
    restored = {"data": 0, "auth": 0, "keys": 0}
    dest_data_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(
        prefix=".ainote-portable-restore-", dir=dest_data_dir.parent))
    stage_data = stage_root / "out"
    stage_auth = stage_root / "auth"
    audit_target.parent.mkdir(parents=True, exist_ok=True)
    stage_audit = audit_target.parent / f".{audit_target.name}.ainote-stage-{secrets.token_hex(6)}"
    committed: list[Path] = []
    completed = False
    try:
        # まず隔離領域へ全ファイルを書き、1件でも失敗したら公開先には触れない。
        for name in sorted(contents):
            path = PurePosixPath(name)
            rel = Path(*path.parts[1:])
            if path.parts[0] == "data":
                target = stage_data / rel
                restored["data"] += 1
            elif path.parts[0] == "auth":
                target = stage_auth / rel
                restored["auth"] += 1
            elif name == "keys/audit_chain.key":
                target = stage_audit
                restored["keys"] += 1
            else:
                raise BackupError("バックアップ内に復元できない鍵項目があります")
            _atomic_restore(target, contents[name])

        # 復号時のbytes検査に加え、実際にcommitする隔離ファイルも再検査する。
        restored_db = stage_data / "hub.db"
        if restored_db.is_file():
            _sqlite_quick_check_path(restored_db)

        # 新規出力先だけをcommitする。監査鍵はhard-linkで既存ファイルを上書きしない。
        if stage_data.exists():
            if dest_data_dir.exists():
                raise BackupError("復元中に業務データの出力先が作られました")
            os.rename(stage_data, dest_data_dir)
            committed.append(dest_data_dir)
        if stage_auth.exists():
            if auth_target.exists():
                raise BackupError("復元中に認証情報の出力先が作られました")
            os.rename(stage_auth, auth_target)
            committed.append(auth_target)
        if audit_target.exists():
            raise BackupError("復元中に監査鍵の出力先が作られました")
        os.link(stage_audit, audit_target)
        committed.append(audit_target)
        stage_audit.unlink()
        completed = True
        return restored
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("復元先への書き込みを完了できませんでした") from exc
    finally:
        # 例外時は、今回commitした新規出力だけを逆順で戻す。既存物はpreflightで拒否済み。
        if not completed:
            for target in reversed(committed):
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError:
                    pass
        shutil.rmtree(stage_root, ignore_errors=True)
        stage_audit.unlink(missing_ok=True)


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="ri-hub 暗号化バックアップ(S0-7・stdlib AEAD・鍵別保管)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="hub.db を暗号化してバックアップ")
    b.add_argument("db", help="SQLite正本 (例: out/hub.db)")
    b.add_argument("--backup-dir", default=None, help="暗号化バックアップ保存先(既定: <db>の親/backups)")
    b.add_argument("--key", default=None, help="鍵パス(既定: ~/.ri-hub/keys/backup.key)")
    b.add_argument("--actor", default="system")
    r = sub.add_parser("restore", help="暗号化バックアップを復号(復元訓練)")
    r.add_argument("enc", help="*.db.enc")
    r.add_argument("dest", help="復元先")
    r.add_argument("--key", default=None)
    pr = sub.add_parser("portable-restore", help="一般配布用 .enc を空の出力先へ復元")
    pr.add_argument("enc", help="画面から保存した ainote-backup.enc")
    pr.add_argument("--dest-data-dir", required=True,
                    help="業務データの新規復元先（例: ~/ainote-restored/out）")
    pr.add_argument("--recovery-key", required=True,
                    help="別保管した ainote-recovery-key.txt")
    pr.add_argument("--audit-key-dest", required=True,
                    help="監査鍵の新規復元先（業務データ・復旧キーと別の場所）")
    args = ap.parse_args(argv)

    if args.cmd == "backup":
        db = Path(args.db)
        bdir = Path(args.backup_dir) if args.backup_dir else db.parent / "backups"
        out = make_backup(db, bdir, args.key, args.actor, audit_dir=db.parent)
        print(f"✓ 暗号化バックアップ: {out}")
        print(f"  鍵(別保管): {Path(args.key) if args.key else default_key_path()}")
        return 0
    if args.cmd == "restore":
        out = restore_backup(args.enc, args.dest, args.key)
        print(f"✓ 復元: {out}")
        return 0
    if args.cmd == "portable-restore":
        enc_path = Path(args.enc)
        if not enc_path.is_file():
            raise BackupError("暗号化バックアップがありません")
        restored = restore_portable_backup(
            enc_path.read_bytes(), args.dest_data_dir,
            key_path=args.recovery_key, audit_key_path=args.audit_key_dest)
        print(f"復元完了: data={restored['data']} auth={restored['auth']} keys={restored['keys']}")
        print(f"業務データ: {Path(args.dest_data_dir)}")
        print(f"監査鍵: {Path(args.audit_key_dest)}")
        return 0
    return 2


if __name__ == "__main__":
    import sys
    try:
        sys.exit(_main())
    except BackupError as exc:
        print(f"復元失敗: {exc}", file=sys.stderr)
        sys.exit(1)
