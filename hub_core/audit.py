"""hub_core/audit.py — 追記専用(append-only)監査台帳 (Stage0 S0-6 / A-1是正)。

旧 write_audit_log_chained は毎回 "w" でチェーンを 0*64 から再生成していた(A-1)。
過去行を改ざんしても次回 ingest で正当チェーンに作り直され、改ざん検知にならなかった。

本モジュールの不変則:
- 既存 ledger を読み込み・チェーン検証してから「新規イベントのみ」を末尾追記する(過去行は不変)。
- 改ざんされた ledger は heal しない。AuditChainError で fail-closed(拡張を拒否し保全を促す)。
- 各エントリに dedup_key(timestamp 等の揮発フィールドを除いた raw イベントの sha256)を埋め込み、
  同日再実行での重複追記を防ぐ(=再生成でなく真の追記)。dedup_key も本文ハッシュに含め改ざん保護する。

stdlib のみ(第三者依存なし)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import contextlib
import tempfile
import threading

try:
    import fcntl
except ImportError:      # Windows には fcntl が無い（プロセス内ロックのみで動く）
    fcntl = None

from pathlib import Path

import hmac
import os

JST = timezone(timedelta(hours=9))
GENESIS = "0" * 64

# --- 改ざん検知の脅威モデル(正直化・敵対検証2026-06-15反映) -----------------------
# 旧実装は素のSHA-256連鎖だった。コードを読める攻撃者(=このrepoにアクセスできる者)は、
# ledgerを改ざんして「全行のハッシュを連鎖再計算」すれば verify をクリーン通過でき、改ざん検知に
# ならなかった(敵対レンズが実証)。これを是正し、エントリハッシュを **out/ 外に別保管した秘密鍵による
# HMAC-SHA256** にする。鍵を持たない攻撃者は連鎖を再計算できない(§4.1(a) のOS権限分離鍵)。
#
# 末尾行の削除は行同士の連鎖だけでは検知できないため、現在の count/last_hash を署名したanchorを
# ledger隣接に置き、その期待値を鍵ディレクトリにも分離保存する。これにより ledger と古いanchorを
# 同時に巻き戻しても検知する。ただし鍵ディレクトリまで読み書きできる攻撃者には偽造可能であり、
# 別ホスト/WORM/署名タイムスタンプへの外部コミットは Stage1 の残リスク。
#
# 限界(過大評価しない・設計正本§5.3/§7.2の繰延と整合):
#  - DBの audit_log トリガ(UPDATE/DELETE拒否)は defense-in-depth。SQLiteは `DROP TRIGGER` で外せるため
#    真の権限剥奪は **Stage2 の自己ホストPostgreSQL権限剥奪**(§5.3)。Stage0の一次防御はこのHMACチェーン。
_DEFAULT_KEY = "audit_chain.key"
_ANCHOR_SUFFIX = ".anchor"
_ANCHOR_STATE_DIR = "audit-anchor-state"
_ANCHOR_VERSION = 1
_ANCHOR_DOMAIN = b"ainote-audit-anchor-v1\0"
_STATE_DOMAIN = b"ainote-audit-anchor-state-v1\0"


def default_chain_key_path() -> Path:
    """連鎖HMAC鍵の置き場。out/ やバックアップとは別ディレクトリ・OS権限分離(chmod600)。"""
    env = os.environ.get("RI_HUB_AUDIT_KEY_PATH")
    if env:
        return Path(env)
    return Path.home() / ".ri-hub" / "keys" / _DEFAULT_KEY


def _load_chain_key(key_path=None) -> bytes:
    # backup.load_or_create_key を流用(32byte・chmod600・dir700・原子的作成)。
    from hub_core.backup import load_or_create_key
    return load_or_create_key(key_path or default_chain_key_path())


class AuditChainError(Exception):
    """ledger のハッシュチェーンが壊れている(改ざんの可能性)。append を fail-closed で拒否する。"""


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def dedup_key(raw_event):
    """同一論理イベントの安定 identity。raw イベントから揮発フィールド(timestamp)のみ除外して sha256。

    event_id(AUD-{day}-... 等)を含む内在内容で決まるため、同日同入力の再実行では一致し重複追記されない。
    内容が変われば(件数差など)別キーになり、新エントリとして正直に追記される。
    """
    body = {k: v for k, v in raw_event.items() if k != "timestamp"}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def normalize_audit_event(event, seq):
    """監査イベントを標準フィールド(audit_id/timestamp/actor/action/target/gate_status)に正規化する。
    元のフィールドは保持しつつ欠落を補完する(MT-172)。"""
    out = dict(event)
    out.setdefault("audit_id", event.get("event_id") or f"AUD-SEQ-{seq:05d}")
    out.setdefault("timestamp", datetime.now(JST).replace(microsecond=0).isoformat())
    out.setdefault("actor", "ri-hub")
    out.setdefault("action", "event")
    out.setdefault("target", event.get("source_ref") or event.get("task_id") or "")
    out.setdefault("gate_status", "pass")
    return out


def _entry_hash(event, prev_hash, key: bytes):
    """entry_hash を除く全フィールド(prev_hash/seq/dedup_key/timestamp 含む)を本文として、
    秘密鍵 key による HMAC-SHA256 で連鎖。鍵を持たない攻撃者は再計算できない(全再計算偽造を封じる)。"""
    body = _canon({k: v for k, v in event.items() if k != "entry_hash"})
    return hmac.new(key, (prev_hash + body).encode("utf-8"), hashlib.sha256).hexdigest()


def _anchor_path(path: Path) -> Path:
    return path.with_name(path.name + _ANCHOR_SUFFIX)


def _ledger_id(path: Path) -> str:
    """鍵側の状態を、同じ鍵を使う複数 ledger 間で衝突させない識別子。"""
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _anchor_state_path(path: Path) -> Path:
    return default_chain_key_path().parent / _ANCHOR_STATE_DIR / f"{_ledger_id(path)}.json"


def _signed_payload(payload: dict, key: bytes, domain: bytes) -> dict:
    body = dict(payload)
    body["signature"] = hmac.new(
        key, domain + _canon(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return body


def _verify_signed_payload(value, key: bytes, domain: bytes) -> dict | None:
    if not isinstance(value, dict):
        return None
    payload = {k: v for k, v in value.items() if k != "signature"}
    stored = str(value.get("signature") or "")
    expected = hmac.new(
        key, domain + _canon(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not stored or not hmac.compare_digest(stored, expected):
        return None
    return payload


def _atomic_write_json(path: Path, value: dict) -> None:
    """同じディレクトリの一時ファイルを fsync してから replace する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _anchor_for(path: Path, events: list[dict], key: bytes) -> dict:
    payload = {
        "version": _ANCHOR_VERSION,
        "ledger": path.name,
        "count": len(events),
        "last_hash": events[-1].get("entry_hash", "") if events else GENESIS,
    }
    return _signed_payload(payload, key, _ANCHOR_DOMAIN)


