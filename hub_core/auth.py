"""認証・認可(Stage0 S0-3)。

- 認証: pbkdf2_hmac(sha256) パスワードハッシュ + サーバ側セッション(メモリ) + cookie。stdlibのみ。
- 認可RBAC: 役割(担当/経理/宅建士/責任者/代表)で行スコープ + 個人情報列マスク。
  query_page(viewer) に差し込み「read には viewer 必須」を実現する(ONLINE_ARCHITECTURE.md §3)。
- 初期データが無いソース開発環境だけは dev mode（代表ロール自動・画面に明示バナー）。
  company.json / users.json が一度でも作られた環境と配布アプリは認証を fail-closed にする。
  ソース開発で明示的に RI_HUB_AUTH=off を指定した場合だけ認証を外せる。

注意: 行/列ポリシーは「最も制限の強い担当=自分の担当行のみ+連絡先/LINEマスク」の保守的既定。
正確な役割×列ポリシーは業務確認で調整可(S0-4でPII隔離を深掘り)。本モジュールは機構を提供する。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ("担当", "経理", "宅建士", "責任者", "代表")
ROLES_SEE_PII = {"経理", "宅建士", "責任者", "代表"}   # 担当は連絡先/LINE等をマスク
PII_LABELS = {"連絡先", "LINEユーザーID"}
# 自由文に電話/メールが埋め込まれうる列(S0-4: PII閲覧権なしには伏字化=A-2の値レベル封じ)
FREE_TEXT_LABELS = {
    "タイトル", "理由", "詳細", "解除条件", "終結条件", "緊急度理由",
    "名称", "名寄せ別名", "近隣トラブル", "行政連絡", "確認対象",
}
ASSIGNEE_LABEL = "担当"
PII_MASK = "****"


@dataclass
class Viewer:
    user: str
    role: str
    is_dev: bool = False
    # 台帳の「担当」列に入る表示名。ログインIDは英字、台帳は日本語氏名という運用が普通なので、
    # 両者を結ぶ値を管理者が users.json に登録する。未登録ならログインIDだけが自分の名前。
    display_name: str = ""
    # 宅地建物取引士として記名確定できる場合の登録番号。会社免許番号とは別物。
    registration_no: str = ""

    def sees_pii(self) -> bool:
        return self.role in ROLES_SEE_PII

    def sees_all_rows(self) -> bool:
        return self.role != "担当"

    def identities(self) -> frozenset[str]:
        """台帳上「自分」と読める文字列の集合。所有権照合はこれと突き合わせる。"""
        return frozenset(v for v in (str(self.user or "").strip(),
                                     str(self.display_name or "").strip()) if v)


# 内部/テスト用のフル権限(serve は常に実 viewer を渡す。None は内部用途のみ)
SYSTEM = Viewer("system", "代表")


# --- パスワード(pbkdf2_hmac sha256) ---
def hash_password(pw: str, salt: str | None = None, iters: int = 200_000) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


# --- ユーザーストア(auth/users.json・パスワードハッシュのみ・gitignore) ---
def users_path(data_dir) -> Path:
    return Path(data_dir).parent / "auth" / "users.json"


def load_users(data_dir) -> dict:
    p = users_path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("users", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def auth_required(data_dir) -> bool:
    """設定済みデータまたは明示onでは認証必須。明示offだけをdev用に許す。"""
    mode = os.environ.get("RI_HUB_AUTH", "").strip().lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    # 内容を読めることを条件にすると、空・破損した認証正本が未認証の代表
    # dev modeへ倒れる。会社正本だけが残った場合も、復旧まで認証を要求する。
    return company_path(data_dir).exists() or users_path(data_dir).exists()


def authenticate(data_dir, user: str, pw: str) -> Viewer | None:
    rec = load_users(data_dir).get(user)
    if not rec:
        return None
    if not verify_password(pw, rec.get("password_hash", "")):
        return None
    role = rec.get("role", "担当")
    if role not in ROLES:
        role = "担当"
    return Viewer(
        user, role,
        display_name=str(rec.get("display_name") or "").strip(),
        registration_no=str(rec.get("registration_no") or rec.get("takkenshi_reg") or "").strip(),
    )


# --- セッション(サーバ側メモリ) ---
# 配布版の認証寿命は端末の環境変数で延長できない固定値とする。Cookie が残っていても、
# サーバ側レコードの絶対期限またはアイドル期限を過ぎた時点で認証は成立しない。
SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60
SESSION_IDLE_TTL_SECONDS = 30 * 60


@dataclass
class _SessionRecord:
    viewer: Viewer
    created_at: float
    last_seen_at: float


_SESSIONS: dict[str, _SessionRecord] = {}
_SESSIONS_LOCK = threading.Lock()
_clock = time.monotonic


def _session_expired(record: _SessionRecord, now: float) -> bool:
    return (now - record.created_at >= SESSION_ABSOLUTE_TTL_SECONDS
            or now - record.last_seen_at >= SESSION_IDLE_TTL_SECONDS)


def _prune_expired_sessions(now: float) -> None:
    for sid, record in list(_SESSIONS.items()):
        if _session_expired(record, now):
            _SESSIONS.pop(sid, None)


def create_session(viewer: Viewer) -> str:
    sid = secrets.token_urlsafe(32)
    now = _clock()
    with _SESSIONS_LOCK:
        _prune_expired_sessions(now)
        _SESSIONS[sid] = _SessionRecord(viewer, now, now)
    return sid


def get_session(sid: str | None) -> Viewer | None:
    if not sid:
        return None
    now = _clock()
    with _SESSIONS_LOCK:
        record = _SESSIONS.get(sid)
        if record is None:
            return None
        if _session_expired(record, now):
            _SESSIONS.pop(sid, None)
            return None
        record.last_seen_at = now
        return record.viewer


def destroy_session(sid: str | None):
    if sid:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(sid, None)


# --- 認可: 行スコープ + 個人情報列マスク ---
def authorize_rows(label_rows, viewer: Viewer | None):
    """日本語ラベルキーの行配列に viewer の認可を適用して返す(読み取りのみ)。

    - 担当: 担当列が自分(または空=未割当)の行のみ + 個人情報列(連絡先/LINE)をマスク
    - それ以外: 全行・全列
    - viewer is None(内部/テスト): 全行・全列(serveは常に実viewerを渡す)
    """
    if viewer is None:
        return label_rows
    rows = label_rows
    if not viewer.sees_all_rows() and rows and any(ASSIGNEE_LABEL in r for r in rows):
        identities = viewer.identities()
        rows = [
            r for r in rows
            if str(r.get(ASSIGNEE_LABEL) or "").strip() in ("", *identities)
        ]
    if not viewer.sees_pii():
        from .pii import redact_text
        masked = []
        for r in rows:
            nr = dict(r)
            for k in list(nr.keys()):
                if k in PII_LABELS and nr[k] not in (None, "", PII_MASK):
                    nr[k] = PII_MASK              # PII列は全マスク
                elif k in FREE_TEXT_LABELS:
                    nr[k] = redact_text(nr[k])    # 自由文の電話/メールを伏字化(A-2を値レベルで封じる)
            masked.append(nr)
        rows = masked
    return rows


# --- 会社設定(オンボーディング・auth/company.json) ---
def company_path(data_dir) -> Path:
    return Path(data_dir).parent / "auth" / "company.json"


class CompanyProfileError(Exception):
    """会社情報の正本が壊れている。空で進めず、呼び出し側に判断させる。"""


def load_company(data_dir, *, strict: bool = False) -> dict:
    """会社情報を読む。

    strict=True では**壊れた正本を空扱いにしない**（CompanyProfileError）。
    静かに {} を返すと「設定済みなのに中身が空」となり、帯は fail-closed でも
    FAX はプレースホルダのまま送信され、重説は空欄のまま交付まで進み得る。
    お客様に届く出力を作る経路は strict=True で呼ぶこと。
    """
    p = company_path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if strict:
            raise CompanyProfileError(
                f"会社情報のファイルが壊れています（{p}）: {exc}。"
                "「業者情報」の画面で登録し直すか、バックアップから戻してください。")
        return {}
    if not isinstance(data, dict):
        if strict:
            raise CompanyProfileError(f"会社情報の形式が不正です（{p}）。")
        return {}
    return data


def is_configured(data_dir) -> bool:
    """初期設定が済んでいるか。

    会社設定(company.json)**または**利用者が1人でもいれば、初期設定の入口は閉じる。
    company.json だけを見ていると、「利用者はいるが会社情報が未登録」という窓が
    残り、そこでは /setup が認証なしで開いて誰でも代表アカウントを作れてしまう
    （2026-08-07 監査で確認）。会社情報の追加は、ログインした代表が行う。
    """
    # 正本の内容が空・破損でも、存在する限り初期設定を再公開しない。
    # 復旧は認証ファイルを保全したうえで管理者が端末上で行う。
    return company_path(data_dir).exists() or users_path(data_dir).exists()


def _secure_write_json(p: Path, data: dict) -> None:
    """所有者のみ(0600)でJSONを書き、親auth/は0700に。認証情報の漏洩防止。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    # 同じディレクトリに一時ファイルを書いて fsync してから rename する（原子的置換）。
    # 直接 O_TRUNC で上書きすると、途中停止・競合・ディスク異常で正本が欠け、
    # 会社情報を正本とする全出力（帯・LINE・重説・ポータル）が同時に壊れる。
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def save_company(data_dir, company: dict) -> None:
    _secure_write_json(company_path(data_dir), company)


def save_user(data_dir, user: str, pw: str, role: str = "代表",
              display_name: str = "", registration_no: str = "") -> None:
    """users.json に1ユーザー追加(パスワードハッシュのみ・所有者のみ0600)。"""
    if role not in ROLES:
        role = "担当"
    users = load_users(data_dir)
    record = {"password_hash": hash_password(pw), "role": role}
    display = str(display_name or "").strip()
    if display:
        record["display_name"] = display
    reg = str(registration_no or "").strip()
    if reg:
        record["registration_no"] = reg
    users[user] = record
    _secure_write_json(users_path(data_dir), {"users": users})