def _state_for(path: Path, anchor: dict, key: bytes) -> dict:
    payload = {
        "version": _ANCHOR_VERSION,
        "ledger": path.name,
        "ledger_id": _ledger_id(path),
        "count": anchor["count"],
        "last_hash": anchor["last_hash"],
        "anchor_signature": anchor["signature"],
    }
    return _signed_payload(payload, key, _STATE_DOMAIN)


def _write_anchor_state(path: Path, events: list[dict], key: bytes) -> None:
    """ledger追記後の状態を、隣接anchor→鍵側期待値の順に原子的に進める。"""
    anchor = _anchor_for(path, events, key)
    _atomic_write_json(_anchor_path(path), anchor)
    _atomic_write_json(_anchor_state_path(path), _state_for(path, anchor, key))


def _configured_key_matches(key: bytes) -> bool:
    """明示keyが実運用の別保管鍵か。任意keyを使う低レベルテストは従来のchain検証だけに保つ。"""
    try:
        configured = default_chain_key_path().read_bytes()
    except OSError:
        return False
    return len(configured) == len(key) and hmac.compare_digest(configured, key)


def _verify_anchor_state(path: Path, events: list[dict], key: bytes) -> list:
    """末尾削除・anchor改変/削除を検知する。旧ledgerは一度だけ現在値から移行する。"""
    anchor_path = _anchor_path(path)
    state_path = _anchor_state_path(path)
    anchor_exists = anchor_path.is_file()
    state_exists = state_path.is_file()

    # anchor作成済みの印が鍵側にあるのに隣接anchorが無い場合は、旧形式扱いに戻さない。
    if state_exists and not anchor_exists:
        return ["anchor_missing"]
    if not anchor_exists:
        if not events:
            return []
        # 既存の非空ledgerにanchorが無い旧版だけ、検証済みの現在値を初回anchorにする。
        _write_anchor_state(path, events, key)
        return []

    raw_anchor = _read_json_file(anchor_path)
    anchor = _verify_signed_payload(raw_anchor, key, _ANCHOR_DOMAIN)
    if anchor is None:
        return ["anchor_signature"]
    if anchor.get("version") != _ANCHOR_VERSION or anchor.get("ledger") != path.name:
        return ["anchor_identity"]

    expected_count = len(events)
    expected_hash = events[-1].get("entry_hash", "") if events else GENESIS
    if anchor.get("count") != expected_count:
        return ["anchor_count"]
    if anchor.get("last_hash") != expected_hash:
        return ["anchor_last_hash"]

    if state_exists:
        raw_state = _read_json_file(state_path)
        state = _verify_signed_payload(raw_state, key, _STATE_DOMAIN)
        if state is None:
            return ["anchor_state_signature"]
        if (state.get("version") != _ANCHOR_VERSION
                or state.get("ledger") != path.name
                or state.get("ledger_id") != _ledger_id(path)):
            return ["anchor_state_identity"]
        if (state.get("count") != anchor.get("count")
                or state.get("last_hash") != anchor.get("last_hash")
                or state.get("anchor_signature") != raw_anchor.get("signature")):
            return ["anchor_state_mismatch"]
    else:
        # 正しいanchorを含むバックアップを別パスへ復元した場合などは、その場所の期待値を登録する。
        _atomic_write_json(state_path, _state_for(path, raw_anchor, key))
    return []


def _read_ledger(path):
    events = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return events
    except (OSError, UnicodeError) as exc:
        raise AuditChainError(f"監査ログを読み取れません: {path}: {exc}") from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if line:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditChainError(
                    f"監査ログのJSON行が壊れています: line={line_no}: {exc.msg}"
                ) from exc
            if not isinstance(event, dict):
                raise AuditChainError(f"監査ログの行がオブジェクトではありません: line={line_no}")
            events.append(event)
    return events


def verify_audit_chain(path, key=None):
    """HMACチェーンを検証し、壊れている行の seq(無ければ行番号)を返す。空リストなら整合。
    key 省略時は別保管の連鎖鍵を読む。鍵が無ければ検証不能=全行をbrokenとして fail-closed。"""
    explicit_key = key is not None
    if key is None:
        key = _load_chain_key()
    enforce_anchor = not explicit_key or _configured_key_matches(key)
    path = Path(path)
    events = _read_ledger(path)
    broken = []
    prev_hash = GENESIS
    for i, event in enumerate(events, start=1):
        stored = event.get("entry_hash", "")
        expected = _entry_hash(event, event.get("prev_hash", ""), key)
        if event.get("prev_hash") != prev_hash or not hmac.compare_digest(stored, expected):
            broken.append(event.get("seq", i))
        prev_hash = stored
    if not broken and enforce_anchor:
        broken.extend(_verify_anchor_state(path, events, key))
    return broken


def append_events(path, raw_events, key=None):
    """raw_events のうち未記録のものだけを ledger 末尾へ追記する(過去行は一切書き換えない)。

    既存 ledger が改ざんされていれば AuditChainError(自動修復しない=fail-closed)。
    エントリハッシュは別保管の秘密鍵による HMAC(全再計算偽造を封じる)。戻り値: 追記件数。

    **排他制御つき**: 「末尾を読む→prev_hash を決める→追記」は不可分でなければならない。
    サーバは複数スレッドで応答するため、2つの操作が同時に来ると同じ prev_hash を持つ行が
    2本書かれ、以後 verify_audit_chain が恒久的に壊れたと判定して**全業務が止まる**
    （自動修復しない設計なので復旧に手作業が要る）。プロセス内はロック、プロセス間は
    ファイルロックで直列化する。
    """
    path = Path(path)
    with _ledger_lock(path):
        return _append_events_locked(path, raw_events, key)


_LOCKS: dict[str, "threading.Lock"] = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _ledger_lock(path):
    """同じ台帳への書き込みを直列化する（同一プロセス内＋別プロセス間）。"""
    keyname = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lk = _LOCKS.setdefault(keyname, threading.Lock())
    with lk:
        lockfile = Path(path).with_name(Path(path).name + ".lock")
        fh = None
        try:
            lockfile.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lockfile, "a+")
            if fcntl is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass      # ロックを取れない環境ではプロセス内ロックだけで進む
            yield
        finally:
            if fh is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                fh.close()


def _append_events_locked(path, raw_events, key=None):
    path = Path(path)
    explicit_key = key is not None
    if key is None:
        key = _load_chain_key()
    enforce_anchor = not explicit_key or _configured_key_matches(key)
    existing = _read_ledger(path)
    broken = verify_audit_chain(path, key)
    if broken:
        raise AuditChainError(
            f"監査ログのハッシュチェーンが壊れています(改ざんの可能性): seq={broken}。"
            f" append-only のため自動修復しません。{path} を保全して原因を調査してください。"
        )
    if existing:
        seen = {e.get("dedup_key") for e in existing}
        prev_hash = existing[-1]["entry_hash"]
        seq = existing[-1].get("seq", len(existing))
    else:
        seen, prev_hash, seq = set(), GENESIS, 0

    new_lines = []
    appended = 0
    for raw in raw_events:
        dk = dedup_key(raw)
        if dk in seen:
            continue
        seq += 1
        event = normalize_audit_event(raw, seq)
        event["seq"] = seq
        event["dedup_key"] = dk
        event["prev_hash"] = prev_hash
        event["entry_hash"] = _entry_hash(event, prev_hash, key)
        prev_hash = event["entry_hash"]
        seen.add(dk)
        new_lines.append(json.dumps(event, ensure_ascii=False))
        appended += 1

    if new_lines:
        # 改行終端だけが欠けていても既存JSONと新規JSONを連結しない。補う改行も末尾追記であり、
        # 過去行は書き換えない。ledgerをfsyncした後でanchorを進める。
        needs_separator = path.exists() and path.stat().st_size > 0
        if needs_separator:
            with path.open("rb") as raw_file:
                raw_file.seek(-1, os.SEEK_END)
                needs_separator = raw_file.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as f:
            if needs_separator:
                f.write("\n")
            f.write("\n".join(new_lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        existing.extend(json.loads(line) for line in new_lines)
        if enforce_anchor:
            _write_anchor_state(path, existing, key)
    elif not path.exists():
        path.write_text("", encoding="utf-8")
    return appended


# 旧名 write_audit_log_chained の後方互換シム(=append化。再生成ではなく追記する)。
def write_audit_log_chained(path, events):
    """後方互換: append_events へ委譲(A-1是正で再生成から追記へ意味変更)。"""
    return append_events(path, events)
