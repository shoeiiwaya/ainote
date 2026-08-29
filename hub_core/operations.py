"""操作OS の統一コア(S1)。

专门UI(serve.py POST) と Claude(mcp_server.py) が **同一ロジック・同一 hub.db** で呼ぶ
単一の操作層。各操作 = RBAC権限ゲート(基準9) + store 更新 + HMAC監査追記。
読み取りは serve/views 側(query_page)が hub.db を見るので、ここで書けば专门UIに反映する。
監査台帳系(audit/viewlog/ledger)は操作対象外=閲覧専用(完全性のため)。
"""
from __future__ import annotations

import hashlib
import os
import contextlib
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windowsではプロセス内ロックだけを使う。
    fcntl = None

from . import op_scope
from .audit import AuditChainError, append_events, verify_audit_chain
from .schema_cols import COLS
from .store import SqliteStore

JST = timezone(timedelta(hours=9))


def _full_row(table: str, partial: dict) -> dict:
    """テーブルの全列を "" で初期化し partial で上書き。未設定列がNULL→Noneで
    UIに『None』と漏れるのを防ぐ(synced行と同形にそろえる)。"""
    row = {key: "" for _, key in COLS.get(table, [])}
    row.update({k: v for k, v in (partial or {}).items() if v is not None})
    return row


def _now() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


CASE_STAGES = ["反響", "内見", "申込", "契約", "管理"]  # 集約ビュー用(5段階・後方互換)

# --- Phase3: 四象限化(取引種別×立場) ---------------------------------------
# 取引種別の分岐を固定し、顧客行動フローを実装で一貫化する。
DEAL_TYPES = ("lease_tenant", "lease_landlord", "sale_buyer", "sale_seller")

# 止めるべきゲート6値域(gate_status の標準値域)。
GATE_STATUSES = ("law", "money", "approval", "identity", "consent", "stock")

# deal_type 別の細粒度ステージ(顧客ジャーニー §3)。
STAGES_BY_DEAL = {
    "lease_tenant": ["反響", "追客", "ヒアリング", "物件提案", "内見", "申込",
                     "審査", "重説", "契約", "初期費用", "鍵渡し", "管理"],
    "lease_landlord": ["物件確認", "募集", "申込受付", "審査取次", "管理引継"],
    "sale_buyer": ["反響ヒアリング", "内見", "買付事前審査", "条件交渉", "重説",
                   "売買契約", "ローン本審査", "決済引渡", "アフター"],
    "sale_seller": ["査定受託", "媒介契約", "物件調査", "REINS登録", "販売活動",
                    "買付受付", "契約", "決済"],
}


# 失注理由(選択式・8分類＋その他)。自由文を許すと集計不能になるため値域固定。
# 「自社対応遅れ」を含める=失注理由の蓄積を仕入れ・掲載改善だけでなく自社動線改善の源泉にする。
LOST_REASONS = ("予算不一致", "他社成約", "希望物件なし", "条件不一致", "審査否決",
                "時期延期・中止", "連絡不通", "自社対応遅れ", "その他")


def normalize_deal_type(raw: str) -> str:
    """自由文(inquiry_type 等)を deal_type 4値域へ正規化。既に値域ならそのまま。"""
    s = str(raw or "").strip().lower()
    if s in DEAL_TYPES:
        return s
    j = str(raw or "")
    is_sale = any(k in j for k in ("売買", "購入", "売却", "sale", "buy"))
    is_landlord = any(k in j for k in ("貸主", "元付", "募集受託", "売主", "landlord", "seller"))
    if is_sale:
        return "sale_seller" if ("売却" in j or "売主" in j or "seller" in s) else "sale_buyer"
    return "lease_landlord" if is_landlord else "lease_tenant"


def stages_for(deal_type: str) -> list:
    """deal_type に対応する細粒度ステージ列。未知は賃貸客付けにフォールバック。"""
    return STAGES_BY_DEAL.get(normalize_deal_type(deal_type), STAGES_BY_DEAL["lease_tenant"])


def identity_required(deal_type: str) -> bool:
    """犯収法の取引時確認(identityゲート)が法令必須か。**売買の媒介/代理のみ対象**・
    賃貸借の媒介は対象外(賃貸の本人確認は審査用=任意・7年保存義務なし)。"""
    return normalize_deal_type(deal_type).startswith("sale_")

# 操作 × 実行可能ロール(基準9: 代表が社員に与える権限の基本表)
OP_ROLES = {
    "case_advance": {"担当", "宅建士", "責任者", "代表"},
    "case_lose": {"担当", "宅建士", "責任者", "代表"},
    "task_done": {"担当", "経理", "宅建士", "責任者", "代表"},
    "task_snooze": {"担当", "経理", "宅建士", "責任者", "代表"},    # 後回し(可逆・クエリ時自然失効)
    "task_unsnooze": {"担当", "経理", "宅建士", "責任者", "代表"},  # スヌーズ解除(可逆)
    "approval_decide": {"責任者", "代表"},
    "hold_release": {"責任者", "代表"},
    # 業務動線(反響→顧客→案件→内見→契約→請求)を一気通貫にする作成系操作
    "lead_convert": {"担当", "宅建士", "責任者", "代表"},      # 反響→顧客+案件(可逆)
    "customer_case_create": {"担当", "宅建士", "責任者", "代表"},  # 既存顧客→新しい案件
    "lead_quick_add": {"担当", "宅建士", "責任者", "代表"},    # 反響の手動登録(電話/紹介・可逆)
    "inbox_ingest": {"担当", "宅建士", "責任者", "代表"},      # 反響/フォルダ取込(冪等)
    "extraction_save": {"担当", "宅建士", "責任者", "代表"},   # 書類抽出draft(出典必須・可逆)
    "ocr_extract": {"担当", "宅建士", "責任者", "代表"},       # OCR読取draft(BYO vision・出典束縛)
    "extraction_approve": {"宅建士", "責任者", "代表"},        # 抽出の人間承認(専門確認)
    "esign_create": {"宅建士", "責任者", "代表"},            # 電子契約封筒の作成(BYO)
    "esign_send": {"宅建士", "責任者", "代表"},              # 電子契約の送信(未接続=モック)
    "application_create": {"担当", "宅建士", "責任者", "代表"},  # 入居申込受付(可逆)
    "application_advance": {"担当", "宅建士", "責任者", "代表"}, # 申込ステート前進(可逆)
    "screening_result": {"宅建士", "責任者", "代表"},          # 審査結果反映(未接続は確定不可)
    "permission_record": {"担当", "宅建士", "責任者", "代表"},  # 帯替え許諾の台帳記録
    "obi_swap": {"担当", "宅建士", "責任者", "代表"},           # 帯替え出力(許諾ゲート必須)
    "viewing_schedule": {"担当", "宅建士", "責任者", "代表"},  # 内見予約(可逆)
    "viewing_list": {"担当", "宅建士", "責任者", "代表"},      # 内見予約の物件別照会(読取)
    "renewal_generate": {"担当", "宅建士", "責任者", "代表"},    # 更新案内ドラフト自動生成(可逆)
    "contract_create": {"担当", "宅建士", "責任者", "代表"},     # 契約台帳登録(更新期限管理)
    "moveout_settle": {"担当", "経理", "宅建士", "責任者", "代表"},  # 退去精算(敷金精算・ドラフト)
    "zoning_lookup": {"担当", "宅建士", "責任者", "代表"},  # 用途地域判定(座標→ローカル点in面・読取)
    "ocr_read": {"担当", "宅建士", "責任者", "代表"},  # 原本の無料ローカルOCR＋幾何構造化(読取・ドラフト)
    "bukkaku_send": {"担当", "宅建士", "責任者", "代表"},   # 物確FAX作成→outbox(queued・実送信しない)
    "fax_confirm_send": {"宅建士", "責任者", "代表"},        # FAX送信ゲート(人間確認・既定Mock)
    "fax_receive": {"担当", "宅建士", "責任者", "代表"},     # 着信FAX→物確回答抽出(Webhookから・ドラフト)
    "line_send": {"担当", "宅建士", "責任者", "代表"},       # LINEメッセージ作成→outbox(実送信しない)
    "line_flex_property_send": {"担当", "宅建士", "責任者", "代表"},  # 公開物件をFlexカードでoutbox(実送信しない)
    "line_confirm_send": {"宅建士", "責任者", "代表"},       # LINE送信ゲート(人間確認・既定Mock)
    "line_receive": {"担当", "宅建士", "責任者", "代表"},    # LINE着信→接触素材(Webhookから・ドラフト)
    "line_harness_pull": {"担当", "宅建士", "責任者", "代表"},  # harness APIをpull→未取込のincomingをinboxへ(dedupe永続)
    "inquiry_create": {"担当", "宅建士", "責任者", "代表"},  # LINE着信のポータルURL→案内可否確認(物確ドラフト・未確認)
    "inquiry_resolve": {"担当", "宅建士", "責任者", "代表"},  # 案内可否を記入(未確認→回答済)＋顧客回答をqueued
    "hearing_create": {"担当", "宅建士", "責任者", "代表"},  # LIFF条件ヒアリング着信→希望条件レコードを台帳化(受付)
    "caller_directory_add": {"担当", "宅建士", "責任者", "代表"},  # 発信者台帳(電話→業者)登録
    "bukkaku_code_assign": {"担当", "宅建士", "責任者", "代表"},   # 物件に物確番号を割当
    "call_receive": {"担当", "宅建士", "責任者", "代表"},    # 着信電話→状態応答＋通話ログ(Webhookから)
    "property_status_set": {"担当", "宅建士", "責任者", "代表"},  # 物確状態(取扱中/成約済等)を設定
    "reins_prepare": {"宅建士", "責任者", "代表"},           # REINS入稿シート生成＋法定期限(REINS非接触)
    "reins_record": {"宅建士", "責任者", "代表"},            # 会員登録後のREINS番号記録
    "activity_report": {"担当", "宅建士", "責任者", "代表"},    # 売主活動報告書生成(法定34条の2)
    "it_session_create": {"担当", "宅建士", "責任者", "代表"},  # IT重説セッション作成(BYO映像)
    "it_check_requirement": {"宅建士", "責任者", "代表"},       # IT重説法定要件の充足記録(専門)
    "it_advance": {"宅建士", "責任者", "代表"},                # IT重説の状態遷移(実施は要件ゲート)
    "it_gate_set": {"責任者", "代表"},                        # IT重説の運用開始ゲート(免許/宅建士登録/GL確認)を会社設定へ
    "it_schedule_confirm": {"担当", "宅建士", "責任者", "代表"},  # IT重説の日時確定＋BYO映像URL送付(queued)
    "juusetsu_consent_record": {"宅建士", "責任者", "代表"},    # 電磁的交付の事前承諾記録(方法/形式/日時)
    "juusetsu_deliver": {"宅建士", "責任者", "代表"},          # 確定済み重説の電磁的交付証跡(hash束縛・送信はqueued)
    "schedule_slots": {"担当", "宅建士", "責任者", "代表"},      # 担当カレンダーの空き枠算出(内見/IT重説・読取)
    "portal_link": {"担当", "宅建士", "責任者", "代表"},        # 顧客ポータルのリンク生成
    "billing_create": {"経理", "責任者", "代表"},             # 請求作成(金銭=人間確認)
    "reconcile_deposits": {"経理", "責任者", "代表"},          # 全銀突合(読取専用・候補提示)
    "overdue_reminders": {"経理", "責任者", "代表"},           # 延滞請求の督促ドラフト(可逆)
    "billing_reconcile": {"経理", "責任者", "代表"},           # 入金消込確定(金銭=人間確認)
    # Phase3 タイムライン操作(顧客ジャーニーを動かす・可逆)
    "stage_advance": {"担当", "宅建士", "責任者", "代表"},     # ジャーニーのステージ前進
    "attribute_update": {"担当", "宅建士", "責任者", "代表"},  # 顧客属性の追加/更新
    "contact_log_add": {"担当", "経理", "宅建士", "責任者", "代表"},  # 接触履歴の記録(滞留リセット)
    "followup_generate": {"担当", "宅建士", "責任者", "代表"},  # 追客ドラフト自動生成(可逆)
    "message_draft": {"担当", "経理", "宅建士", "責任者", "代表"},  # 送信ドラフト作成(可逆・経理=督促)
    "message_queue": {"担当", "経理", "宅建士", "責任者", "代表"},  # 送信キューへ(可逆)
    "message_send": {"責任者", "代表"},                       # 実送信(人間ゲート・BYO)
    "proposal_draft": {"担当", "宅建士", "責任者", "代表"},     # PR/提案文の下書き(AI臭ガード)
    "asset_attest": {"担当", "宅建士", "責任者", "代表"},       # 素材の自社記名provenance発行
    "property_register": {"担当", "宅建士", "責任者", "代表"},  # 物件登録(PRSリスクcache)
    "requirement_check": {"担当", "宅建士", "責任者", "代表"},  # 書類要件の充足更新
    "liff_publish": {"担当", "宅建士", "責任者", "代表"},       # 物件のLIFF公開opt-in+顧客表示フィールド(可逆)
    "liff_export": {"担当", "宅建士", "責任者", "代表"},        # 公開物件→LIFF properties.json+写真 書出(読取→書出・可逆)
}

# ステージのデフォルト期限日数(滞留検知の due_at 算出。設計§3の代表値)
STAGE_DUE_DAYS = {
    "反響": 1, "追客": 3, "ヒアリング": 3, "物件提案": 5, "内見": 3, "申込": 2, "審査": 7,
    "重説": 2, "契約": 3, "初期費用": 3, "鍵渡し": 3, "管理": 180,
    "反響ヒアリング": 7, "買付事前審査": 3, "条件交渉": 7, "売買契約": 3,
    "ローン本審査": 14, "決済引渡": 5, "アフター": 30,
    "査定受託": 5, "媒介契約": 3, "物件調査": 7, "REINS登録": 2, "販売活動": 14, "買付受付": 3, "決済": 5,
}


def aggregate_status(stage: str) -> str:
    """細粒度ステージ→既存5段階status(集約ビュー用)へ写像。"""
    s = str(stage or "")
    if s in ("内見",):
        return "内見"
    if s in ("申込", "審査", "買付事前審査", "条件交渉", "申込受付", "審査取次"):
        return "申込"
    if s in ("重説", "契約", "売買契約", "ローン本審査", "初期費用", "決済引渡", "決済", "媒介契約"):
        return "契約"
    if s in ("鍵渡し", "管理", "管理引継", "アフター"):
        return "管理"
    return "反響"


class OpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _parse_amount(raw) -> int:
    """金額文字列→非負整数。全角/半角カンマ・円・空白を除去。不正/負値は OpError(400)。"""
    s = str(raw or "").strip().replace(",", "").replace("，", "").replace("円", "").replace(" ", "")
    # 全角数字→半角
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    try:
        v = int(s)
    except ValueError:
        raise OpError(400, f"金額は数値で入力してください（受領: {raw!r}）。")
    if v < 0:
        raise OpError(400, "金額は0以上で入力してください。")
    return v


def _store(data_dir):
    """業務台帳（hub.db）。取り出す前に監査が健全かを確かめる。"""
    _require_healthy_audit(data_dir)
    db = Path(data_dir) / "hub.db"
    if not db.exists():
        # CSVは業務台帳の正本。名簿を取り込んだ直後はまだDBが無いため、空DBを
        # 先に作ると最初の業務操作を境に全顧客が画面から消える。書き込み前に
        # 正本全体を派生DBへ復元し、その断面へ今回の操作を積む。
        from . import vault
        vault.rebuild_business_tables(data_dir)
    return SqliteStore(db)


def _require_customer(st: SqliteStore, customer_id: str) -> dict:
    """Return a real customers-ledger row, refusing arbitrary/orphan customer IDs."""
    rows = st.query("customers", "customer_id = ?", (customer_id,))
    if rows:
        return rows[0]
    raise OpError(404, f"顧客 {customer_id} が見つかりません。")


def _require_healthy_audit(data_dir):
    """監査チェーンが壊れていたら、台帳へ書く**前に**止める。"""
    log = Path(data_dir) / "audit_log.jsonl"
    if not log.is_file():
        return
    try:
        broken = verify_audit_chain(log)
    except AuditChainError as exc:
        raise OpError(409, str(exc))
    except OSError:
        return          # 読めないだけなら従来どおり進む（_audit 側で再度確かめる）
    if broken:
        raise OpError(409, "監査ログのハッシュチェーンが壊れています(改ざんの可能性): "
                           f"seq={broken}。台帳を守るため、この操作は行いませんでした。")


def _audit(data_dir, event):
    journal = getattr(_OPERATION_LOCAL, "webhook_journal", None)
    if journal is not None and journal.root == Path(data_dir).resolve():
        journal.events.append(event)
        return
    log = Path(data_dir) / "audit_log.jsonl"
    try:
        append_events(log, [event])
        broken = verify_audit_chain(log)
    except AuditChainError as exc:
        raise OpError(409, str(exc))
    if broken:
        raise OpError(409, f"監査ログ検証に失敗しました: {broken}")


_OPERATION_LOCKS: dict[str, "threading.RLock"] = {}
_OPERATION_LOCKS_GUARD = threading.Lock()
_OPERATION_LOCAL = threading.local()


class _WebhookJournal:
    """Rollback the file side effects of one inbound provider delivery."""

    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.backup_root = Path(tempfile.mkdtemp(prefix="ainote-webhook-operation-"))
        self.entries: list[tuple[Path, Path | None, bool]] = []
        self.events: list[dict] = []

    def track(self, raw_path) -> None:
        path = Path(raw_path).resolve(strict=False)
        auth_root = (self.root.parent / "auth").resolve(strict=False)
        if not path.is_relative_to(self.root) and not path.is_relative_to(auth_root):
            raise OpError(500, "Webhook操作の保存先がデータ領域外です。")
        for original, _backup, _is_dir in self.entries:
            if path == original or path.is_relative_to(original):
                return
        if not path.exists():
            self.entries.append((path, None, False))
            return
        backup = self.backup_root / str(len(self.entries))
        is_dir = path.is_dir()
        if is_dir:
            shutil.copytree(path, backup)
        else:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        self.entries.append((path, backup, is_dir))

    def rollback(self) -> None:
        for path, backup, is_dir in reversed(self.entries):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            if backup is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if is_dir:
                shutil.copytree(backup, path)
            else:
                shutil.copy2(backup, path)

    def close(self) -> None:
        shutil.rmtree(self.backup_root, ignore_errors=True)


def _track_webhook_path(path) -> None:
    journal = getattr(_OPERATION_LOCAL, "webhook_journal", None)
    if journal is not None:
        journal.track(path)


def _commit_webhook_audit(data_dir, journal: _WebhookJournal) -> None:
    if not journal.events:
        return
    log = Path(data_dir) / "audit_log.jsonl"
    try:
        append_events(log, journal.events)
        broken = verify_audit_chain(log)
    except AuditChainError as exc:
        raise OpError(409, str(exc))
    if broken:
        raise OpError(409, f"監査ログ検証に失敗しました: {broken}")


@contextlib.contextmanager
def _operation_lock(data_dir):
    """DBスナップショットから監査完了までを同一data_dirごとに直列化する。"""
    root = Path(data_dir).resolve()
    key = str(root)
    with _OPERATION_LOCKS_GUARD:
        lock = _OPERATION_LOCKS.setdefault(key, threading.RLock())
    with lock:
        root.mkdir(parents=True, exist_ok=True)
        depths = getattr(_OPERATION_LOCAL, "depths", {})
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        _OPERATION_LOCAL.depths = depths
        handle = None
        try:
            if depth == 0:
                handle = (root / ".operation.lock").open("a+")
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


def _snapshot_database(data_dir) -> Path | None:
    """hub.dbの一貫断面を同一ディレクトリへ作る。DB未作成ならNone。"""
    db = Path(data_dir) / "hub.db"
    if not db.is_file():
        return None
    # `.db` はバックアップZIPの明示除外対象。操作中の平文スナップショットを
    # 同時ダウンロードへ混ぜない。
    fd, name = tempfile.mkstemp(prefix=".hub.operation.", suffix=".db", dir=db.parent)
    os.close(fd)
    snapshot = Path(name)
    source = sqlite3.connect(str(db), timeout=30.0)
    target = sqlite3.connect(str(snapshot), timeout=30.0)
    try:
        source.backup(target)
        target.execute("PRAGMA synchronous=FULL")
        target.commit()
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        source.close()
    return snapshot


def _restore_database(data_dir, snapshot: Path | None) -> None:
    """失敗した操作のSQLite変更だけを、操作開始時の断面へ戻す。"""
    db = Path(data_dir) / "hub.db"
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    if snapshot is None:
        db.unlink(missing_ok=True)
        return
    os.replace(snapshot, db)
    try:
        dir_fd = os.open(db.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _check_role(op: str, role: str):
    if role not in OP_ROLES.get(op, set()):
        raise OpError(403, f"この操作({op})は {'・'.join(sorted(OP_ROLES.get(op, set())))} のみ実行できます。")


def _event(actor, action, target, gate_status, **extra):
    e = {
        "event_id": action.upper().replace("_", "-") + "-" + hashlib.sha256(
            f"{target}|{gate_status}|{_now()}".encode("utf-8")).hexdigest()[:16],
        "actor": actor, "action": action, "target": target, "gate_status": gate_status,
        "timestamp": _now(), "source_ref": "operations",
    }
    e.update(extra)
    return e


def case_advance(data_dir, params, actor, role):
    """案件ステージを次段階へ(隣接前進のみ): 反響→内見→申込→契約→管理。"""
    _check_role("case_advance", role)
    cid = (params.get("case_id") or "").strip()
    to = (params.get("to_status") or "").strip()
    if not cid or to not in CASE_STAGES:
        raise OpError(400, "case_id と有効な to_status(反響/内見/申込/契約/管理)が必要です。")
    st = _store(data_dir)
    rows = st.query("cases", "case_id = ?", (cid,))
    if not rows:
        raise OpError(404, f"案件 {cid} が見つかりません(hub.db)。")
    frm = (rows[0].get("status") or "").strip()
    if frm == "失注":
        # 失注はCASE_STAGES外(index=-1)なので to=反響 が隣接前進判定をすり抜けて無音復活する穴を塞ぐ
        raise OpError(409, "失注済みの案件です(ステージ前進不可)。")
    fi = CASE_STAGES.index(frm) if frm in CASE_STAGES else -1
    ti = CASE_STAGES.index(to)
    if ti != fi + 1:
        raise OpError(409, f"{frm or '未設定'}→{to} は隣接前進ではありません(現在の次段階のみ可)。")
    st.update_row("cases", "case_id", cid, {"status": to})
    _audit(data_dir, _event(actor, "case_stage_advanced", cid, to, from_status=frm, to_status=to, case=cid))
    return {"ok": True, "op": "case_advance", "case_id": cid, "from": frm, "to": to, "link": f"/case?id={cid}"}


def task_done(data_dir, params, actor, role):
    """タスクを完了(status=done)にする。"""
    _check_role("task_done", role)
    tid = (params.get("task_id") or "").strip()
    if not tid:
        raise OpError(400, "task_id が必要です。")
    st = _store(data_dir)
    rows = st.query("tasks", "task_id = ?", (tid,))
    if not rows:
        raise OpError(404, f"タスク {tid} が見つかりません。")
    prev = (rows[0].get("status") or "").strip()
    st.update_row("tasks", "task_id", tid, {"status": "done"})
    _audit(data_dir, _event(actor, "task_completed", tid, "done", from_status=prev))
    return {"ok": True, "op": "task_done", "task_id": tid, "from": prev, "to": "done", "link": "/today"}


def task_snooze(data_dir, params, actor, role):
    """タスクを指定日まで一覧から隠す（CAN-09型・可逆）。状態の正本は監査イベント
    （task_snoozed）＝ task_snooze テーブルは派生インデックス（vault が監査リプレイで再構築）。"""
    _check_role("task_snooze", role)
    tid = (params.get("task_id") or "").strip()
    until = (params.get("until") or "").strip()
    reason = (params.get("reason") or "").strip()
    if not tid:
        raise OpError(400, "task_id が必要です。")
    try:
        d = datetime.strptime(until, "%Y-%m-%d").date()
    except ValueError:
        raise OpError(400, f"until は YYYY-MM-DD 形式で: {until!r}")
    today = datetime.now(JST).date()
    if d <= today:
        raise OpError(400, f"until は明日以降の日付で（{until} は過去/当日）。")
    if (d - today).days > 180:
        raise OpError(400, f"スヌーズは最長180日（{until} は {(d - today).days}日先）。"
                           "それ以上先はタスク自体を見直してください。")
    st = _store(data_dir)
    rows = st.query("tasks", "task_id = ?", (tid,))
    if not rows:
        raise OpError(404, f"タスク {tid} が見つかりません。")
    task = rows[0]
    if (task.get("status") or "").strip() == "done":
        raise OpError(409, f"タスク {tid} は完了済み（スヌーズ不要）。")
    # P0・法令系タスクの先送りは責任者/代表のみ＋理由必須（F-c是正／wv敵対R2 B4-snooze是正）。
    # 法令信号は gate(返信ゲート='send')でなく hold_reason/gate/title に入る（privacy_hold等）。
    _sig = " ".join(str(task.get(k) or "") for k in ("hold_reason", "gate", "title")).lower()
    _legal_words = ("privacy", "professional", "個人情報", "専門", "本人確認", "identity",
                    "反社", "aml", "重要事項", "juusetsu", "宅建")
    sensitive = ((task.get("priority") or "") in ("P0", "P1")
                 and any(w in _sig for w in _legal_words)) or (task.get("priority") or "") == "P0"
    if sensitive:
        if role not in ("責任者", "代表"):
            raise OpError(403, "P0・法令系タスクのスヌーズは 責任者/代表 のみ実行できます。")
        if not reason:
            raise OpError(400, "P0・法令系タスクのスヌーズは reason（理由）必須です。")
    now = _now()
    if st.query("task_snooze", "task_id = ?", (tid,)):
        st.update_row("task_snooze", "task_id", tid,
                      {"snooze_until": until, "reason": reason, "actor": actor, "created_at": now})
    else:
        st.insert_row("task_snooze", {"task_id": tid, "snooze_until": until,
                                      "reason": reason, "actor": actor, "created_at": now})
    _audit(data_dir, _event(actor, "task_snoozed", tid, "snoozed", until=until, reason=reason))
    return {"ok": True, "op": "task_snooze", "task_id": tid, "until": until, "link": "/today"}


def task_unsnooze(data_dir, params, actor, role):
    """スヌーズを解除して即座に一覧へ戻す（可逆）。"""
    _check_role("task_unsnooze", role)
    tid = (params.get("task_id") or "").strip()
    if not tid:
        raise OpError(400, "task_id が必要です。")
    st = _store(data_dir)
    if not st.query("task_snooze", "task_id = ?", (tid,)):
        raise OpError(404, f"タスク {tid} はスヌーズされていません。")
    st.delete_row("task_snooze", "task_id", tid)
    _audit(data_dir, _event(actor, "task_unsnoozed", tid, "resurfaced"))
    return {"ok": True, "op": "task_unsnooze", "task_id": tid, "link": "/today"}


def snoozed_task_ids(data_dir, today: str | None = None) -> dict:
    """現在有効なスヌーズ {task_id: until}。until <= 今日 は自然失効（クエリ時フィルタ＝cron不要）。
    hub.db 不在なら空（空dbを作ってCSVフォールバックを毒さない）。"""
    if not (Path(data_dir) / "hub.db").exists():
        return {}
    st = _store(data_dir)
    today = today or datetime.now(JST).date().isoformat()
    try:
        rows = st.query("task_snooze", "snooze_until > ?", (today,))
    except Exception:
        return {}
    return {r["task_id"]: r["snooze_until"] for r in rows}


def approval_decide(data_dir, params, actor, role):
    """承認待ちに決定(approved/rejected)を記録する。"""
    _check_role("approval_decide", role)
    aid = (params.get("approval_id") or "").strip()
    dec = (params.get("decision") or "").strip()
    if not aid or dec not in ("approved", "rejected"):
        raise OpError(400, "approval_id と decision(approved/rejected)が必要です。")
    st = _store(data_dir)
    rows = st.query("approval_queue", "approval_id = ?", (aid,))
    if not rows:
        raise OpError(404, f"承認 {aid} が見つかりません。")
    st.update_row("approval_queue", "approval_id", aid, {"decision": dec})
    _audit(data_dir, _event(actor, "approval_decided", aid, dec, decision=dec))
    return {"ok": True, "op": "approval_decide", "approval_id": aid, "decision": dec, "link": "/approval"}


def hold_release(data_dir, params, actor, role):
    """保留を解除(gate=cleared)する。"""
    _check_role("hold_release", role)
    hid = (params.get("hold_id") or "").strip()
    if not hid:
        raise OpError(400, "hold_id が必要です。")
    st = _store(data_dir)
    rows = st.query("hold_queue", "hold_id = ?", (hid,))
    if not rows:
        raise OpError(404, f"保留 {hid} が見つかりません。")
    st.update_row("hold_queue", "hold_id", hid, {"gate": "cleared"})
    _audit(data_dir, _event(actor, "hold_released", hid, "cleared"))
    return {"ok": True, "op": "hold_release", "hold_id": hid, "link": "/hold"}


def lead_convert(data_dir, params, actor, role):
    """反響(portal_lead)を顧客+案件へ変換する(可逆・新規作成)。業務動線の起点。"""
    _check_role("lead_convert", role)
    lid = (params.get("portal_lead_id") or "").strip()
    if not lid:
        raise OpError(400, "portal_lead_id が必要です。")
    st = _store(data_dir)
    leads = st.query("portal_leads", "portal_lead_id = ?", (lid,))
    if not leads:
        raise OpError(404, f"反響 {lid} が見つかりません(hub.db)。")
    lead = leads[0]
    if st.query("cases", "source_ref = ?", (lid,)):  # 二重変換ガード
        raise OpError(409, f"反響 {lid} は既に変換済みです。")
    h = hashlib.sha256(lid.encode("utf-8")).hexdigest()[:8].upper()
    cust_id, case_id = "CUST-" + h, "CASE-" + h
    name = lead.get("customer_name") or ""
    st.insert_row("customers", _full_row("customers", {
        "customer_id": cust_id, "customer_name": name, "contact": lead.get("customer_contact") or "",
        "status": "新規", "source_ref": lid, "source_tool": "lead_convert"}))
    st.insert_row("cases", _full_row("cases", {
        "case_id": case_id, "customer_id": cust_id, "customer_name": name,
        "property_id": lead.get("property_ref") or "", "deal_type": lead.get("inquiry_type") or "",
        "status": "反響", "source_ref": lid, "source_tool": "lead_convert"}))
    _audit(data_dir, _event(actor, "lead_converted", lid, "反響",
                            customer=cust_id, case=case_id))
    return {"ok": True, "op": "lead_convert", "portal_lead_id": lid,
            "customer_id": cust_id, "case_id": case_id, "link": f"/case?id={case_id}"}


def customer_case_create(data_dir, params, actor, role):
    """既存のお客様に新しい案件を紐づける（リピート取引の起点）。"""
    _check_role("customer_case_create", role)
    customer_id = (params.get("customer_id") or "").strip()
    if not customer_id:
        raise OpError(400, "customer_id が必要です。")
    st = _store(data_dir)
    customer = _require_customer(st, customer_id)

    now = _now()
    nonce = os.urandom(8).hex()
    case_id = "CASE-" + hashlib.sha256(
        f"{customer_id}|{now}|{nonce}".encode("utf-8")
    ).hexdigest()[:12].upper()
    deal_type = normalize_deal_type(params.get("deal_type") or "")
    property_name = (params.get("property_name") or "").strip()
    property_id = (params.get("property_id") or "").strip()
    if property_id:
        properties = st.query("properties", "property_id = ?", (property_id,))
        if not properties:
            raise OpError(404, f"物件 {property_id} が見つかりません。")
        # 登録済み物件との接続では、ブラウザから渡された表示名を正本にしない。
        # propertiesには名称列が無いため、同じproperty_idを持つ既存案件の名称を優先し、
        # 無ければ所在地を人が読める名称として使う。
        linked_cases = st.query("cases", "property_id = ?", (property_id,))
        property_name = next(
            (str(row.get("property_name") or "").strip()
             for row in linked_cases if str(row.get("property_name") or "").strip()),
            str(properties[0].get("address") or "").strip(),
        )
    customer_name = (customer.get("customer_name") or "").strip()
    st.insert_row("cases", _full_row("cases", {
        "case_id": case_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "property_id": property_id,
        "property_name": property_name,
        "deal_type": deal_type,
        "status": "反響",
        "source_ref": f"customer:{customer_id}",
        "source_tool": "customer_case_create",
    }))

    first_stage = stages_for(deal_type)[0]
    due = (datetime.now(JST) + timedelta(
        days=STAGE_DUE_DAYS.get(first_stage, 3))).date().isoformat()
    track = ("buyer" if deal_type == "sale_buyer" else
             "seller" if deal_type == "sale_seller" else "tenant")
    journey_id = "JR-" + hashlib.sha256(
        f"{case_id}|{now}".encode("utf-8")
    ).hexdigest()[:12].upper()
    st.insert_row("customer_journey", _full_row("customer_journey", {
        "journey_id": journey_id,
        "case_id": case_id,
        "deal_track": track,
        "stage": first_stage,
        "entered_at": now,
        "due_at": due,
    }))
    _audit(data_dir, _event(
        actor, "customer_case_created", case_id, "反響",
        customer=customer_id, case=case_id, deal_type=deal_type,
    ))
    return {
        "ok": True,
        "op": "customer_case_create",
        "customer_id": customer_id,
        "case_id": case_id,
        "link": f"/case?id={case_id}",
    }


def lead_quick_add(data_dir, params, actor, role):
    """反響の手動クイック登録（電話・紹介・店頭。M-inbox第2経路・可逆）。
    復元の正本=監査イベント lead_quick_added（vault再構築が inbox.replay でリプレイ）。"""
    _check_role("lead_quick_add", role)
    name = (params.get("customer_name") or "").strip()
    contact = (params.get("contact") or "").strip()
    channel = (params.get("channel") or "電話").strip()
    if not name:
        raise OpError(400, "customer_name（お客様名）が必要です。")
    from hub_core import inbox as _inbox
    st = _store(data_dir)
    now = _now()
    lid = "L-MAN-" + hashlib.sha256(f"{name}|{contact}|{now}".encode("utf-8")).hexdigest()[:10].upper()
    lead = {"portal_lead_id": lid, "platform_id": channel, "source_method": "manual",
            "received_at": now, "customer_name": name, "customer_contact": contact,
            "property_ref": (params.get("property_ref") or "").strip(),
            "inquiry_type": (params.get("inquiry_type") or "").strip(),
            "consent_status": "", "reply_gate": "pending", "hold_reason": "",
            "raw_ref": "manual:" + actor}
    st.insert_row("portal_leads", _full_row("portal_leads", lead))
    task = _inbox._first_reply_task(lead)
    st.insert_row("tasks", _full_row("tasks", task))
    _audit(data_dir, _event(actor, "lead_quick_added", lid, "pending", lead=lead))
    return {"ok": True, "op": "lead_quick_add", "portal_lead_id": lid,
            "task_id": task["task_id"], "link": "/leads"}


def inbox_ingest(data_dir, params, actor, role):
    """反響/フォルダの .eml を台帳へ取込（冪等・M-inbox第1経路）。"""
    _check_role("inbox_ingest", role)
    from hub_core import inbox as _inbox
    res = _inbox.ingest_inbox(data_dir, actor=actor)
    return {"ok": True, "op": "inbox_ingest", **res, "link": "/leads"}


def esign_create(data_dir, params, actor, role):
    """電子契約の封筒を作成（BYO・監査ESIGN是正=死蔵していたesign.pyを配線）。実送信しない。"""
    _check_role("esign_create", role)
    from hub_core import esign as _es
    eid = (params.get("envelope_id") or "").strip()
    title = (params.get("title") or "").strip()
    signers = params.get("signers") or []
    if isinstance(signers, str):
        signers = [{"name": s.strip(), "email": ""} for s in signers.split(",") if s.strip()]
    try:
        env = _es.new_envelope(eid, title, signers)
    except _es.EsignError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "esign_created", eid, "created", title=title,
                            signers=[s.get("name") for s in env["signers"]]))
    return {"ok": True, "op": "esign_create", "envelope_id": eid, "status": env["status"]}


def esign_send(data_dir, params, actor, role):
    """封筒を送信（BYO provider・未接続時はモック=実送信しない・監査）。"""
    _check_role("esign_send", role)
    from hub_core import esign as _es
    eid = (params.get("envelope_id") or "").strip()
    title = (params.get("title") or "").strip()
    signers = params.get("signers") or []
    if isinstance(signers, str):
        signers = [{"name": s.strip(), "email": ""} for s in signers.split(",") if s.strip()]
    try:
        env = _es.new_envelope(eid or "ENV", title or "契約書", signers or [{"name": "署名者", "email": ""}])
        # BYO実プロバイダは env 設定から（未設定=Mock=実送信しない・接続時のみ明示承認で実送信）
        prov, allow = None, False
        try:
            from hub_core import esign_provider as _ep
            prov = _ep.build_esign_provider(data_dir)
            allow = _ep.esign_connected()
        except Exception:
            prov = None
        _es.transition(env, "sent", provider=prov, allow_real_send=allow)
    except _es.EsignError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "esign_sent", eid, "sent",
                            provider=env.get("provider"), delivered=env.get("delivered", False)))
    return {"ok": True, "op": "esign_send", "envelope_id": eid, "status": env["status"],
            "delivered": env.get("delivered", False),
            "note": "未接続時はモック=実送信していません（実プロバイダ接続は人間ゲート）。"}


def _load_application(data_dir, app_id: str) -> dict | None:
    """申込の最新状態を監査イベント(application_state)のリプレイで得る（正本=audit）。"""
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    if not ap.is_file():
        return None
    latest = None
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if ev.get("action") == "application_state" and ev.get("target") == app_id:
            st = ev.get("app_state")
            if isinstance(st, dict):
                latest = st
    return latest


def applications_for_case(data_dir, case_id: str) -> list:
    """案件に紐づく申込の最新状態リスト（監査リプレイ・表示用）。"""
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    if not ap.is_file() or not case_id:
        return []
    by_id = {}
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if ev.get("action") == "application_state":
            st = ev.get("app_state")
            if isinstance(st, dict) and st.get("case_id") == case_id:
                by_id[st.get("application_id")] = st
    return list(by_id.values())


def application_create(data_dir, params, actor, role):
    """入居申込を受け付ける（契約クローズ後半の入口・可逆）。状態は監査イベントで永続。"""
    _check_role("application_create", role)
    from hub_core import screening as _scr
    _rent_raw = str(params.get("monthly_rent") or "0").strip()
    try:
        _rent = int(_rent_raw) if _rent_raw else 0   # try外の安全パース（非数値→400・500面回避 F3是正）
    except ValueError:
        raise OpError(400, f"monthly_rent は数値で指定してください: {_rent_raw!r}")
    try:
        app = _scr.new_application(
            (params.get("application_id") or "").strip(),
            (params.get("applicant") or "").strip(),
            (params.get("property_ref") or "").strip(),
            monthly_rent=_rent,
            case_id=(params.get("case_id") or "").strip())
    except _scr.ScreeningError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "application_state", app["application_id"], app["status"],
                            app_state=app))
    return {"ok": True, "op": "application_create", "application_id": app["application_id"],
            "status": app["status"], "link": "/case?id=" + (app.get("case_id") or "")}


def application_advance(data_dir, params, actor, role):
    """申込ステートを進める（received→docs_ok 等・可逆）。承認/否認は screening_result で。"""
    _check_role("application_advance", role)
    from hub_core import screening as _scr
    aid = (params.get("application_id") or "").strip()
    to = (params.get("to") or "").strip()
    app = _load_application(data_dir, aid)
    if app is None:
        raise OpError(404, f"申込 {aid} が見つかりません。")
    try:
        if to == "screening":
            _scr.request_screening(app)   # 既定Mock（実審査しない）
        else:
            _scr.advance(app, to)
    except _scr.ScreeningError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "application_state", aid, app["status"], app_state=app))
    return {"ok": True, "op": "application_advance", "application_id": aid, "status": app["status"]}


def _guarantor_connected() -> bool:
    """実保証会社が接続されているか＝**サーバ側の配備事実**（env）。既定False＝未接続。
    リクエストの自己申告では決してTrueにできないようにするのがHIGH是正の核。
    実アダプタ採用時に RI_HUB_GUARANTOR_CONNECTED=1 を人間が設定して初めてTrue。"""
    return os.environ.get("RI_HUB_GUARANTOR_CONNECTED", "").strip() in ("1", "true", "yes")


def screening_result(data_dir, params, actor, role):
    """保証会社の審査結果を反映。**実接続の保証会社でなければ確定できない**(fail-closed)。
    connected はリクエストでなく**サーバ側の配備事実**（_guarantor_connected）から決める＝
    宅建士等が connected=true を注入しても未接続のまま承認確定できない（HIGH是正）。"""
    _check_role("screening_result", role)
    from hub_core import screening as _scr
    aid = (params.get("application_id") or "").strip()
    decision = (params.get("decision") or "").strip()
    connected = _guarantor_connected()   # params.get("connected") は信頼しない
    app = _load_application(data_dir, aid)
    if app is None:
        raise OpError(404, f"申込 {aid} が見つかりません。")
    try:
        _scr.apply_screening_result(app, decision, connected=connected)
    except _scr.ScreeningError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "application_state", aid, app["status"], app_state=app))
    return {"ok": True, "op": "screening_result", "application_id": aid, "status": app["status"],
            "contract_ready": _scr.is_contract_ready(app)}


def extraction_save(data_dir, params, actor, role):
    """書類抽出のdraft保存（AIクライアント/手動の共通入口・可逆）。出典（page必須）を強制。"""
    _check_role("extraction_save", role)
    from hub_core import extract as _ex
    try:
        res = _ex.save_extraction(data_dir, (params.get("source") or "").strip(),
                                  params.get("fields"), actor,
                                  extractor=(params.get("extractor") or "manual"))
    except _ex.ExtractError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "extraction_saved", params.get("source") or "", "draft",
                            fields=res["fields"], extractor=(params.get("extractor") or "manual")))
    return {"ok": True, "op": "extraction_save", **res}


def ocr_extract(data_dir, params, actor, role):
    """原本をBYO-LLM visionで読取→extract出典束縛へdraft保存（G2）。実読取はBYO vision・未接続は手動へ。
    OCRはドラフト補助＝値は出典束縛と宅建士承認を通る（勝手に台帳へ流さない）。"""
    _check_role("ocr_extract", role)
    from hub_core import ocr as _ocr
    # 実visionプロバイダはBYO設定から（未設定=MockVision＝正直に手動へ）。
    prov = None
    try:
        from hub_core import ocr_provider as _op  # 任意: 実vision実装があれば
        prov = _op.build_vision_provider(data_dir)
    except Exception:
        prov = None
    try:
        res = _ocr.ocr_document(data_dir, (params.get("source") or "").strip(), actor, provider=prov)
    except _ocr.OcrError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "ocr_extracted", params.get("source") or "", "draft",
                            fields=res.get("fields", 0), connected=res.get("connected", False)))
    return {"ok": True, "op": "ocr_extract", **res}


def extraction_approve(data_dir, params, actor, role):
    """抽出値の人間承認（宅建士/責任者/代表）。原本sha256一致が前提（改竄=fail-closed）。"""
    _check_role("extraction_approve", role)
    from hub_core import extract as _ex
    try:
        res = _ex.approve_extraction(data_dir, (params.get("source") or "").strip(), actor)
    except _ex.ExtractError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "extraction_approved", params.get("source") or "", "approved",
                            fields=res["fields"]))
    return {"ok": True, "op": "extraction_approve", **res}


def permission_record(data_dir, params, actor, role):
    """帯替え許諾の台帳記録（根拠sha256束縛・HMAC監査・可逆=再記録可）。"""
    _check_role("permission_record", role)
    from hub_core import obi as _obi
    try:
        rec = _obi.record_permission(
            data_dir, (params.get("property") or "").strip(),
            (params.get("source_company") or "").strip(),
            params.get("permitted") or [], actor,
            evidence_rel=(params.get("evidence") or "").strip())
    except _obi.ObiError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "obi_permission_recorded", rec["property"], "recorded",
                            source_company=rec["source_company"], permitted=rec["permitted"],
                            record_hash=rec["record_hash"]))
    return {"ok": True, "op": "permission_record", **{k: rec[k] for k in ("property", "permitted")}}


def obi_swap(data_dir, params, actor, role):
    """帯替え出力（許諾ゲート必須=無許諾は拒否・HMAC監査）。v0対象=xlsx。"""
    _check_role("obi_swap", role)
    from hub_core import obi as _obi
    # 会社情報の正本は「業者情報」画面で保存した company.json。一度入れれば帯・重説・LINE まで
    # 同じ値が回るため、セットアップ後の出力を一貫させられる。
    # 個別指定（params）は元付ごとの例外用にだけ上書きを許す。
    from hub_core.auth import load_company as _load_company
    _prof = _load_company(data_dir, strict=True) or {}
    company = {
        "company_name": str(_prof.get("name") or _prof.get("company_name") or "").strip(),
        "license": str(_prof.get("license_no") or _prof.get("license") or "").strip(),
        "tel": str(_prof.get("tel") or "").strip(),
        "email": str(_prof.get("email") or "").strip(),
    }
    company.update({k: (params.get(k) or "").strip()
                    for k in ("company_name", "license", "tel", "email") if params.get(k)})
    company = {k: v for k, v in company.items() if v}
    try:
        res = _obi.swap_obi_xlsx(
            data_dir, (params.get("property") or "").strip(),
            (params.get("source") or "").strip(), actor,
            company=company or None, clear_rows=(params.get("clear_rows") or "").strip())
    except _obi.ObiError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "obi_swapped", res["out"], "generated",
                            source_company=res["source_company"], version=res["version"]))
    return {"ok": True, "op": "obi_swap", **res, "link": "/maisoku"}


def renewal_generate(data_dir, params, actor, role):
    """更新期限が近い契約に更新案内ドラフトを自動生成（Tier2・ドラフト止まり=可逆・実送信しない）。
    today と within_days は params から（決定的）。生成ドラフトは M-sender の送信ゲート（人間）を通る。"""
    _check_role("renewal_generate", role)
    from hub_core import contract as _ct
    import datetime as _dt
    today = (params.get("today") or _dt.date.today().isoformat()).strip()
    try:
        _dt.date.fromisoformat(today)
    except ValueError:
        raise OpError(400, "today は YYYY-MM-DD 形式で入力してください。")
    try:
        within = int(params.get("within_days") or 60)
    except (ValueError, TypeError):
        raise OpError(400, "within_days は整数で入力してください。")
    if within < 0:
        raise OpError(400, "within_days は0以上で入力してください。")
    st = _store(data_dir)
    try:
        rows = st.query("contract_register")
    except Exception:
        rows = []
    due = _ct.expiring_contracts(rows, today=today, within_days=within)
    created = []
    for c in due[:50]:
        note = _ct.renewal_note(c)
        r = message_draft(data_dir, {"to": c.get("case_id") or "", "subject": "契約更新のご案内",
                                     "body": note, "channel": "email", "ref": c.get("contract_id") or ""},
                          actor, role)
        created.append({"contract_id": c.get("contract_id"), "message_id": r["message_id"],
                        "days_left": c.get("days_left")})
    return {"ok": True, "op": "renewal_generate", "due": len(due), "drafted": len(created),
            "drafts": created, "note": "更新案内ドラフトを生成しました（可逆）。送信は送信ゲート（人間確認）を通ります。"}


def contract_create(data_dir, params, actor, role):
    """契約を契約台帳に登録（Tier2・賃貸借/保証/火災保険等の更新期限管理）。可逆。"""
    _check_role("contract_create", role)
    from hub_core import contract as _ct
    cid = (params.get("case_id") or "").strip()
    ctype = (params.get("contract_type") or "").strip()
    end = (params.get("end_date") or "").strip()
    start = (params.get("start_date") or "").strip()
    # 賃貸借は終了日未指定なら開始日＋2年を既定に（普通借家の一般的な期間・手入力を減らす）。
    if not end and start and ("賃貸借" in ctype):
        try:
            import datetime as _d
            sd = _d.date.fromisoformat(start)
            end = sd.replace(year=sd.year + 2).isoformat()
        except (ValueError, TypeError):
            pass
    if ctype not in _ct.CONTRACT_TYPES:
        raise OpError(400, f"contract_type は {'/'.join(_ct.CONTRACT_TYPES)} のいずれかです。")
    if not end:
        raise OpError(400, "end_date(終了日 YYYY-MM-DD)が必要です。")
    st = _store(data_dir)
    prop = ""
    if cid:
        cases = st.query("cases", "case_id = ?", (cid,))
        prop = cases[0].get("property_name") if cases else ""
    ctid = "CTR-" + hashlib.sha256(f"{cid}|{ctype}|{end}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    row = {"contract_id": ctid, "case_id": cid, "property_name": prop or params.get("property_name") or "",
           "contract_type": ctype, "counterparty": params.get("counterparty") or "",
           "start_date": params.get("start_date") or "", "end_date": end,
           "auto_renewal": params.get("auto_renewal") or "", "status": "有効",
           "created_at": _now(), "source_ref": cid, "source_tool": "contract_create"}
    st.insert_row("contract_register", _full_row("contract_register", row))
    # 変更は apply_operation の記録→entity_state で再構築耐性を持つ。
    _audit(data_dir, _event(actor, "contract_created", ctid, "registered", type=ctype, end=end))
    return {"ok": True, "op": "contract_create", "contract_id": ctid, "end_date": end}


def activity_report(data_dir, params, actor, role):
    """売主活動報告書を生成（Tier2・専任媒介の法定報告34条の2）。数値は台帳実績のみ・生成のみ。
    売主への送付は M-sender の人間ゲートを通る。period_from/to は呼出側が渡す。"""
    _check_role("activity_report", role)
    from hub_core import activity_report as _ar
    cid = (params.get("case_id") or "").strip()
    pf = (params.get("period_from") or "").strip()
    pt = (params.get("period_to") or "").strip()
    if not cid or not pf or not pt:
        raise OpError(400, "case_id・period_from・period_to(YYYY-MM-DD)が必要です。")
    try:
        rep = _ar.build_activity_report(data_dir, cid, period_from=pf, period_to=pt,
                                        mediation_type=(params.get("mediation_type") or "専任媒介"))
    except ValueError as e:
        raise OpError(404, str(e))
    _audit(data_dir, _event(actor, "activity_report_built", cid, "report",
                            period=f"{pf}〜{pt}", contacts=rep["counts"]["contacts"]))
    return {"ok": True, "op": "activity_report", "case_id": cid, "counts": rep["counts"],
            "text": _ar.render_report_text(rep)}


def _load_it_session(data_dir, sid: str) -> dict | None:
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    if not ap.is_file():
        return None
    latest = None
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if ev.get("action") == "it_session" and ev.get("target") == sid:
            st = ev.get("it_state")
            if isinstance(st, dict):
                latest = st
    return latest


def _iter_audit(data_dir):
    """audit_log.jsonl のイベントを行順に yield（壊れた行はスキップ）。読取専用。"""
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    if not ap.is_file():
        return
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def _latest_juusetsu_consent(data_dir, case_id: str, recipient: str) -> dict | None:
    """当該案件×相手方の電磁的交付の承諾記録（最新・可逆＝再記録で上書き）。無ければ None。"""
    cid = (case_id or "").strip()
    rcp = (recipient or "").strip()
    latest = None
    for ev in _iter_audit(data_dir):
        if (ev.get("action") == "juusetsu_consent" and ev.get("target") == cid
                and (ev.get("recipient") or "") == rcp):
            latest = ev
    return latest


def _finalized_hash_exists(data_dir, content_sha256: str) -> bool:
    """指定 content_sha256 で記名確定（finalized_with_signature）されたイベントが実在するか。
    交付する版の本文ハッシュと finalize の content_hash 突合＝改変検知の一次証跡。"""
    h = (content_sha256 or "").strip()
    if not h:
        return False
    for ev in _iter_audit(data_dir):
        if ev.get("action") == "finalized_with_signature" and ev.get("content_hash") == h:
            return True
    return False


def _finalized_version_exists(data_dir, doc_id: str, version: int,
                              content_sha256: str, case_id: str = "") -> bool:
    """記名確定を doc_id・版・本文hash・案件へ束縛して照合する。

    hashだけの照合では、同じ本文を別doc_idへ複製した未確定書類が、他書類の
    記名確定を流用して交付できる。案件もHMAC監査イベント側と一致させる。
    """
    did = (doc_id or "").strip()
    digest = (content_sha256 or "").strip()
    cid = (case_id or "").strip()
    if not did or not digest or int(version or 0) < 1:
        return False
    try:
        from hub_core import documents as _docs
        evidence = _docs.require_finalized_version(
            data_dir, did, int(version), require_case=bool(cid))
    except _docs.DocError:
        return False
    return (evidence["content_sha256"] == digest
            and (not cid or evidence["case_id"] == cid))


def _latest_juusetsu_delivery(data_dir, case_id: str) -> dict | None:
    """当該案件の重説交付証跡（juusetsu_delivered・最新）。IT重説『事前送付』要件の一次証跡。"""
    cid = (case_id or "").strip()
    latest = None
    for ev in _iter_audit(data_dir):
        if ev.get("action") == "juusetsu_delivered" and ev.get("target") == cid:
            latest = ev
    return latest


def _latest_it_conduct(data_dir, session_id: str) -> dict | None:
    """当該IT重説セッションの実施記録（it_juusetsu_conducted・最新）。M6の実施証跡。"""
    sid = (session_id or "").strip()
    latest = None
    for ev in _iter_audit(data_dir):
        if ev.get("action") == "it_juusetsu_conducted" and ev.get("target") == sid:
            latest = ev
    return latest


def _it_autowire_prefill(data_dir, session: dict) -> dict:
    """要件2「重要事項説明書の事前送付」を、同一案件の交付証跡（juusetsu_deliver）から自動充足する。
    他3要件（宅建士証提示・双方向性・環境確認）は実施時に宅建士が視認して手動チェックする（機械化不能）。
    交付証跡が実在する時のみ met=True にし、False へは倒さない（手動充足の後方互換を壊さない）。"""
    from hub_core import it_juusetsu as _it
    req2 = _it.IT_REQUIREMENTS[1]   # 「重要事項説明書の事前送付（説明前に相手方へ交付済み）」
    reqs = session.get("requirements") or {}
    if _latest_juusetsu_delivery(data_dir, session.get("case_id") or "") is not None:
        reqs[req2] = True
    session["requirements"] = reqs
    return session


def it_session_create(data_dir, params, actor, role):
    """IT重説セッションを作成（Tier2磨き・BYO映像URL・実施は法定要件充足ゲート）。可逆。
    実際の映像会議は外部(Zoom/Meet等・BYO)＝あいのては日程・要件チェック・記録のみ。"""
    _check_role("it_session_create", role)
    from hub_core import it_juusetsu as _it
    import hashlib as _h
    cid = (params.get("case_id") or "").strip()
    sid = "ITS-" + _h.sha256(f"{cid}|{_now()}".encode("utf-8")).hexdigest()[:12].upper()
    try:
        s = _it.new_session(sid, cid, scheduled_at=(params.get("scheduled_at") or "").strip(),
                            video_url=(params.get("video_url") or "").strip())
    except _it.ItJuusetsuError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "it_session", sid, "予約", it_state=s))
    return {"ok": True, "op": "it_session_create", "session_id": sid, "state": "予約"}


def _case_for_line_friend(data_dir, to_user: str, name: str, actor: str) -> tuple:
    """LINEの友だち（会話）に対応する案件を返す。無ければ**顧客＋案件を自動作成**する。
    起点は顧客との会話＝ユーザーに案件IDを打たせない設計。分かる範囲のみ埋める（表示名だけ・物件は空＝捏造しない）。
    冪等: source_ref = friend UUID で一意（二度呼んでも増えない）。返り (case_id, customer_id, created)。"""
    st = _store(data_dir)
    cases = st.query("cases", "source_ref = ?", (to_user,))
    if cases:
        c = cases[0]
        return (c.get("case_id") or "", c.get("customer_id") or "", False)
    h = hashlib.sha256(("line:" + to_user).encode("utf-8")).hexdigest()[:8].upper()
    cust_id, case_id = "CUST-" + h, "CASE-" + h
    st.insert_row("customers", _full_row("customers", {
        "customer_id": cust_id, "customer_name": name, "contact": "",
        "status": "新規", "source_ref": to_user, "source_tool": "line"}))
    st.insert_row("cases", _full_row("cases", {
        "case_id": case_id, "customer_id": cust_id, "customer_name": name,
        "property_id": "", "deal_type": "", "status": "反響",
        "source_ref": to_user, "source_tool": "line"}))
    _audit(data_dir, _event(actor, "line_case_created", to_user, "反響",
                            customer=cust_id, case=case_id, name=name))
    return (case_id, cust_id, True)


def line_start_it_juusetsu(data_dir, params, actor, role):
    """会話ファースト: LINEの会話（友だち）から**IT重説を1クリックで開始**する。案件が無ければその顧客
    （友だち）から顧客＋案件を自動作成し、その案件のIT重説セッションを用意して /it へ橋渡しする。
    ユーザーに案件IDを打たせない（起点は会話）。可逆（下書き相当）。
    冪等: 案件は friend UUID（source_ref）で一意＝二度押しても増えない。IT重説セッションも同一案件に
    既存があれば再利用する（重複作成しない）。"""
    _check_role("it_session_create", role)   # IT重説セッション作成と同じ役割ゲート
    from hub_core import it_juusetsu as _it
    to_user = (params.get("to_user") or "").strip()
    name = (params.get("display_name") or "").strip()
    if not to_user:
        raise OpError(400, "to_user（友だちのUUID）が必要です。")
    case_id, cust_id, created = _case_for_line_friend(data_dir, to_user, name, actor)
    # この案件のIT重説セッションを用意（既存があれば再利用＝重複作成しない・冪等）。
    existing_sid = ""
    for ev in _iter_audit(data_dir):
        if ev.get("action") == "it_session" and isinstance(ev.get("it_state"), dict):
            if (ev["it_state"].get("case_id") or "") == case_id:
                existing_sid = ev.get("target") or ev["it_state"].get("session_id") or existing_sid
    if existing_sid:
        sid, reused = existing_sid, True
    else:
        sid = "ITS-" + hashlib.sha256(f"{case_id}|{_now()}".encode("utf-8")).hexdigest()[:12].upper()
        s = _it.new_session(sid, case_id)
        _audit(data_dir, _event(actor, "it_session", sid, "予約", it_state=s))
        reused = False
    return {"ok": True, "op": "line_start_it_juusetsu", "case_id": case_id,
            "customer_id": cust_id, "session_id": sid, "case_created": created,
            "session_reused": reused, "link": "/it"}


def it_check_requirement(data_dir, params, actor, role):
    """IT重説の法定要件の充足を記録（宅建士）。全要件充足まで実施可にできない。"""
    _check_role("it_check_requirement", role)
    from hub_core import it_juusetsu as _it
    sid = (params.get("session_id") or "").strip()
    s = _load_it_session(data_dir, sid)
    if s is None:
        raise OpError(404, f"セッション {sid} が見つかりません。")
    try:
        _it.check_requirement(s, (params.get("requirement") or "").strip(),
                              str(params.get("met") or "true").lower() in ("1", "true", "はい", "yes"))
    except _it.ItJuusetsuError as e:
        raise OpError(e.code, e.msg)
    _it_autowire_prefill(data_dir, s)   # 要件2は交付証跡から自動充足（新規D結線）
    _audit(data_dir, _event(actor, "it_session", sid, s["state"], it_state=s))
    return {"ok": True, "op": "it_check_requirement", "session_id": sid,
            "all_met": _it.all_requirements_met(s)}


def it_advance(data_dir, params, actor, role):
    """IT重説セッションを次状態へ（宅建士・実施可は法定要件全充足ゲート・実施済は記録）。"""
    _check_role("it_advance", role)
    from hub_core import it_juusetsu as _it
    sid = (params.get("session_id") or "").strip()
    to_state = (params.get("to_state") or "").strip()
    s = _load_it_session(data_dir, sid)
    if s is None:
        raise OpError(404, f"セッション {sid} が見つかりません。")
    # 実施系（実施可/実施済）は運用開始ゲート（免許/宅建士登録/GL確認）を fail-closed で要求する（設計§7）。
    # company.json 不在＝dev/ライブラリ文脈（auth_required と同じ「非設定=dev」思想）は素通し（回帰保護）。
    if to_state in ("実施可", "実施済"):
        from hub_core.auth import is_configured as _is_conf, load_company as _lc
        if _is_conf(data_dir):
            gate = _it.operational_gate_status(_lc(data_dir))
            if not gate["ready"]:
                raise OpError(403, "IT重説の運用開始ゲートが未充足です（練習モード）。次を会社設定に登録してください: "
                                   + "・".join(gate["missing"]))
    _it_autowire_prefill(data_dir, s)   # 要件2は交付証跡から自動充足（実施可ゲートは証跡ベースで機械判定）
    try:
        _it.transition(s, to_state)
    except _it.ItJuusetsuError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "it_session", sid, s["state"], it_state=s))
    # M6 実施記録: 実施済へ遷移したら「誰が/いつ/どの doc hash を/どの video_url で説明したか」を audit へ確定。
    # doc hash は当該案件の交付証跡（＝説明した確定版重説）の content_sha256 に束縛する。
    if s["state"] == "実施済":
        delivery = _latest_juusetsu_delivery(data_dir, s.get("case_id") or "") or {}
        _audit(data_dir, _event(actor, "it_juusetsu_conducted", sid, "実施済",
                                case=s.get("case_id") or "", scheduled_at=s.get("scheduled_at") or "",
                                video_url=s.get("video_url") or "", takkenshi=actor,
                                doc_id=delivery.get("doc_id") or "",
                                content_hash=delivery.get("content_sha256") or ""))
    return {"ok": True, "op": "it_advance", "session_id": sid, "state": s["state"]}


def it_schedule_confirm(data_dir, params, actor, role):
    """IT重説の日時（＋任意でBYO映像URL）を確定し、確定通知＆映像URLを送信ゲート（queued）へ積む（設計M5）。
    候補日時は別途 line_send（queued）で送付済み・相手方が選んだ結果を担当がここで確定する想定。
    実送信はしない（line_confirm_send の人間ゲートを通る）。可逆。"""
    _check_role("it_schedule_confirm", role)
    from hub_core import it_juusetsu as _it
    sid = (params.get("session_id") or "").strip()
    scheduled_at = (params.get("scheduled_at") or "").strip()
    video_url = (params.get("video_url") or "").strip()
    to_user = (params.get("to_user") or "").strip()
    if not scheduled_at:
        raise OpError(400, "scheduled_at（確定する日時）が必要です。")
    s = _load_it_session(data_dir, sid)
    if s is None:
        raise OpError(404, f"セッション {sid} が見つかりません。")
    if (s.get("state") or "") in ("実施済", "中止"):
        raise OpError(409, "実施済・中止のセッションは日時変更できません。")
    s["scheduled_at"] = scheduled_at
    if video_url:
        s["video_url"] = video_url
    _audit(data_dir, _event(actor, "it_session", sid, s["state"], it_state=s))
    msg_id = ""
    if to_user:   # 確定通知＋BYO映像URL（あれば）を outbox へ（queued＝実送信しない）
        vurl = s.get("video_url") or ""
        text = (f"IT重説の日時が確定しました: {scheduled_at.replace('T', ' ')}。"
                + (f" 当日はこちらの映像URLからご参加ください: {vurl}" if vurl else ""))
        notify = apply_operation(data_dir, "line_send",
                                 {"to_user": to_user, "kind": "push", "text": text}, actor, role)
        msg_id = notify.get("msg_id") or ""
    return {"ok": True, "op": "it_schedule_confirm", "session_id": sid,
            "scheduled_at": scheduled_at, "video_url": s.get("video_url") or "",
            "msg_id": msg_id, "status": "queued" if to_user else "saved"}


def it_gate_set(data_dir, params, actor, role):
    """IT重説の運用開始ゲート（免許番号・宅建士登録番号・現行GL確認）を会社設定へ登録する（設計§7）。
    3点が揃うと it_advance→実施可 が解錠（練習モード→本番モード）。会社設定＝責任者/代表のみ。可逆。"""
    _check_role("it_gate_set", role)
    from hub_core import it_juusetsu as _it
    from hub_core import branding as _br
    from hub_core.auth import load_company as _lc, is_configured as _is_conf
    if not _is_conf(data_dir):
        raise OpError(409, "会社設定（company.json）が未作成です。先に初期設定を済ませてください。")
    company = dict(_lc(data_dir))
    lic = (params.get("license_no") or "").strip()
    reg = (params.get("takkenshi_reg") or "").strip()
    confirmed = str(params.get("guideline_confirmed") or "").lower() in ("1", "true", "はい", "yes", "on")
    if lic:
        company["license_no"] = lic
    if reg:
        company["takkenshi_reg"] = reg
    if confirmed and not str(company.get("it_guideline_confirmed_at") or "").strip():
        company["it_guideline_confirmed_at"] = _now()   # 確認日時はサーバ側で確定（捏造しない）
    # 免許番号・宅建士登録番号は法定表示。直書きせず版を積む（戻せる・誰がいつ変えたか残る）。
    try:
        _br.save(data_dir, company, actor, source="it_gate_set")
    except _br.BrandError as exc:
        raise OpError(400, exc.msg)
    # it_guideline_confirmed_at はブランド項目でないので正本へ直接反映してから読み直す
    from hub_core.auth import save_company as _sc
    _sc(data_dir, {**_lc(data_dir), **company})
    gate = _it.operational_gate_status(company)
    _audit(data_dir, _event(actor, "it_gate_set", "company", gate["mode"],
                            ready=gate["ready"], missing=gate["missing"]))
    return {"ok": True, "op": "it_gate_set", "ready": gate["ready"], "mode": gate["mode"],
            "missing": gate["missing"], "link": "/it"}


def juusetsu_consent_record(data_dir, params, actor, role):
    """電磁的交付の『相手方の事前承諾』（方法・ファイル形式・日時）を監査へ記録する（設計§4・新規B）。
    承諾が無い案件×相手方は juusetsu_deliver を fail-closed でブロックする根拠。
    可逆＝撤回は method="refuse"（拒否申出）を記録し、以後の交付をブロック（マニュアル3-1(1)⑥・
    施行令2条の6第2項等=拒否後の電磁的提供禁止）。再承諾の記録で再び交付可能。"""
    _check_role("juusetsu_consent_record", role)
    cid = (params.get("case_id") or "").strip()
    recipient = (params.get("recipient") or "").strip()
    method = (params.get("method") or "").strip()
    file_format = (params.get("file_format") or "").strip().lower()
    if not cid or not recipient:
        raise OpError(400, "case_id と recipient（相手方）が必要です。")
    if method in ("refuse", "拒否"):
        # nonce: 同一秒に承諾⇄拒否が並ぶと dedup_key(内容同一・秒精度event_id)が衝突し
        # 後続イベントが無音で落ちる=撤回が効かない。意思表示の時系列は全件残す。
        _audit(data_dir, _event(actor, "juusetsu_consent", cid, "refused",
                                recipient=recipient, method="refuse", case=cid,
                                nonce=os.urandom(4).hex()))
        return {"ok": True, "op": "juusetsu_consent_record", "case_id": cid,
                "recipient": recipient, "method": "refuse", "status": "refused", "link": "/it"}
    if method not in ("email", "download", "usb"):
        raise OpError(400, "method は email / download / usb（撤回は refuse）のいずれかです。")
    if file_format not in ("pdf", "html"):
        raise OpError(400, "file_format は pdf / html のいずれかです。")
    _audit(data_dir, _event(actor, "juusetsu_consent", cid, "recorded",
                            recipient=recipient, method=method, file_format=file_format, case=cid,
                            nonce=os.urandom(4).hex()))
    return {"ok": True, "op": "juusetsu_consent_record", "case_id": cid, "recipient": recipient,
            "method": method, "file_format": file_format, "link": "/it"}


def juusetsu_deliver(data_dir, params, actor, role):
    """記名確定済み重説を電磁的に『交付』し、content_sha256 束縛の交付証跡を監査へ確定する（設計§4・新規B）。
    前提を両方満たさなければ 403（fail-closed）:
      (1) 当該 doc_id×version が記名確定済み（finalize の content_hash と本文hashが一致）。
      (2) 当該案件×相手方に電磁的交付の承諾記録がある。
    交付連絡は line_send（queued＝実送信しない・送信は line_confirm_send の人間ゲート）を再利用する。"""
    _check_role("juusetsu_deliver", role)
    from hub_core import documents as _docs
    cid = (params.get("case_id") or "").strip()
    doc_id = (params.get("doc_id") or "").strip()
    recipient = (params.get("recipient") or "").strip()
    ver_raw = params.get("version")
    if not cid or not doc_id or not recipient:
        raise OpError(400, "case_id・doc_id・recipient（相手方）が必要です。")
    version = None
    if ver_raw is not None and str(ver_raw).strip():
        try:
            version = int(str(ver_raw).strip())
        except ValueError:
            raise OpError(400, "version は整数で指定してください。")
    try:
        ver = _docs.get_version(data_dir, doc_id, version)
    except _docs.DocError as e:
        raise OpError(e.code, e.msg)
    meta = ver["meta"]
    if meta.get("kind") != "juusetsu":
        raise OpError(403, "重要事項説明書として保存された書類だけを交付できます。")
    bound_case = str(meta.get("case_id") or "").strip()
    if not bound_case:
        raise OpError(409, "この重説は案件に紐付いていません。案件画面から作り直してください。")
    if bound_case != cid:
        raise OpError(403, f"この重説は別の案件（{bound_case}）に紐付いているため交付できません。")
    if not _store(data_dir).query("cases", "case_id = ?", (cid,)):
        raise OpError(404, f"案件 {cid} が見つかりません。")
    content_sha256 = meta.get("content_sha256") or ""
    delivered_version = int(meta.get("version") or 0)
    # 前提(1): このdoc_id×版×本文hash×案件が同じ記名確定イベントに束縛されているか。
    if not _finalized_version_exists(
            data_dir, doc_id, delivered_version, content_sha256, case_id=cid):
        raise OpError(403, "この版は記名確定されていません（記名確定した重説のみ交付できます）。")
    # 前提(2): 相手方の電磁的交付の事前承諾があるか。最新記録が拒否申出なら提供禁止
    # （マニュアル3-1(1)⑥「拒否する場合は電磁的方法による提供をしてはいけません」）。
    consent = _latest_juusetsu_consent(data_dir, cid, recipient)
    if consent is None:
        raise OpError(403, "電磁的交付の事前承諾が記録されていません（先に承諾を記録してください）。")
    if (consent.get("gate_status") or "") == "refused":
        raise OpError(403, "電磁的交付の拒否申出が記録されています（紙交付へ切替。再承諾の記録があれば再開可）。")
    method = consent.get("method") or ""
    file_format = consent.get("file_format") or ""
    # 交付連絡を outbox に積む（queued＝実送信しない。実送信は line_confirm_send の人間ゲート）。
    notify = apply_operation(data_dir, "line_send", {
        "to_user": recipient, "kind": "push", "case_id": cid,
        "text": f"重要事項説明書（確定版）をお送りします。ご説明の前にご確認ください。"
                f"（交付方法: {method} / 形式: {file_format}）",
    }, actor, role)
    msg_id = notify.get("msg_id") or ""
    # 交付イベントに会社プロファイル版を刻む。後日 業者情報を変えても、
    # 「どの業者表示で交付したか」が監査チェーン（HMAC append-only）側にも残る。
    from hub_core import branding as _brx
    _ph_deliver = _brx.snapshot_profile(data_dir)
    _audit(data_dir, _event(actor, "juusetsu_delivered", cid, "delivered",
                            company_profile_hash=_ph_deliver,
                            doc_id=doc_id, version=delivered_version, content_sha256=content_sha256,
                            recipient=recipient, method=method, file_format=file_format,
                            msg_id=msg_id, case=cid))
    return {"ok": True, "op": "juusetsu_deliver", "case_id": cid, "doc_id": doc_id,
            "version": delivered_version, "content_sha256": content_sha256, "recipient": recipient,
            "method": method, "file_format": file_format, "msg_id": msg_id, "status": "queued",
            "link": "/it"}


SLOT_KINDS = ("内見", "IT重説")
_SLOT_START_HOUR = 9
_SLOT_END_HOUR = 18


def _parse_dt(raw):
    """"2026-07-20T14:00" / "... 14:00" / "...:00" を datetime へ（失敗は None・捏造しない）。"""
    s = str(raw or "").strip().replace(" ", "T")
    if len(s) == 16:   # 分までなら秒を補う
        s += ":00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _load_all_it_sessions(data_dir):
    """audit から IT重説セッションの最新状態を session_id ごとに集約（中止も含む全件）。"""
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    latest = {}
    if not ap.is_file():
        return []
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("action") == "it_session" and isinstance(ev.get("it_state"), dict):
            latest[ev.get("target")] = ev["it_state"]
    return list(latest.values())


def _day_bookings(data_dir, date: str) -> list:
    """指定日（YYYY-MM-DD）の担当予定＝内見イベント＋IT重説セッション（中止除く）を
    (start, end, label) で返す。内見/IT重説を単一の担当カレンダーで扱う（設計§5）。"""
    out = []
    st = _store(data_dir)
    try:
        rows = st.query("events", "event_type = ?", ("内見",))
    except Exception:
        rows = []
    for r in rows:
        dt = _parse_dt(r.get("event_at"))
        if dt is not None and dt.date().isoformat() == date:
            out.append((dt, dt + timedelta(hours=1), "内見"))
    for s in _load_all_it_sessions(data_dir):
        if (s.get("state") or "") == "中止":
            continue
        dt = _parse_dt(s.get("scheduled_at"))
        if dt is not None and dt.date().isoformat() == date:
            out.append((dt, dt + timedelta(hours=1), "IT重説"))
    return out


def schedule_slots(data_dir, params, actor, role):
    """担当カレンダーの空き枠を算出（設計§5・新規C・読取）。内見/IT重説を単一の担当予定で扱い、
    9:00–18:00 の毎時枠から、既存の内見イベント＋IT重説セッションと重複する枠を unavailable にする。
    harness_configured 時は Google FreeBusy（担当実予定）を fail-open で重ねる
    （harness未設定/死でもあいのて単独で枠が出る＝ローカルファースト）。"""
    _check_role("schedule_slots", role)
    cid = (params.get("case_id") or "").strip()
    date = (params.get("date") or "").strip()
    kind = (params.get("kind") or "").strip()
    if not date:
        raise OpError(400, "date（YYYY-MM-DD）が必要です。")
    if kind not in SLOT_KINDS:
        raise OpError(400, "kind は 内見 / IT重説 のいずれかです。")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise OpError(400, f"date は YYYY-MM-DD 形式で: {date!r}")
    bookings = _day_bookings(data_dir, date)
    # harness FreeBusy（担当の実予定）を fail-open で重ねる。未設定/失敗でもあいのて単独で枠は出す。
    from hub_core import connections as _conn
    harness_busy = []
    harness_used = False
    if _conn.harness_configured():
        hs = _conn.harness_calendar_slots(date, start_hour=_SLOT_START_HOUR, end_hour=_SLOT_END_HOUR)
        if hs.get("ok"):
            harness_used = True
            for s in hs.get("slots") or []:
                if not s.get("available"):
                    b0, b1 = _parse_dt(s.get("start")), _parse_dt(s.get("end"))
                    if b0 is not None and b1 is not None:
                        harness_busy.append((b0, b1, "外部カレンダー"))
    all_busy = bookings + harness_busy
    slots = []
    base = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=JST)
    for h in range(_SLOT_START_HOUR, _SLOT_END_HOUR):
        s0 = base.replace(hour=h)
        s1 = s0 + timedelta(hours=1)
        reason = ""
        for b0, b1, label in all_busy:
            # tz有無を揃えて比較（events/scheduled_at は naive、base は JST 付き）
            bb0 = b0.replace(tzinfo=JST) if b0.tzinfo is None else b0
            bb1 = b1.replace(tzinfo=JST) if b1.tzinfo is None else b1
            if s0 < bb1 and s1 > bb0:
                reason = f"{label}と重複"
                break
        slots.append({"start": s0.strftime("%Y-%m-%dT%H:%M"),
                      "end": s1.strftime("%Y-%m-%dT%H:%M"),
                      "available": not reason, "reason": reason})
    return {"ok": True, "op": "schedule_slots", "case_id": cid, "date": date, "kind": kind,
            "harness_used": harness_used, "slots": slots,
            "available_count": sum(1 for s in slots if s["available"])}


def portal_link(data_dir, params, actor, role):
    """案件の顧客ポータル・マジックリンクを生成（G7）。expires(YYYY-MM-DD)は呼出側が渡す。
    リンクの実配布は M-sender 経由＝人間ゲート（ここはトークン生成のみ）。"""
    _check_role("portal_link", role)
    from hub_core import portal as _pt
    cid = (params.get("case_id") or "").strip()
    exp = (params.get("expires") or "").strip()
    if not cid or not exp:
        raise OpError(400, "case_id と expires(YYYY-MM-DD) が必要です。")
    try:
        token = _pt.make_token(cid, exp, scope=(params.get("scope") or "customer"))
    except _pt.PortalError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "portal_link_issued", cid, "issued", expires=exp))
    return {"ok": True, "op": "portal_link", "case_id": cid, "token": token,
            "url": "/portal?token=" + token, "expires": exp,
            "note": "このリンクの配布は送信ゲート（人間確認）を通してください。"}


def viewing_schedule(data_dir, params, actor, role):
    """内見を予約する(events に内見イベントを追加・可逆)＋カレンダー取込用の .ics 成果物を1件生成。
    .ics は data_dir/documents/VIEW-<event_id>/ に保存（成果物ディレクトリ＝書類ストアの正本方式）。
    物件名/住所は property_info（案件ID）から取り、無ければ埋めない（捏造しない）。"""
    _check_role("viewing_schedule", role)
    from hub_core import ics as _ics, documents as _docs, property_info as _pi
    cid = (params.get("case_id") or "").strip()
    at = (params.get("event_at") or "").strip()
    if not cid or not at:
        raise OpError(400, "case_id と event_at(内見日時)が必要です。")
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (cid,))
    if not cases:
        raise OpError(404, f"案件 {cid} が見つかりません。")
    case = cases[0]
    eid = "EV-" + hashlib.sha256(f"{cid}|{at}|{_now()}".encode("utf-8")).hexdigest()[:12].upper()
    st.insert_row("events", _full_row("events", {
        "event_id": eid, "source_tool": "viewing_schedule", "event_type": "内見",
        "event_at": at, "customer_id": case.get("customer_id") or "", "case_id": cid,
        "property_id": case.get("property_id") or "", "source_ref": cid}))
    # カレンダー取込用 .ics（担当が確定した日時のみ・物件名/住所は捏造せず property_info から）
    pf = _pi.load_property_info(data_dir, cid) or {}
    name = str(pf.get("property_name") or "").strip()
    ics_doc_id, ics_error = "", ""
    try:
        # お客様の予定表に出る主催者は取扱会社（製品名を出さない・未設定なら主催者行を出さない）
        from hub_core.auth import load_company as _lc
        _co = _lc(data_dir, strict=True) or {}
        body = _ics.build_viewing_ics(
            event_id=eid, event_at=at,
            summary=(f"{name} 内見".strip() if name else "内見"),
            location=str(pf.get("address") or "").strip(),
            organizer=str(_co.get("name") or "").strip(),
            organizer_email=str(_co.get("email") or "").strip(),
            description=f"案件: {cid}")
        ics_doc_id = "VIEW-" + eid
        _docs.save_version(data_dir, ics_doc_id, body, kind="ics", fmt="ics",
                           author="あいのて(内見予約)")
    except (ValueError, _docs.DocError) as exc:   # 日時が異形式でも予約(台帳)は成立＝.icsは付随成果物
        ics_doc_id, ics_error = "", f"{exc}"
    _audit(data_dir, _event(actor, "viewing_scheduled", cid, "内見", event_id=eid, event_at=at,
                            case=cid, ics=ics_doc_id))
    return {"ok": True, "op": "viewing_schedule", "case_id": cid, "event_id": eid,
            "event_at": at, "ics_doc_id": ics_doc_id, "ics_error": ics_error,
            "link": f"/case?id={cid}"}


def viewing_list(data_dir, params, actor, role):
    """物件ID(property_id)で内見予約(events.内見)を絞り込む照会（読取専用）。
    物理分離＝SQLの WHERE で property_id を実フィルタ（物件Xの一覧に物件Yは混ざらない）。"""
    _check_role("viewing_list", role)
    pid = (params.get("property_id") or "").strip()
    st = _store(data_dir)
    if pid:
        rows = st.query("events", "event_type = ? AND property_id = ?", ("内見", pid))
    else:
        rows = st.query("events", "event_type = ?", ("内見",))
    rows = sorted(rows, key=lambda r: str(r.get("event_at") or ""))
    return {"ok": True, "op": "viewing_list", "property_id": pid, "count": len(rows),
            "viewings": [{"event_id": r.get("event_id"), "event_at": r.get("event_at"),
                          "case_id": r.get("case_id"), "property_id": r.get("property_id"),
                          "customer_id": r.get("customer_id")} for r in rows]}


def billing_create(data_dir, params, actor, role):
    """請求を作成する(billing_register に追加・金銭=人間確認後に実行)。"""
    _check_role("billing_create", role)
    cid = (params.get("case_id") or "").strip()
    amount = (params.get("amount") or "").strip()
    kind = (params.get("kind") or "請求").strip()
    if not cid or not amount:
        raise OpError(400, "case_id と amount(金額)が必要です。")
    amount = str(_parse_amount(amount))   # 非負整数へ正規化（全角/カンマ/円を吸収・不正は400）
    if kind not in ("請求", "入金", "返金"):
        raise OpError(400, "kind は 請求/入金/返金 のいずれかです。")
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (cid,))
    if not cases:
        raise OpError(404, f"案件 {cid} が見つかりません。")
    bid = "BILL-" + hashlib.sha256(f"{cid}|{amount}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    # 名義は消込(全銀の入金名義との突合)に必須。案件の顧客名(和/英どちらの列でも)＋payer指定を拾う。
    payer_name = (str(params.get("payer") or "").strip()
                  or cases[0].get("顧客名") or cases[0].get("customer_name") or "")
    st.insert_row("billing_register", _full_row("billing_register", {
        "billing_id": bid, "case_id": cid, "customer_name": payer_name,
        "kind": kind, "amount": amount, "status": "発行", "gate_status": "money",
        "created_at": _now(), "source_ref": cid, "source_tool": "billing_create"}))
    # 変更は apply_operation の記録→entity_state で再構築耐性を持つ（billing専用replay不要）。
    _audit(data_dir, _event(actor, "billing_created", bid, "money", case=cid, amount=amount, kind=kind))
    return {"ok": True, "op": "billing_create", "case_id": cid, "billing_id": bid,
            "amount": amount, "kind": kind, "link": "/money"}


def zoning_lookup(data_dir, params, actor, role):
    """座標(lat/lon)→用途地域・建ぺい率・容積率（ローカル点in面・住所を外に出さない）。
    A29データ未取込なら not_available（捏造しない＝自治体都市計画図で確認）。"""
    _check_role("zoning_lookup", role)
    from hub_core import zoning as _z
    if not _z.available():
        return {"ok": True, "op": "zoning_lookup", "available": False,
                "note": "用途地域データ(A29)が未取込です。各自治体の都市計画図でご確認ください。"}
    r = _z.lookup(params.get("lat"), params.get("lon"))
    if not r:
        return {"ok": True, "op": "zoning_lookup", "available": True, "found": False,
                "note": "この座標は用途地域データの範囲外です。"}
    return {"ok": True, "op": "zoning_lookup", "available": True, "found": True, **r}


def ocr_read(data_dir, params, actor, role):
    """アップロード済みの画像/PDF原本を**無料ローカルOCR＋幾何グリッド復元**で構造化し、物件フィールドの
    下書きを返す。macOS=Vision / Windows=Windows.Media.Ocr / それ以外=tesseract（あれば）。
    クラウドに送らない・捏造しない（読取れた項目のみ）・値は宅建士確認まで台帳に流れない。"""
    _check_role("ocr_read", role)
    from pathlib import Path as _P
    from hub_core import ocr_structure as _ocst, local_ocr as _loc
    src = (params.get("source") or params.get("source_rel") or "").strip()
    if not src:
        raise OpError(400, "source（原本の相対パス）が必要です。")
    root = _P(data_dir).resolve()
    p = (root / src).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise OpError(404, f"原本が見つかりません: {src}")
    if not _loc.available():
        return {"ok": True, "op": "ocr_read", "source": src, "available": False, "fields": {}, "count": 0,
                "note": "無料ローカルOCRが使えません（macOS=Vision/Windows=Windows.Media.Ocr）。手動入力へ。"}
    fields = _ocst.structure_document(str(p))
    meta = fields.pop("_meta", {})
    return {"ok": True, "op": "ocr_read", "source": src, "available": True,
            "fields": fields, "count": len(fields), "engine": meta,
            "note": "OCRはドラフト補助です。読取値は出典（原本）に束縛され、宅建士の確認まで台帳に流れません。捏造しません。"}


# ---- FAX outbox/inbox 永続化（JSONL・load-all→rewrite。fax.py はプロトコル純関数・保持は data_dir 側）----
def _fax_jsonl(data_dir, name):
    return Path(data_dir) / name


def _read_jsonl_rows(path):
    import json as _j
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(_j.loads(line))
            except ValueError:
                pass
    return out


import threading as _threading
_FAX_LOCK = _threading.Lock()   # ThreadingサーバでのFAX永続化 read-modify-write を直列化（lost update防止）


def _write_jsonl_rows(path, rows):
    """JSONL全書き（アトミック: temp→fsync→os.replace）。truncate中クラッシュでの全損を防ぐ。"""
    import json as _j
    import os as _os
    import tempfile as _tf
    _track_webhook_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(_j.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    fd, tmp = _tf.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


def _provider_delivery_id(raw, *, field: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise OpError(400, f"{field}（プロバイダ配信ID）が必要です。安全に再送判定できない着信は保存しません。")
    if len(value) > 512:
        raise OpError(400, f"{field} が長すぎます。")
    return value


def _delivery_key(source: str, delivery_id: str) -> str:
    raw = f"{source}\x00{delivery_id}".encode("utf-8")
    return f"{source}:" + hashlib.sha256(raw).hexdigest()


def _line_event_identity(event: dict) -> tuple[str, str]:
    import json as _j
    source = str(event.get("delivery_source") or event.get("source") or "line").strip() or "line"
    delivery_id = str(event.get("delivery_id") or event.get("webhook_event_id")
                      or event.get("harness_msg_id") or "").strip()
    if not delivery_id:
        stable = {k: v for k, v in event.items()
                  if k not in ("recorded_at", "delivery_key", "delivery_id", "delivery_source")}
        canonical = _j.dumps(stable, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
        delivery_id = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if len(delivery_id) > 512:
        delivery_id = "sha256:" + hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    return delivery_id, _delivery_key(source, delivery_id)


_SEND_ATTEMPT_COLUMNS = (
    "attempt_id", "idempotency_key", "kind", "target_id", "provider", "state",
    "audit_status", "reserved_at", "updated_at", "external_id", "provider_outcome",
    "provider_finished_at", "audited_at",
)

_CREATE_SEND_ATTEMPTS_SQL = """
CREATE TABLE IF NOT EXISTS external_send_attempts (
    attempt_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    audit_status TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    provider_outcome TEXT NOT NULL DEFAULT '',
    provider_finished_at TEXT NOT NULL DEFAULT '',
    audited_at TEXT NOT NULL DEFAULT ''
)
"""

_INSERT_SEND_ATTEMPT_SQL = """
INSERT OR IGNORE INTO external_send_attempts (
    attempt_id, idempotency_key, kind, target_id, provider, state,
    audit_status, reserved_at, updated_at, external_id, provider_outcome,
    provider_finished_at, audited_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_SEND_ATTEMPT_SQL = """
UPDATE external_send_attempts SET
    idempotency_key = ?, kind = ?, target_id = ?, provider = ?, state = ?,
    audit_status = ?, reserved_at = ?, updated_at = ?, external_id = ?,
    provider_outcome = ?, provider_finished_at = ?, audited_at = ?
WHERE attempt_id = ?
"""


def _send_attempt_db_path(data_dir) -> Path:
    return Path(data_dir) / "external_send_attempts.sqlite3"


def _send_attempt_values(row: dict) -> tuple:
    return tuple(str(row.get(column) or "") for column in _SEND_ATTEMPT_COLUMNS)


def _open_send_attempt_db(data_dir) -> sqlite3.Connection:
    """外部送信予約のprocess横断正本。SQL識別子は全て固定し値だけをbindする。"""
    path = _send_attempt_db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # 空DBを複数processが同時に初期化する場合も、DDL前から待機させる。
    # journal_modeの切替自体はbusy_timeoutで待たない実装があるため行わず、
    # exactly-onceに必要な直列化はBEGIN IMMEDIATEとUNIQUE制約で担保する。
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(_CREATE_SEND_ATTEMPTS_SQL)

    # 旧JSONLは消さず、固定schemaへ一方向に取り込む。毎回実行しても一意キーで冪等。
    legacy = _read_jsonl_rows(_fax_jsonl(data_dir, "external_send_attempts.jsonl"))
    if legacy:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in legacy:
                connection.execute(_INSERT_SEND_ATTEMPT_SQL, _send_attempt_values(row))
            connection.commit()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
    return connection


def load_external_send_attempts(data_dir) -> list[dict]:
    with contextlib.closing(_open_send_attempt_db(data_dir)) as connection:
        rows = connection.execute(
            "SELECT attempt_id, idempotency_key, kind, target_id, provider, state, "
            "audit_status, reserved_at, updated_at, external_id, provider_outcome, "
            "provider_finished_at, audited_at "
            "FROM external_send_attempts ORDER BY reserved_at, attempt_id"
        ).fetchall()
    return [dict(row) for row in rows]


def _send_attempt_key(kind: str, target_id: str) -> str:
    raw = f"ainote-external-send-v1\x00{kind}\x00{target_id}".encode("utf-8")
    return "ainote-" + hashlib.sha256(raw).hexdigest()


def _send_attempt_for(data_dir, kind: str, target_id: str) -> dict | None:
    key = _send_attempt_key(kind, target_id)
    with contextlib.closing(_open_send_attempt_db(data_dir)) as connection:
        row = connection.execute(
            "SELECT attempt_id, idempotency_key, kind, target_id, provider, state, "
            "audit_status, reserved_at, updated_at, external_id, provider_outcome, "
            "provider_finished_at, audited_at FROM external_send_attempts "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    return dict(row) if row is not None else None


def _attempt_block_error(attempt: dict) -> OpError:
    state = str(attempt.get("state") or "unknown")
    key = str(attempt.get("idempotency_key") or "")
    return OpError(
        409,
        "外部送信は既に試行済みで、結果の自動判定または監査確定が完了していません。"
        "二重送信を防ぐため自動再送しません。プロバイダ側を照会し、手動で解決してください。"
        f"（状態: {state} / 冪等キー: {key}）",
    )


def _attempt_is_committed(attempt: dict | None) -> bool:
    return bool(attempt and attempt.get("state") == "accepted"
                and attempt.get("audit_status") == "committed")


def _send_confirmation(raw) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "はい", "on")


def _reserve_send_attempt(data_dir, kind: str, target_id: str, provider: str) -> dict:
    key = _send_attempt_key(kind, target_id)
    now = _now()
    attempt = {
        "attempt_id": "ATT-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper(),
        "idempotency_key": key,
        "kind": kind,
        "target_id": target_id,
        "provider": provider,
        "state": "reserved",
        "audit_status": "pending",
        "reserved_at": now,
        "updated_at": now,
        "external_id": "",
        "provider_outcome": "",
        "provider_finished_at": "",
        "audited_at": "",
    }
    with contextlib.closing(_open_send_attempt_db(data_dir)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT attempt_id, idempotency_key, kind, target_id, provider, state, "
                "audit_status, reserved_at, updated_at, external_id, provider_outcome, "
                "provider_finished_at, audited_at FROM external_send_attempts "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise _attempt_block_error(dict(existing))
            connection.execute(_INSERT_SEND_ATTEMPT_SQL, _send_attempt_values(attempt))
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    return attempt


def _update_send_attempt(data_dir, attempt: dict, **changes) -> dict:
    updated = {**attempt, **changes, "updated_at": _now()}
    with contextlib.closing(_open_send_attempt_db(data_dir)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            values = (
                str(updated.get("idempotency_key") or ""),
                str(updated.get("kind") or ""),
                str(updated.get("target_id") or ""),
                str(updated.get("provider") or ""),
                str(updated.get("state") or ""),
                str(updated.get("audit_status") or ""),
                str(updated.get("reserved_at") or ""),
                str(updated.get("updated_at") or ""),
                str(updated.get("external_id") or ""),
                str(updated.get("provider_outcome") or ""),
                str(updated.get("provider_finished_at") or ""),
                str(updated.get("audited_at") or ""),
                str(updated.get("attempt_id") or ""),
            )
            cursor = connection.execute(_UPDATE_SEND_ATTEMPT_SQL, values)
            if cursor.rowcount != 1:
                connection.rollback()
                raise OpError(409, "外部送信の試行記録が失われたため、安全に状態を更新できません。自動再送しません。")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    attempt.clear()
    attempt.update(updated)
    return attempt


def _record_provider_result(data_dir, attempt: dict, record: dict, *, sent_key: str) -> str:
    accepted = bool(record.get(sent_key))
    reported = str(record.get("provider_outcome") or "").strip().lower()
    state = "accepted" if accepted else (reported if reported in ("rejected", "unknown") else "unknown")
    _update_send_attempt(
        data_dir, attempt, state=state,
        external_id=str(record.get("external_id") or ""),
        provider_outcome=reported or state,
        provider_finished_at=_now(),
    )
    return state


def _commit_send_attempt_audit(data_dir, attempt: dict) -> None:
    _update_send_attempt(data_dir, attempt, audit_status="committed", audited_at=_now())


def load_fax_outbox(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "fax_outbox.jsonl"))


def _save_fax_job(data_dir, job):
    with _FAX_LOCK:   # load→変更→全書き を直列化（並行リクエストでの取りこぼし防止）
        rows = [r for r in load_fax_outbox(data_dir) if r.get("job_id") != job.get("job_id")]
        rows.append(job)
        _write_jsonl_rows(_fax_jsonl(data_dir, "fax_outbox.jsonl"), rows)


def load_fax_inbox(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "fax_inbox.jsonl"))


def bukkaku_send(data_dir, params, actor, role):
    """物確(物件確認)FAXを作成してoutboxに積む(queued)。物件の合流レコード(property_info)から物確FAX本文を
    決定論生成。実送信はまだ=送信確認(fax_confirm_send)が要る・既定Mock=実送信しない。可逆(下書き)。"""
    _check_role("bukkaku_send", role)
    import datetime as _d
    from hub_core import fax as _fax, property_info as _pi, documents as _docs, branding as _br
    from hub_core.auth import load_company
    cid = (params.get("case_id") or "").strip()
    to_number = (params.get("to_number") or params.get("fax") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    if not to_number:
        raise OpError(400, "to_number(送信先FAX番号)が必要です。")
    fields = _pi.load_property_info(data_dir, cid)
    if not fields:
        st = _store(data_dir)
        rows = st.query("cases", "case_id = ?", (cid,))
        if rows:
            fields = {"property_name": rows[0].get("物件名") or ""}
    company = load_company(data_dir, strict=True)   # 社外へ出るFAX。壊れた正本で送らない
    sender = {"company_name": company.get("name"), "staff": company.get("staff"),
              "tel": company.get("tel"), "fax": company.get("fax")}
    body = _fax.build_bukkaku_fax(fields, sender=sender, today=_d.date.today().isoformat())
    idempotency_key = str(params.get("idempotency_key") or "").strip()
    job_seed = f"{cid}|{to_number}|{idempotency_key}" if idempotency_key else f"{cid}|{to_number}|{_now()}"
    job_id = "FAX-" + hashlib.sha256(job_seed.encode("utf-8")).hexdigest()[:10].upper()
    doc_id = "BUKKAKU-" + job_id
    _track_webhook_path(Path(data_dir) / "documents" / doc_id)
    expected_profile_hash = _br.profile_hash(company)
    _track_webhook_path(_br.snapshot_dir(data_dir) / f"{expected_profile_hash}.json")
    profile_hash = _br.snapshot_profile(data_dir)
    _docs.save_version(data_dir, doc_id, body, kind="fax", fmt="md", author="あいのて(物確FAX)",
                       case_id=cid, company_profile_hash=profile_hash)
    job = _fax.new_fax_job(job_id, to_number, doc_id,
                           "物確: " + (str(fields.get("property_name") or cid)), pages=1)
    job["case_id"] = cid
    _save_fax_job(data_dir, job)
    _audit(data_dir, _event(actor, "bukkaku_queued", job_id, "queued", case=cid, to=to_number))
    return {"ok": True, "op": "bukkaku_send", "job_id": job_id, "doc_id": doc_id, "status": "queued",
            "note": "物確FAXをoutboxに積みました。送信確認(fax_confirm_send)で人が確認してから送ります"
                    "（既定Mock=実送信しません）。"}


def fax_confirm_send(data_dir, params, actor, role):
    """outboxの物確FAXを人が確認して送信(queued→gated→sent)。**既定Mock=実送信しない**。
    実プロバイダは1回限りのallow_real_send、永続attempt、provider冪等キーを全て満たす時だけ呼ぶ。"""
    _check_role("fax_confirm_send", role)
    from hub_core import fax as _fax
    jid = (params.get("job_id") or "").strip()
    job = next((r for r in load_fax_outbox(data_dir) if r.get("job_id") == jid), None)
    if job is None:
        raise OpError(404, f"FAXジョブ {jid} が見つかりません。")
    existing_attempt = _send_attempt_for(data_dir, "fax", jid)
    if existing_attempt is not None:
        if _attempt_is_committed(existing_attempt):
            return {"ok": True, "op": "fax_confirm_send", "job_id": jid, "status": "sent",
                    "sent": True, "provider": existing_attempt.get("provider", ""),
                    "external_id": existing_attempt.get("external_id", ""),
                    "note": "既にプロバイダ受理・監査確定済みです。再送していません。"}
        raise _attempt_block_error(existing_attempt)
    if job.get("status") == "sent":   # 冪等: 二重送信/再読込でも落ちない
        return {"ok": True, "op": "fax_confirm_send", "job_id": jid, "status": "sent",
                "sent": job.get("sent", False), "provider": job.get("provider", ""),
                "note": "既に送信済みです。"}
    provider = _fax.build_fax_provider(data_dir)
    connected = bool(getattr(provider, "connected", False))
    approved = _send_confirmation(params.get("allow_real_send"))
    if connected and not approved:
        raise OpError(403, "実FAX送信には、この送信1回に対する明示承認が必要です。")
    attempt = None
    try:
        if job.get("status") == "queued":
            _fax.transition(job, "gated", actor=actor)   # 人間の送信確認＝ゲート
        if connected:
            attempt = _reserve_send_attempt(data_dir, "fax", jid, str(provider.name))
            job["idempotency_key"] = attempt["idempotency_key"]
            job["send_attempt_id"] = attempt["attempt_id"]
        _fax.transition(job, "sent", provider=provider,
                        allow_real_send=bool(connected and approved), actor=actor)
    except _fax.FaxError as exc:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(exc).__name__, provider_finished_at=_now())
        raise OpError(exc.code, exc.msg)                 # FaxErrorはOpErrorでないので変換（未捕捉500回避）
    except Exception as exc:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(exc).__name__, provider_finished_at=_now())
        raise
    attempt_state = _record_provider_result(
        data_dir, attempt, job, sent_key="sent") if attempt is not None else "mock"
    if connected and attempt_state != "accepted" and job.get("status") == "sent":
        _fax.transition(job, "failed", actor=actor, note="プロバイダ受理を確認できません")
    _save_fax_job(data_dir, job)
    _audit(data_dir, _event(actor, "fax_sent", jid, "sent",
                            sent=str(job.get("sent")), provider=job.get("provider"),
                            send_attempt_id=(attempt or {}).get("attempt_id", ""),
                            idempotency_key=(attempt or {}).get("idempotency_key", ""),
                            provider_outcome=job.get("provider_outcome", "")))
    if attempt is not None:
        _commit_send_attempt_audit(data_dir, attempt)
        if attempt_state != "accepted":
            raise OpError(409, "FAXの受理を確認できません。二重送信防止のため自動再送しません。"
                               "プロバイダ側を照会してください。")
    return {"ok": True, "op": "fax_confirm_send", "job_id": jid, "status": job["status"],
            "sent": job["sent"], "provider": job["provider"],
            "external_id": job.get("external_id", ""),
            "note": "送信ゲートを通しました。既定Mockのため実際のFAX送信は行っていません"
                    "（実送信は実プロバイダ接続＋明示承認が必要＝人間ゲート）。"}


def fax_receive(data_dir, params, actor, role):
    """着信FAX(正規化済 inbound + OCRテキスト)→物確回答を抽出してinboxに保存。Webhookから呼ぶ。可逆(下書き)。
    OCRは呼出側の無料ローカルOCR。読み取れない回答は入れない(捏造しない)。"""
    _check_role("fax_receive", role)
    from hub_core import fax as _fax
    inbound = params.get("inbound") if isinstance(params.get("inbound"), dict) else {}
    ocr_text = str(params.get("ocr_text") or "").strip()
    reply = _fax.parse_bukkaku_reply(ocr_text) if ocr_text else {}
    fid = _provider_delivery_id(inbound.get("fax_id") or inbound.get("delivery_id"), field="fax_id")
    provider_delivery_id = _provider_delivery_id(inbound.get("delivery_id") or fid,
                                                 field="delivery_id")
    delivery_key = _delivery_key("fax", provider_delivery_id)
    rec = {"fax_id": fid, "provider_delivery_id": provider_delivery_id,
           "delivery_key": delivery_key, "from_number": inbound.get("from_number", ""),
           "pages": inbound.get("pages", ""), "reply": reply, "ocr_len": len(ocr_text),
           "received_at": inbound.get("received_at") or _now(), "recorded_at": _now()}
    with _FAX_LOCK:   # 並行着信での lost update 防止
        rows = load_fax_inbox(data_dir)
        existing = next((row for row in rows
                         if row.get("delivery_key") == delivery_key
                         or (not row.get("delivery_key") and
                             (row.get("provider_delivery_id") == provider_delivery_id
                              or row.get("fax_id") == fid))), None)
        if existing is not None:
            return {"ok": True, "op": "fax_receive", "fax_id": existing.get("fax_id") or fid,
                    "reply": existing.get("reply") or {}}
        rows.append(rec)
        _write_jsonl_rows(_fax_jsonl(data_dir, "fax_inbox.jsonl"), rows)
    _audit(data_dir, _event(actor, "fax_received", fid, "received",
                            provider_delivery_id=provider_delivery_id,
                            status=reply.get("status", "")))
    return {"ok": True, "op": "fax_receive", "fax_id": fid, "reply": reply}


# ==================== LINE ハーネス（fax と同じ型・line_outbox/inbox.jsonl） ====================
def load_line_outbox(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "line_outbox.jsonl"))


def load_line_inbox(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "line_inbox.jsonl"))


def _save_line_msg(data_dir, msg):
    with _FAX_LOCK:
        rows = [r for r in load_line_outbox(data_dir) if r.get("msg_id") != msg.get("msg_id")]
        rows.append(msg)
        _write_jsonl_rows(_fax_jsonl(data_dir, "line_outbox.jsonl"), rows)


def line_send(data_dir, params, actor, role):
    """LINEメッセージを outbox に積む（queued・実送信しない）。送信確認(line_confirm_send)が要る。可逆。"""
    _check_role("line_send", role)
    from hub_core import line as _line
    to_user = (params.get("to_user") or "").strip()
    text = (params.get("text") or "").strip()
    kind = (params.get("kind") or "push").strip()
    if not text:
        raise OpError(400, "text が必要です。")
    mid = "LINE-" + hashlib.sha256(f"{to_user}|{text}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    msg = _line.new_line_message(mid, to_user, text, kind=kind)
    msg["case_id"] = (params.get("case_id") or "").strip()
    _save_line_msg(data_dir, msg)
    _audit(data_dir, _event(actor, "line_queued", mid, "queued", to=to_user))
    return {"ok": True, "op": "line_send", "msg_id": mid, "status": "queued",
            "note": "LINEをoutboxに積みました。送信確認で人が確認してから送ります（既定Mock=実送信しません）。"}


def line_flex_property_send(data_dir, params, actor, role):
    """公開物件（liff-export/properties.json）を写真つきFlexカード（カルーセル）にして outbox に積む。
    実送信はしない＝送信は既存の line_confirm_send（送信ゲート＋allow_real_send）を通す。可逆（queued）。
    - properties.json が無い/空なら ok:false で「先にLIFFエクスポート」を案内（勝手に幽霊物件を作らない）。
    - property_ids のうち公開データに無いIDは missing_ids に開示（黙って落とさない）。全滅なら 404。
    - carousel は最大10 bubble（超過は truncated で報告）。捏造しない。"""
    _check_role("line_flex_property_send", role)
    import json as _json
    from hub_core import line as _line, flex_card as _fc
    to_user = (params.get("to_user") or "").strip()
    if not to_user:
        raise OpError(400, "to_user（友だちのUUID）が必要です。")
    pids = params.get("property_ids")
    if isinstance(pids, str):
        pids = [x.strip() for x in pids.replace("\n", ",").split(",") if x.strip()]
    elif isinstance(pids, (list, tuple)):
        pids = [str(x).strip() for x in pids if str(x).strip()]
    else:
        pids = []
    if not pids:
        raise OpError(400, "property_ids（公開物件のID）が必要です。")
    badge = (params.get("badge") or "").strip()

    props_path = Path(data_dir) / "liff-export" / "properties.json"
    if not props_path.is_file():
        return {"ok": False, "op": "line_flex_property_send", "status": "no_export",
                "note": "公開物件データ（liff-export/properties.json）がありません。"
                        "先に「LIFFへ物件を書き出す」（LIFFエクスポート）を実行してください。", "link": "/line"}
    try:
        all_props = _json.loads(props_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "op": "line_flex_property_send", "status": "no_export",
                "note": "公開物件データを読み込めませんでした。LIFFエクスポートをやり直してください。", "link": "/line"}
    if not isinstance(all_props, list) or not all_props:
        return {"ok": False, "op": "line_flex_property_send", "status": "no_export",
                "note": "公開物件がありません。物件を公開してからLIFFエクスポートしてください。", "link": "/line"}

    by_id = {str(p.get("id")): p for p in all_props if isinstance(p, dict) and p.get("id")}
    selected, missing_ids = [], []
    for pid in pids:
        (selected.append(by_id[pid]) if pid in by_id else missing_ids.append(pid))
    if not selected:
        raise OpError(404, f"指定の物件が公開データに見つかりません: {', '.join(missing_ids)}"
                           "（先に物件を公開＋LIFFエクスポートしてください）。")

    # お客様に届くカードは利用会社のブランドで出す（あいのて自身の名前・色は出さない）。
    from hub_core.auth import load_company as _load_company
    try:
        carousel = _fc.build_property_carousel(selected, badge=badge,
                                               company=_load_company(data_dir, strict=True))
    except _fc.CardComplianceError as exc:
        # 物件カードは広告。取引態様・商号・免許証番号が欠けたまま送らせない。
        raise OpError(400, "広告に必要な表示が足りないため、カードを作れません: "
                           + "、".join(exc.missing)
                           + "（取引態様は物件の公開設定、商号と免許証番号は業者情報で入れてください）") from exc
    truncated = carousel.pop("truncated", 0)   # 報告用キーは wire に載せない（clean container）
    n = len(carousel.get("contents", []))
    alt = f"物件のご紹介（{n}件）"
    mid = "LINE-" + hashlib.sha256(f"{to_user}|flex|{','.join(pids)}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    msg = _line.new_line_flex_message(mid, to_user, carousel, alt, kind="push")
    msg["case_id"] = (params.get("case_id") or "").strip()
    _save_line_msg(data_dir, msg)
    _audit(data_dir, _event(actor, "line_flex_queued", mid, "queued", to=to_user, count=n))
    return {"ok": True, "op": "line_flex_property_send", "msg_id": mid, "status": "queued",
            "count": n, "truncated": truncated, "missing_ids": missing_ids,
            "note": "物件カードをoutboxに積みました。送信確認（人の確認）を通してから送ります"
                    "（既定Mock=実送信しません）。", "link": "/line"}


def line_confirm_send(data_dir, params, actor, role):
    """outboxのLINEを人が確認して送信。実チャネルは永続attempt＋provider冪等キーで一度だけ呼ぶ。"""
    _check_role("line_confirm_send", role)
    from hub_core import line as _line
    mid = (params.get("msg_id") or "").strip()
    msg = next((r for r in load_line_outbox(data_dir) if r.get("msg_id") == mid), None)
    if msg is None:
        raise OpError(404, f"LINEメッセージ {mid} が見つかりません。")
    existing_attempt = _send_attempt_for(data_dir, "line", mid)
    if existing_attempt is not None:
        if _attempt_is_committed(existing_attempt):
            return {"ok": True, "op": "line_confirm_send", "msg_id": mid, "status": "sent",
                    "sent": True, "external_id": existing_attempt.get("external_id", ""),
                    "note": "既にプロバイダ受理・監査確定済みです。再送していません。"}
        raise _attempt_block_error(existing_attempt)
    if msg.get("status") == "sent":   # 冪等: 二重送信/再読込でも落ちない
        return {"ok": True, "op": "line_confirm_send", "msg_id": mid, "status": "sent",
                "sent": msg.get("sent", False), "note": "既に送信済みです。"}
    # プロバイダ選択: line-harness連携(env)が揃い、かつ明示承認(allow_real_send)がある時だけ実送信。
    # それ以外は Mock（実送信しない）＝既定。実チャネル送信は毎回の人間承認が必須（esign/faxと同一規律）。
    from hub_core import connections as _conn
    approve = _send_confirmation(params.get("allow_real_send"))
    configured = _conn.harness_configured()
    if configured and not approve:
        raise OpError(403, "実LINE送信には、この送信1回に対する明示承認が必要です。")
    provider = _line.HarnessLineProvider() if configured else _line.MockLineProvider()
    attempt = None
    try:
        if msg.get("status") == "queued":
            _line.transition(msg, "gated", actor=actor)
        if configured:
            attempt = _reserve_send_attempt(data_dir, "line", mid, str(provider.name))
            msg["idempotency_key"] = attempt["idempotency_key"]
            msg["send_attempt_id"] = attempt["attempt_id"]
        _line.transition(msg, "sent", provider=provider,
                         allow_real_send=bool(configured and approve), actor=actor)
    except _line.LineError as exc:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(exc).__name__, provider_finished_at=_now())
        raise OpError(exc.code, exc.msg)   # LineErrorはOpErrorでないので変換（未捕捉500回避）
    except Exception as exc:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(exc).__name__, provider_finished_at=_now())
        raise
    attempt_state = _record_provider_result(
        data_dir, attempt, msg, sent_key="sent") if attempt is not None else "mock"
    if configured and attempt_state != "accepted" and msg.get("status") == "sent":
        _line.transition(msg, "failed", actor=actor, note="プロバイダ受理を確認できません")
    _save_line_msg(data_dir, msg)
    _audit(data_dir, _event(actor, "line_sent", mid, msg.get("status", ""),
                            sent=str(msg.get("sent")),
                            send_attempt_id=(attempt or {}).get("attempt_id", ""),
                            idempotency_key=(attempt or {}).get("idempotency_key", ""),
                            provider_outcome=msg.get("provider_outcome", "")))
    if attempt is not None:
        _commit_send_attempt_audit(data_dir, attempt)
        if attempt_state != "accepted":
            raise OpError(409, "LINEの受理を確認できません。二重送信防止のため自動再送しません。"
                               "プロバイダ側を照会してください。")
    return {"ok": True, "op": "line_confirm_send", "msg_id": mid, "status": msg["status"],
            "sent": msg["sent"], "external_id": msg.get("external_id", ""),
            "note": "送信ゲートを通しました（未接続時はMockのため実送信していません）。"}


def line_receive(data_dir, params, actor, role):
    """LINE着信イベント（正規化済リスト）をinboxに保存し顧客/案件の接触素材に。可逆(下書き)。捏造しない。"""
    _check_role("line_receive", role)
    events = params.get("events") if isinstance(params.get("events"), list) else []
    normalized = []
    seen_input = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        delivery_id, delivery_key = _line_event_identity(ev)
        if delivery_key in seen_input:
            continue
        seen_input.add(delivery_key)
        source = str(ev.get("delivery_source") or ev.get("source") or "line").strip() or "line"
        normalized.append({**ev, "delivery_id": delivery_id, "delivery_source": source,
                           "delivery_key": delivery_key})
    if not normalized:
        return {"ok": True, "op": "line_receive", "received": 0}
    saved = 0
    with _FAX_LOCK:
        rows = load_line_inbox(data_dir)
        existing_keys = {str(row.get("delivery_key") or "") for row in rows}
        for ev in normalized:
            if ev["delivery_key"] in existing_keys:
                continue
            rows.append({**ev, "recorded_at": _now()})
            existing_keys.add(ev["delivery_key"])
            saved += 1
        if saved:
            _write_jsonl_rows(_fax_jsonl(data_dir, "line_inbox.jsonl"), rows)
    if saved:
        batch_id = hashlib.sha256("|".join(sorted(seen_input)).encode("utf-8")).hexdigest()[:16]
        _audit(data_dir, _event(actor, "line_received", f"LINE-IN-{batch_id}", "received",
                                count=str(saved)))
    return {"ok": True, "op": "line_receive", "received": len(normalized)}


# ---- pull型着信取込（harness API をあいのてがポーリング→未取込のincomingだけをinboxへ・dedupe永続） ----
import logging as _logging
_LINE_PULL_LOG = _logging.getLogger("hub_core.line_pull")


# pull ストリームの版。2 = 無フィルタ（送受信両方）。1（またはキー無し）= 旧 direction=incoming のみ。
# 版が上がると cursor の意味が変わる（filter が変わる）ので、poll_once は一度だけ cursor を0にリセットし
# 全再走査する（メッセージUUID dedupe が二重取込を防ぐ移行の安全装置＝過去のoutgoing履歴もこれで入る）。
_LINE_PULL_STREAM_VERSION = 2


def _line_pull_state_path(data_dir):
    return Path(data_dir) / "line_pull_state.json"


def _load_line_pull_state(data_dir) -> dict:
    """pull取込の永続状態を返す {"seen": set(取込済メッセージUUID), "cursor": str(増分feedのnextCursor),
    "stream_version": int(取り込んだストリームの版)}。
    cursor は harness が発行した**不透明トークン**（実体はrowid・あいのては中身をパースせず echo するだけ）。
    壊れていれば空（安全側＝cursor="" で先頭から再走査＋書込前のUUID dedupeで重複を防ぐ）。
    stream_version 欠落＝旧 incoming-only 状態（=1扱い・poll_once が移行する）。
    ⚠️ **同一data_dirは単一puller前提**: seen/cursor は last-writer-wins で分散ロックを持たない。
    複数端末/プロセスからの同時pullは未サポート（重複や取りこぼしの原因になる）。"""
    import json as _j
    p = _line_pull_state_path(data_dir)
    if not p.is_file():
        return {"seen": set(), "cursor": "", "stream_version": _LINE_PULL_STREAM_VERSION}
    try:
        data = _j.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": set(), "cursor": "", "stream_version": _LINE_PULL_STREAM_VERSION}
    if not isinstance(data, dict):
        return {"seen": set(), "cursor": "", "stream_version": _LINE_PULL_STREAM_VERSION}
    seen = data.get("seen")
    try:
        ver = int(data.get("stream_version") or 1)   # 版キー欠落＝旧incoming-only（=1）
    except (TypeError, ValueError):
        ver = 1
    return {"seen": set(str(x) for x in seen) if isinstance(seen, list) else set(),
            "cursor": str(data.get("cursor") or ""), "stream_version": ver}


def _save_line_pull_state(data_dir, seen: set, cursor: str,
                          stream_version: int = _LINE_PULL_STREAM_VERSION) -> None:
    """取込済UUID集合＋カーソル＋ストリーム版をアトミックに保存（temp→fsync→os.replace）。
    順序は安定させ差分を読みやすく。単一puller前提（上記 _load 参照）＝プロセス内は _FAX_LOCK 呼び側で
    直列化するが端末間の同時実行は守らない。"""
    import json as _j
    import os as _os
    import tempfile as _tf
    p = _line_pull_state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _j.dumps({"seen": sorted(seen), "cursor": str(cursor or ""),
                        "stream_version": int(stream_version), "updated_at": _now()},
                       ensure_ascii=False)
    fd, tmp = _tf.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, p)
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


def poll_once(data_dir, *, actor: str = "line-harness-pull", role: str = "代表",
              limit: int = 200, max_pages: int = 50) -> dict:
    """harness API を1周ポーリングし、**未取込のincomingメッセージだけ**を既存の line_receive 経由で
    inboxへ取り込む。冪等: (a) 増分feed の cursor を永続＝次回はそこから増分取得、(b) メッセージid dedupe を
    永続（line_pull_state.json）＝cursor 端の再送やページ境界でも重複ゼロ。再実行しても二重取込しない。
    - env(URL/API_KEY)未設定なら**静かにno-op**（Mock既定のあいのてを壊さない）。
    - 認証失敗・接続失敗は例外を握り潰さずログに記録し、**台帳には何も書かない**（fail-closed・捏造しない）。
    - 書込は line_receive（正規化済eventの単一書込経路）に委譲＝役割ゲート＋HMAC監査を温存。
    - fetchは connections.harness_pull_feed（**唯一のfetch境界**・増分feedの単一endpoint）だけを呼ぶ＝
      endpoint選択・ページングを このロジックに漏らさない（N+1のper-friend APIは使わない）。
    - cursor は**不透明トークン**（中身をパースしない・direction=incoming ストリームに束縛して一貫使用）。
      レート上限(429)は fetch境界が台帳に何も書かず返す＝ここも書込ゼロで retry_after を上位へ伝える。
    ⚠️ 同一data_dirは**単一puller前提**（seen/cursorは分散ロック無し・複数端末同時pullは未サポート）。"""
    from hub_core import connections as _conn, line as _line
    if not _conn.harness_configured():
        return {"ok": True, "op": "line_harness_pull", "configured": False, "received": 0,
                "note": "line-harness 未設定のため取込しません（Mockのまま）。"}
    state = _load_line_pull_state(data_dir)
    seen, cursor = state["seen"], state["cursor"]
    # 無フィルタ（送受信両方）ストリームへ移行: 旧 incoming-only 状態なら cursor を一度だけ0に戻して
    # 全再走査する。既存の incoming は seen dedupe で二重取込されず、過去の outgoing 履歴だけが新規に入る。
    migrating = state.get("stream_version", 1) < _LINE_PULL_STREAM_VERSION
    if migrating:
        cursor = ""
    feed = _conn.harness_pull_feed(cursor=cursor, direction="", limit=limit, max_pages=max_pages)
    if not feed.get("ok"):
        _LINE_PULL_LOG.warning("harness pull feed failed (nothing written): %s", feed.get("detail"))
        return {"ok": False, "op": "line_harness_pull", "configured": True, "received": 0,
                "error": feed.get("detail"), "status": feed.get("status"),
                "retry_after": feed.get("retry_after")}
    new_events, new_ids = [], []
    for ev in _line.normalize_feed_items(feed.get("items") or []):
        mid = ev.get("harness_msg_id")
        if mid and mid not in seen and mid not in new_ids:
            new_events.append(ev)
            new_ids.append(mid)
    new_cursor = feed.get("next_cursor") or cursor
    if new_events:
        # 単一書込経路（line_receive）へ委譲＝正規化・役割ゲート・監査を温存。書込成功後にのみ状態を永続
        # （書込前にseen/cursor更新すると書込失敗時にメッセージを取りこぼす＝取込漏れを防ぐ順序）。
        line_receive(data_dir, {"events": new_events}, actor, role)
        seen.update(new_ids)
        _save_line_pull_state(data_dir, seen, new_cursor)
    elif new_cursor != cursor or migrating:
        # 新規は無いが cursor が前進、または移行の完了を永続（stream_version を現行版に上げて再移行を防ぐ）。
        _save_line_pull_state(data_dir, seen, new_cursor)
    return {"ok": True, "op": "line_harness_pull", "configured": True, "received": len(new_events),
            "pages": feed.get("pages", 0), "migrated": migrating,
            "note": "harnessから会話を取り込みました。" if new_events else "新規のメッセージはありませんでした。"}


def line_harness_pull(data_dir, params, actor, role):
    """UIの「harnessから取込」ボタン＝pull型着信取込を1周実行（poll_once・役割ゲート＋監査）。
    未設定なら no-op（received=0）。認証/接続失敗は台帳に何も書かない（fail-closed）。"""
    _check_role("line_harness_pull", role)
    return poll_once(data_dir, actor=actor, role=role)


# ---- 案内可否確認（LINE着信のポータルURL→物確ドラフト・未確認→回答済） ----
# 顧客が他社ポータル（SUUMO/athome/HOMES等）の物件URLをLINEに貼って「案内できますか」と聞く反響を、
# 既存の物確（bukkaku）回答shape（status/viewing）に接続する。物件名/条件はテキストから構造化しない
# （＝URLと生テキストだけを台帳へ・捏造しない）。可否は担当が確定した値のみ（未確認→回答済）。
_INQUIRY_AVAIL = {"可", "要連絡", "不可"}                 # build_bukkaku_fax の「内見/紹介」欄と同じ語彙
_INQUIRY_STATUS = {"取扱中", "商談中", "成約済", "取扱終了"}  # 物確「現在の状況」欄と同じ語彙


def load_inquiries(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "inquiry_ledger.jsonl"))


def _inquiry_src_key(to_user, raw_text):
    """着信（相手UUID＋生テキスト）→安定キー。時刻を含めない＝同一着信への二重作成を冪等化する
    （担当のワンクリックが二度発火しても1件）。表示側も同じキーで「作成済み」を判定する。"""
    return hashlib.sha256(f"{to_user}|{(raw_text or '').strip()}".encode("utf-8")).hexdigest()[:16]


def _save_inquiry(data_dir, rec):
    with _FAX_LOCK:
        rows = [r for r in load_inquiries(data_dir) if r.get("inquiry_id") != rec.get("inquiry_id")]
        rows.append(rec)
        _write_jsonl_rows(_fax_jsonl(data_dir, "inquiry_ledger.jsonl"), rows)


def inquiry_create(data_dir, params, actor, role):
    """LINE着信（friend UUID・生テキスト）から「案内可否確認」レコードを作成（未確認）。可逆（下書き）。
    - URL は**生テキストから再抽出**（＝必ず本文の部分文字列・クライアント値を信用しない・捏造しない）。
    - 物件名/条件はテキストから構造化しない（URLと生テキストだけを運ぶ）。
    - 既存の物確回答shape（status/viewing）に接続＝担当が可否を記入すると同型の answer で確定する。
    - 冪等: 同一着信（相手＋本文）は再作成せず既存を返す（二重クリック/再取込でも1件）。"""
    _check_role("inquiry_create", role)
    from hub_core import line as _line
    to_user = (params.get("to_user") or "").strip()
    raw = str(params.get("text") or params.get("raw_text") or "")
    intent = _line.inquiry_intent(raw)
    if not intent["is_inquiry"]:
        raise OpError(400, "案内可否の問い合わせ（URL または案内可否の語彙）が見つかりません。")
    src_key = _inquiry_src_key(to_user, raw)
    existing = next((r for r in load_inquiries(data_dir) if r.get("src_key") == src_key), None)
    if existing is not None:   # 冪等: 同一着信は再作成しない（作成済みを返す）
        return {"ok": True, "op": "inquiry_create", "inquiry_id": existing.get("inquiry_id"),
                "status": existing.get("status"), "created": False,
                "note": "この着信の案内可否確認は作成済みです。", "link": "/line"}
    iid = "INQ-" + hashlib.sha256(f"{to_user}|{raw}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    rec = {"inquiry_id": iid, "src_key": src_key, "to_user": to_user, "raw_text": raw.strip(),
           "urls": intent["urls"], "status": "未確認", "answer": {},
           "case_id": (params.get("case_id") or "").strip(),
           "created_at": _now(), "source_tool": "inquiry_create"}
    _save_inquiry(data_dir, rec)
    _audit(data_dir, _event(actor, "inquiry_created", iid, "未確認", to=to_user,
                            urls=str(len(intent["urls"]))))
    return {"ok": True, "op": "inquiry_create", "inquiry_id": iid, "status": "未確認",
            "created": True, "urls": intent["urls"],
            "note": "案内可否確認を作成しました（未確認）。元付へ物確のうえ、可否を記入してください。",
            "link": "/line"}


def _inquiry_reply_text(avail, pstatus, note):
    """顧客への案内可否回答の定型文（決定論生成・弱いLLMに書かせない・物件詳細は捏造しない）。"""
    head = {"可": "お問い合わせの物件、ご案内可能です。",
            "要連絡": "お問い合わせの物件について確認中です。追ってご連絡いたします。",
            "不可": "お問い合わせの物件は、現在ご案内が難しい状況です。"}.get(avail, "")
    tail = "ご希望でしたら内見のご予約を承ります。" if avail == "可" else ""
    body = " ".join(x for x in (head, note, tail) if x)
    return body or "お問い合わせありがとうございます。担当よりご連絡いたします。"


def inquiry_resolve(data_dir, params, actor, role):
    """案内可否確認に担当が可否を記入（未確認→回答済）＋顧客への回答を line_send(queued) でドラフト化。可逆。
    - availability（可/要連絡/不可）は必須・property_status（取扱中/商談中/成約済/取扱終了）は任意＝担当が確定した値のみ。
    - answer は物確回答と同型（viewing＝案内可否・status＝空室状況）＝既存の物確データ形に接続。
    - 顧客回答は実送信しない＝outbox(queued)＝送信ゲート（line_confirm_send）を通す。物件詳細は捏造しない。"""
    _check_role("inquiry_resolve", role)
    iid = (params.get("inquiry_id") or "").strip()
    rec = next((r for r in load_inquiries(data_dir) if r.get("inquiry_id") == iid), None)
    if rec is None:
        raise OpError(404, f"案内可否確認 {iid} が見つかりません。")
    avail = (params.get("availability") or "").strip()
    if avail not in _INQUIRY_AVAIL:
        raise OpError(400, "availability（案内可否）は 可/要連絡/不可 のいずれかが必要です。")
    pstatus = (params.get("property_status") or "").strip()
    if pstatus and pstatus not in _INQUIRY_STATUS:
        raise OpError(400, "property_status（空室状況）は 取扱中/商談中/成約済/取扱終了 のいずれかです。")
    note = (params.get("note") or "").strip()
    rec["answer"] = {"viewing": avail, **({"status": pstatus} if pstatus else {}),
                     **({"note": note} if note else {})}
    rec["status"] = "回答済"
    rec["resolved_at"] = _now()
    rec["resolved_by"] = actor
    _save_inquiry(data_dir, rec)
    _audit(data_dir, _event(actor, "inquiry_resolved", iid, "回答済",
                            availability=avail, status=pstatus))
    # 顧客への回答を line_send(queued) でドラフト化（実送信は送信ゲート）。可否だけを載せ、物件詳細は捏造しない。
    draft_msg = None
    to_user = rec.get("to_user") or ""
    if to_user:
        r = line_send(data_dir, {"to_user": to_user, "kind": "push",
                                 "text": _inquiry_reply_text(avail, pstatus, note)}, actor, role)
        draft_msg = r.get("msg_id")
    return {"ok": True, "op": "inquiry_resolve", "inquiry_id": iid, "status": "回答済",
            "availability": avail, "draft_msg_id": draft_msg,
            "note": "可否を記入しました（回答済）。顧客への回答は送信待ちに積みました（送信ゲート経由）。",
            "link": "/line"}


# ---- 希望条件ヒアリング（LIFF条件フォーム着信→希望条件レコードを台帳化・担当が提案に使う） ----
# 顧客が LIFF の条件オートヒアリング（賃貸/購入→エリア・予算・間取り/種別・時期・こだわり）を
# トークに送ると「物件さがしの希望です。」＋ラベル行として着信する。担当がワンクリックで
# 希望条件レコードを台帳化し、条件に合う物件の提案に使う。物件は捏造しない（提案は担当が別途行う）。
def load_hearings(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "hearing_ledger.jsonl"))


def _hearing_src_key(to_user, receipt, raw_text):
    """着信→安定キー（冪等）。受付番号(hr_xxx)があればそれを優先（LIFF が採番＝再取込でも一意）。
    無ければ相手UUID＋生テキストで固定（時刻を含めない＝二重クリック/再取込でも1件）。
    表示側も同じキーで「台帳済み」を判定する。"""
    seed = ("hr|" + receipt) if receipt else (str(to_user or "") + "|" + str(raw_text or "").strip())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _save_hearing(data_dir, rec):
    with _FAX_LOCK:
        rows = [r for r in load_hearings(data_dir) if r.get("hearing_id") != rec.get("hearing_id")]
        rows.append(rec)
        _write_jsonl_rows(_fax_jsonl(data_dir, "hearing_ledger.jsonl"), rows)


def hearing_create(data_dir, params, actor, role):
    """LINE着信（LIFF条件ヒアリング）から「希望条件」レコードを台帳化。可逆（下書き）。
    - 希望条件は**生テキストから再パース**（hearing_intent）＝既知ラベル行のみ・値の推測補完をしない。
    - ラベルに無い行（お名前/電話/自由文の続き等）は fields に入れないが、生テキストは raw_text に残す
      （担当が本文をそのまま読める＝取りこぼさない）。
    - 冪等: 同一着信（受付番号 or 相手＋本文）は再作成せず既存を返す（二重クリック/再取込でも1件）。"""
    _check_role("hearing_create", role)
    from hub_core import line as _line
    to_user = (params.get("to_user") or "").strip()
    raw = str(params.get("text") or params.get("raw_text") or "")
    intent = _line.hearing_intent(raw)
    if not intent["is_hearing"]:
        raise OpError(400, "希望条件のヒアリング（「物件さがしの希望です」で始まる送信）が見つかりません。")
    src_key = _hearing_src_key(to_user, intent["receipt"], raw)
    existing = next((r for r in load_hearings(data_dir) if r.get("src_key") == src_key), None)
    if existing is not None:   # 冪等: 同一着信は再作成しない（台帳済みを返す）
        return {"ok": True, "op": "hearing_create", "hearing_id": existing.get("hearing_id"),
                "status": existing.get("status"), "created": False,
                "note": "この希望条件は台帳済みです。", "link": "/line"}
    hid = "HR-" + hashlib.sha256(f"{to_user}|{raw}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
    rec = {"hearing_id": hid, "src_key": src_key, "to_user": to_user,
           "receipt": intent["receipt"], "mode": intent["mode"], "fields": intent["fields"],
           "raw_text": raw.strip(), "status": "受付", "case_id": (params.get("case_id") or "").strip(),
           "created_at": _now(), "source_tool": "hearing_create"}
    _save_hearing(data_dir, rec)
    _audit(data_dir, _event(actor, "hearing_created", hid, "受付", to=to_user,
                            mode=intent["mode"] or "未指定", fields=str(len(intent["fields"]))))
    return {"ok": True, "op": "hearing_create", "hearing_id": hid, "status": "受付",
            "created": True, "mode": intent["mode"], "fields": intent["fields"],
            "note": "希望条件を台帳化しました（受付）。条件に合う物件のご提案にお使いください。",
            "link": "/line"}


# ==================== 物確自動応答（telephony・IVR着信→状態応答→自動FAX返信） ====================
def load_caller_directory(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "caller_directory.jsonl"))


def resolve_caller(data_dir, from_key):
    """電話番号(正規化キー)→業者名。自社台帳のみ（信頼できる無料逆引きAPIは無い・スクレイピングしない）。"""
    fk = str(from_key or "")
    for r in load_caller_directory(data_dir):
        if r.get("from_key") == fk:
            return r.get("company") or ""
    return ""


def load_bukkaku_codes(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "bukkaku_codes.jsonl"))


def resolve_case_by_code(data_dir, code):
    c = str(code or "").strip()
    for r in load_bukkaku_codes(data_dir):
        if r.get("code") == c:
            return r.get("case_id") or ""
    return ""


def _property_status(data_dir, case_id):
    """物件の物確状態（取扱中/商談中/成約済/取扱終了）。property_info の bukkaku_status。
    **未設定なら空を返す**（既定で「取扱中」と断定しない＝捏造しない・空はIVRが担当転送に落とす）。"""
    from hub_core import property_info as _pi
    info = _pi.load_property_info(data_dir, case_id)
    return str(info.get("bukkaku_status") or "").strip()


def load_calls(data_dir):
    return _read_jsonl_rows(_fax_jsonl(data_dir, "calls.jsonl"))


def caller_directory_add(data_dir, params, actor, role):
    """発信者台帳に 電話番号→業者名 を登録（caller-ID→業者特定を自社で蓄積）。可逆。"""
    _check_role("caller_directory_add", role)
    from hub_core import telephony as _tel
    num = (params.get("number") or "").strip()
    company = (params.get("company") or "").strip()
    if not num or not company:
        raise OpError(400, "number と company が必要です。")
    fk = _tel.normalize_number(num)
    with _FAX_LOCK:
        rows = [r for r in load_caller_directory(data_dir) if r.get("from_key") != fk]
        rows.append({"from_key": fk, "number": num, "company": company,
                     "note": (params.get("note") or "").strip(), "added_at": _now()})
        _write_jsonl_rows(_fax_jsonl(data_dir, "caller_directory.jsonl"), rows)
    _audit(data_dir, _event(actor, "caller_dir_add", fk, "recorded", company=company))
    return {"ok": True, "op": "caller_directory_add", "company": company, "from_key": fk}


def bukkaku_code_assign(data_dir, params, actor, role):
    """物件に物確番号（業者向けマイソクに載せる短い番号）を割当。業者が電話でダイヤル入力→物件特定。"""
    _check_role("bukkaku_code_assign", role)
    cid = (params.get("case_id") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    code = (params.get("code") or "").strip()
    if not code:
        code = str(int(hashlib.sha256(cid.encode("utf-8")).hexdigest(), 16) % 900000 + 100000)  # 6桁
    with _FAX_LOCK:
        rows = [r for r in load_bukkaku_codes(data_dir) if r.get("case_id") != cid and r.get("code") != code]
        rows.append({"code": code, "case_id": cid, "assigned_at": _now()})
        _write_jsonl_rows(_fax_jsonl(data_dir, "bukkaku_codes.jsonl"), rows)
    _audit(data_dir, _event(actor, "bukkaku_code_assign", code, "recorded", case=cid))
    return {"ok": True, "op": "bukkaku_code_assign", "code": code, "case_id": cid}


def call_receive(data_dir, params, actor, role):
    """着信電話（正規化済）→ 発信者特定＋物確番号→物件＋状態応答を組み、通話ログに記録。
    詳細希望かつ発信者FAXありなら物確/マイソクFAXの自動送信を outbox に積む（実送信は人間ゲート）。捏造しない。"""
    _check_role("call_receive", role)
    from hub_core import telephony as _tel, property_info as _pi, fax as _fax
    call = params.get("call") if isinstance(params.get("call"), dict) else {}
    call_id = _provider_delivery_id(call.get("call_id") or call.get("delivery_id"), field="call_id")
    provider_delivery_id = _provider_delivery_id(call.get("delivery_id") or call_id,
                                                 field="delivery_id")
    delivery_key = _delivery_key("telephony", provider_delivery_id)
    with _FAX_LOCK:
        existing = next((row for row in load_calls(data_dir)
                         if row.get("delivery_key") == delivery_key
                         or (not row.get("delivery_key") and
                             (row.get("provider_delivery_id") == provider_delivery_id
                              or row.get("call_id") == call_id))), None)
    if existing is not None:
        return {"ok": True, "op": "call_receive", "company": existing.get("company") or "(未登録)",
                "case_id": existing.get("case_id") or "", "status": existing.get("status") or "",
                "say": existing.get("say") or "", "action": existing.get("action") or "",
                "fax_job": existing.get("fax_job") or ""}
    from_key = call.get("from_key") or _tel.normalize_number(call.get("from_number", ""))
    company = resolve_caller(data_dir, from_key)
    code = call.get("digits") or ""
    cid = resolve_case_by_code(data_dir, code) if code else ""
    pname, status, resp = "", "", {}
    fax_job = ""
    if cid:
        info = _pi.load_property_info(data_dir, cid)
        pname = str(info.get("property_name") or cid)
        status = _property_status(data_dir, cid)
        want_details = str(call.get("intent") or "").strip() == "details"
        import re as _re
        caller_fax_num = str(params.get("caller_fax") or "").strip()
        has_valid_fax = bool(_re.match(r"^[0-9+\-() ]{6,20}$", caller_fax_num))
        resp = _tel.build_ivr_response(property_name=pname, status=status,
                                       want_details=want_details, caller_has_fax=has_valid_fax)
        if resp.get("fax") and has_valid_fax:
            try:   # webhook由来の番号なので防御的に（不正番号でのFaxError等でcall_receiveを落とさない）
                r = bukkaku_send(data_dir, {"case_id": cid, "to_number": caller_fax_num,
                                            "idempotency_key": delivery_key}, actor, role)
                fax_job = r.get("job_id", "")
            except (OpError, _fax.FaxError):
                fax_job = ""
    rec = {"call_id": call_id, "provider_delivery_id": provider_delivery_id,
           "delivery_key": delivery_key, "from_number": call.get("from_number", ""),
           "from_key": from_key, "company": company, "code": code, "case_id": cid,
           "property_name": pname, "status": status, "action": resp.get("action", ""),
           "say": resp.get("say", ""), "fax_job": fax_job,
           "received_at": call.get("received_at") or _now(), "recorded_at": _now()}
    with _FAX_LOCK:
        rows = load_calls(data_dir)
        rows.append(rec)
        _write_jsonl_rows(_fax_jsonl(data_dir, "calls.jsonl"), rows)
    _audit(data_dir, _event(actor, "call_received", call_id, "received",
                            provider_delivery_id=provider_delivery_id, company=company,
                            case=cid, ivr_action=resp.get("action", "")))
    return {"ok": True, "op": "call_receive", "company": company or "(未登録)", "case_id": cid,
            "status": status, "say": resp.get("say", ""), "action": resp.get("action", ""),
            "fax_job": fax_job}


def property_status_set(data_dir, params, actor, role):
    """物件の物確状態（取扱中/商談中/成約済/取扱終了）を設定。IVR自動応答が返す状態はこれに基づく。可逆。"""
    _check_role("property_status_set", role)
    from hub_core import property_info as _pi
    cid = (params.get("case_id") or "").strip()
    status = (params.get("status") or "").strip()
    valid = {"取扱中", "商談中", "成約済", "取扱終了"}
    if not cid or status not in valid:
        raise OpError(400, f"case_id と status（{'/'.join(sorted(valid))}）が必要です。")
    _pi.set_property_fields(data_dir, cid, {"bukkaku_status": status})
    _audit(data_dir, _event(actor, "property_status_set", cid, status))
    return {"ok": True, "op": "property_status_set", "case_id": cid, "status": status}


def reins_prepare(data_dir, params, actor, role):
    """REINS登録の準備: 物件レコード→入稿シート生成＋法定登録期限を算出して物件に記録。
    **REINSには一切アクセスしない**（スクレイピング/自動登録しない）。登録は会員が手動で行う前提。可逆(下書き)。"""
    _check_role("reins_prepare", role)
    import datetime as _d
    from hub_core import reins as _reins, property_info as _pi, documents as _docs
    cid = (params.get("case_id") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    mediation = (params.get("mediation") or "").strip()
    contract_date = (params.get("contract_date") or "").strip()
    fields = _pi.load_property_info(data_dir, cid)
    if not fields:
        raise OpError(404, f"物件レコードがありません（先に書類を集めてください）: {cid}")
    sheet = _reins.build_reins_sheet(fields, mediation=mediation, contract_date=contract_date,
                                     today=_d.date.today().isoformat())
    doc_id = "REINS-" + cid
    _docs.save_version(data_dir, doc_id, sheet, kind="reins", fmt="md", author="あいのて(REINS入稿準備)")
    dl = _reins.reins_deadline(mediation, contract_date)
    _pi.set_property_fields(data_dir, cid, {
        "reins_status": "準備", "reins_mediation": mediation,
        "reins_deadline": dl.get("deadline", "") if dl.get("required") else "",
        "reins_required": "1" if dl.get("required") else "0"})
    _audit(data_dir, _event(actor, "reins_prepared", cid, "準備",
                            deadline=dl.get("deadline", ""), mediation=mediation))
    return {"ok": True, "op": "reins_prepare", "case_id": cid, "doc_id": doc_id,
            "required": dl.get("required", False), "deadline": dl.get("deadline", ""),
            "note": "REINS入稿シートを作成しました。REINSには送信していません。会員画面で登録し、"
                    "登録後に reins_record でREINS番号を記録してください。"}


def reins_record(data_dir, params, actor, role):
    """会員がREINS登録した後、REINS物件番号をあいのてに記録し登録済にする（案件/マイソク/重説と紐付け）。"""
    _check_role("reins_record", role)
    from hub_core import property_info as _pi
    cid = (params.get("case_id") or "").strip()
    reins_no = (params.get("reins_no") or "").strip()
    if not cid or not reins_no:
        raise OpError(400, "case_id と reins_no（REINS物件番号）が必要です。")
    _pi.set_property_fields(data_dir, cid, {"reins_status": "登録済", "reins_no": reins_no})
    _audit(data_dir, _event(actor, "reins_recorded", cid, "登録済", reins_no=reins_no))
    return {"ok": True, "op": "reins_record", "case_id": cid, "reins_no": reins_no, "status": "登録済"}


def moveout_settle(data_dir, params, actor, role):
    """退去精算（敷金精算）: 敷金 − 原状回復費 − 未払賃料 = 返還額（下限0）。精算書ドラフトを保存。
    金銭は人間確認後に実行。実返金はしない（ドラフト＝可逆）。"""
    _check_role("moveout_settle", role)
    cid = (params.get("case_id") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    deposit = _parse_amount(params.get("deposit") or "0")
    restoration = _parse_amount(params.get("restoration") or "0")
    unpaid = _parse_amount(params.get("unpaid_rent") or "0")
    refund = max(0, deposit - restoration - unpaid)
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (cid,))
    name = (cases[0].get("顧客名") or cases[0].get("customer_name") or "") if cases else ""
    prop = (cases[0].get("物件名") or "") if cases else ""
    lines = [
        "# 退去精算書（敷金精算）", "",
        "> 本書面は敷金精算の**下書き**です。原状回復費の範囲・金額は賃借人と協議のうえ確定してください"
        "（国交省「原状回復をめぐるトラブルとガイドライン」参照）。実際の返金は確認後に行います。", "",
        f"- 案件: {cid}", f"- 物件: {prop}", f"- 賃借人: {name or '＿＿＿＿＿＿'}", "",
        "| 項目 | 金額 |", "|---|---|",
        f"| 預り敷金 | ¥{deposit:,} |",
        f"| 原状回復費（協議） | −¥{restoration:,} |",
        f"| 未払賃料等 | −¥{unpaid:,} |",
        f"| **返還額** | **¥{refund:,}** |", "",
        "- ☐ 原状回復費の内訳（明細）を添付",
        "- ☐ 返還先口座を確認",
        "- ☐ 賃借人の同意（記名）を得てから返金",
    ]
    from hub_core import documents as _docs
    did = "SEISAN-" + hashlib.sha256(f"{cid}|{_now()}".encode("utf-8")).hexdigest()[:8].upper()
    _docs.save_version(data_dir, did, "\n".join(lines), kind="seisan", fmt="md",
                       author="あいのて(退去精算)")
    _audit(data_dir, _event(actor, "moveout_settled", did, "money", case=cid, refund=str(refund)))
    return {"ok": True, "op": "moveout_settle", "deposit": deposit, "restoration": restoration,
            "unpaid_rent": unpaid, "refund": refund, "doc_id": did,
            "note": "敷金精算の下書きを作成しました（実返金は確認後）。"}


def reconcile_deposits(data_dir, params, actor, role):
    """全銀ファイル（Vault内）を読み込み、未消込の請求と突合して候補を返す（読取専用・G3）。
    自動消込は金額+名義一致のみ・部分一致は要確認へ（誤消込防止）。実際の消込確定は billing_reconcile で。"""
    _check_role("reconcile_deposits", role)
    from hub_core import zengin as _zg
    src = (params.get("source") or "").strip()
    try:
        deposits = _zg.load_zengin_file(data_dir, src) if src else []
    except ValueError as e:
        raise OpError(400, str(e))
    st = _store(data_dir)
    # 未消込の請求（kind=請求・状態が消込済でない）
    invoices = []
    for r in st.query("billing_register", "kind = ?", ("請求",)):
        if (r.get("status") or "").strip() in ("消込済", "入金消込"):
            continue
        try:
            amt = int(str(r.get("amount") or "0").replace(",", "").replace("円", "") or "0")
        except ValueError:
            amt = 0
        invoices.append({"invoice_id": r.get("billing_id"), "payer": r.get("customer_name") or "", "amount": amt})
    result = _zg.reconcile(deposits, invoices)
    return {"ok": True, "op": "reconcile_deposits", "deposits": len(deposits),
            "invoices": len(invoices), **result}


def overdue_reminders(data_dir, params, actor, role):
    """未消込の請求で期日超過を検出し督促ドラフトを生成（Tier2・ドラフト止まり=可逆・実送信しない）。
    期日=請求作成からdue_days日後（既定30）。today決定的。督促の実送信はM-sender人間ゲート。"""
    _check_role("overdue_reminders", role)
    import datetime as _dt
    today = (params.get("today") or _dt.date.today().isoformat()).strip()
    try:
        _dt.date.fromisoformat(today)
    except ValueError:
        raise OpError(400, "today は YYYY-MM-DD 形式で入力してください。")
    try:
        due_days = int(params.get("due_days") or 30)
    except (ValueError, TypeError):
        raise OpError(400, "due_days は整数で入力してください。")
    if due_days < 0:
        raise OpError(400, "due_days は0以上で入力してください。")
    st = _store(data_dir)
    try:
        bills = st.query("billing_register", "kind = ?", ("請求",))
    except Exception:
        bills = []
    overdue, created = [], []
    for b in bills:
        if (b.get("status") or "").strip() in ("消込済", "入金消込"):
            continue
        created_d = str(b.get("created_at") or "")[:10]
        try:
            due_date = (_dt.date.fromisoformat(created_d) + _dt.timedelta(days=due_days))
            days_over = (_dt.date.fromisoformat(today) - due_date).days
        except ValueError:
            continue
        if days_over <= 0:      # 期日当日(=0)はまだ延滞でない
            continue
        overdue.append({"billing_id": b.get("billing_id"), "amount": b.get("amount"),
                        "customer": b.get("customer_name"), "days_over": days_over})
    for o in overdue[:50]:
        body = (f'{o["customer"]}様　ご請求（{o["amount"]}円）のご入金が確認できておりません。'
                f'行き違いの場合はご容赦ください。ご確認をお願いいたします。')
        r = message_draft(data_dir, {"to": o.get("billing_id") or "", "subject": "ご入金確認のお願い",
                                     "body": body, "channel": "email", "ref": o.get("billing_id") or ""},
                          actor, role)
        created.append({"billing_id": o["billing_id"], "message_id": r["message_id"],
                        "days_over": o["days_over"]})
    return {"ok": True, "op": "overdue_reminders", "overdue": len(overdue), "drafted": len(created),
            "drafts": created, "note": "督促ドラフトを生成しました（可逆）。送信は送信ゲート（人間確認）を通ります。"}


def billing_reconcile(data_dir, params, actor, role):
    """請求を入金消込にする（金銭=人間が候補を確認した上で1件ずつ確定・可逆でない状態遷移）。
    実送金は行わない＝入金の記録・消込のみ（全銀取込値に基づく）。"""
    _check_role("billing_reconcile", role)
    bid = (params.get("billing_id") or "").strip()
    if not bid:
        raise OpError(400, "billing_id が必要です。")
    st = _store(data_dir)
    rows = st.query("billing_register", "billing_id = ?", (bid,))
    if not rows:
        raise OpError(404, f"請求 {bid} が見つかりません。")
    if (rows[0].get("status") or "").strip() in ("消込済", "入金消込"):
        raise OpError(409, f"請求 {bid} は既に消込済みです。")
    st.update_row("billing_register", "billing_id", bid, {"status": "消込済"})
    _audit(data_dir, _event(actor, "billing_reconciled", bid, "money",
                            amount=rows[0].get("amount"), customer=rows[0].get("customer_name")))
    return {"ok": True, "op": "billing_reconcile", "billing_id": bid, "status": "消込済", "link": "/money"}


def stage_advance(data_dir, params, actor, role):
    """顧客ジャーニーのステージを前進(deal_type別の細粒度ステージ)。due_at自動設定・5段階statusへ集約。"""
    _check_role("stage_advance", role)
    cid = (params.get("case_id") or "").strip()
    to = (params.get("to_stage") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (cid,))
    if not cases:
        raise OpError(404, f"案件 {cid} が見つかりません。")
    if (cases[0].get("status") or "") == "失注":
        raise OpError(409, "失注済みの案件です(ステージ前進不可)。")
    deal = cases[0].get("deal_type") or "lease_tenant"
    stages = stages_for(deal)
    jr = st.query("customer_journey", "case_id = ?", (cid,))
    cur = (jr[0].get("stage") if jr else "") or stages[0]
    if not to:  # 指定なければ次段階へ
        ci = stages.index(cur) if cur in stages else -1
        if ci + 1 >= len(stages):
            raise OpError(409, "最終ステージです。")
        to = stages[ci + 1]
    if to not in stages:
        raise OpError(400, f"{deal} に無いステージ: {to}")
    due = (datetime.now(JST) + timedelta(days=STAGE_DUE_DAYS.get(to, 3))).date().isoformat()
    now = _now()
    if jr:
        st.update_row("customer_journey", "journey_id", jr[0]["journey_id"],
                      {"stage": to, "entered_at": now, "due_at": due})
    else:
        jid = "JR-" + hashlib.sha256(f"{cid}|{now}".encode("utf-8")).hexdigest()[:12].upper()
        track = "buyer" if deal == "sale_buyer" else ("seller" if deal == "sale_seller" else "tenant")
        st.insert_row("customer_journey", _full_row("customer_journey", {
            "journey_id": jid, "case_id": cid, "deal_track": track, "stage": to,
            "entered_at": now, "due_at": due}))
    st.update_row("cases", "case_id", cid, {"status": aggregate_status(to)})
    _audit(data_dir, _event(actor, "stage_advanced", cid, to, from_stage=cur, to_stage=to, case=cid))
    return {"ok": True, "op": "stage_advance", "case_id": cid, "from": cur, "to": to,
            "due": due, "link": f"/timeline?id={cid}"}


def case_lose(data_dir, params, actor, role):
    """案件を失注として記録(どのステージからでも・理由は選択式で必須)。
    cases を太らせず lost_records 分離テーブルへ。理由の蓄積が仕入れ・掲載改善の源泉。"""
    _check_role("case_lose", role)
    cid = (params.get("case_id") or "").strip()
    reason = (params.get("reason") or "").strip()
    note = (params.get("note") or "").strip()
    if not cid:
        raise OpError(400, "case_id が必要です。")
    if reason not in LOST_REASONS:
        raise OpError(400, "失注理由は選択式です: " + "／".join(LOST_REASONS))
    st = _store(data_dir)
    cases = st.query("cases", "case_id = ?", (cid,))
    if not cases:
        raise OpError(404, f"案件 {cid} が見つかりません。")
    if (cases[0].get("status") or "") == "失注" or st.query("lost_records", "case_id = ?", (cid,)):
        raise OpError(409, "既に失注記録があります。")
    jr = st.query("customer_journey", "case_id = ?", (cid,))
    stage = (jr[0].get("stage") if jr else "") or (cases[0].get("status") or "")
    now = _now()
    lid = "LOST-" + hashlib.sha256(f"{cid}|{now}".encode("utf-8")).hexdigest()[:12].upper()
    st.insert_row("lost_records", _full_row("lost_records", {
        "lost_id": lid, "case_id": cid, "customer_id": cases[0].get("customer_id") or "",
        "deal_type": normalize_deal_type(cases[0].get("deal_type") or ""),
        "lost_stage": stage, "lost_reason": reason, "note": note, "lost_at": now, "actor": actor}))
    st.update_row("cases", "case_id", cid, {"status": "失注"})
    _audit(data_dir, _event(actor, "case_lost", cid, "失注",
                            reason=reason, stage=stage, case=cid))
    return {"ok": True, "op": "case_lose", "case_id": cid, "reason": reason,
            "stage": stage, "link": f"/timeline?id={cid}"}


def attribute_update(data_dir, params, actor, role):
    """顧客属性を追加/更新(customer_id×field_keyでupsert)。"""
    _check_role("attribute_update", role)
    cust = (params.get("customer_id") or "").strip()
    key = (params.get("field_key") or "").strip()
    val = (params.get("field_value") or "").strip()
    cat = (params.get("category") or "その他").strip()
    if not cust or not key:
        raise OpError(400, "customer_id と field_key が必要です。")
    st = _store(data_dir)
    _require_customer(st, cust)
    now = _now()
    ex = st.query("customer_attributes", "customer_id = ? AND field_key = ?", (cust, key))
    if ex:
        st.update_row("customer_attributes", "attr_id", ex[0]["attr_id"],
                      {"field_value": val, "category": cat, "updated_at": now})
    else:
        aid = "AT-" + hashlib.sha256(f"{cust}|{key}|{now}".encode("utf-8")).hexdigest()[:12].upper()
        st.insert_row("customer_attributes", _full_row("customer_attributes", {
            "attr_id": aid, "customer_id": cust, "field_key": key, "field_value": val,
            "category": cat, "updated_at": now}))
    _audit(data_dir, _event(actor, "attribute_updated", cust, key, field=key))
    back = (params.get("back") or "").strip()
    return {"ok": True, "op": "attribute_update", "customer_id": cust, "field_key": key,
            "link": back or "/customers"}


def contact_log_add(data_dir, params, actor, role):
    """接触履歴を記録し、journey.last_contact_at を更新(滞留検知のリセット)。"""
    _check_role("contact_log_add", role)
    cust = (params.get("customer_id") or "").strip()
    cid = (params.get("case_id") or "").strip()
    channel = (params.get("channel") or "").strip()
    summary = (params.get("summary") or "").strip()
    reaction = (params.get("reaction") or "").strip()
    if not cust or not channel:
        raise OpError(400, "customer_id と channel(接触手段)が必要です。")
    st = _store(data_dir)
    _require_customer(st, cust)
    if cid:
        cases = st.query("cases", "case_id = ?", (cid,))
        if not cases:
            raise OpError(404, f"案件 {cid} が見つかりません。")
        if (cases[0].get("customer_id") or "").strip() != cust:
            raise OpError(409, f"案件 {cid} は顧客 {cust} の案件ではありません。")
    now = _now()
    ctid = "CT-" + hashlib.sha256(f"{cust}|{now}".encode("utf-8")).hexdigest()[:12].upper()
    st.insert_row("contact_log", _full_row("contact_log", {
        "contact_id": ctid, "customer_id": cust, "case_id": cid, "channel": channel,
        "occurred_at": now, "summary": summary, "reaction": reaction, "actor": actor}))
    if cid:
        jr = st.query("customer_journey", "case_id = ?", (cid,))
        if jr:
            st.update_row("customer_journey", "journey_id", jr[0]["journey_id"], {"last_contact_at": now})
    _audit(data_dir, _event(actor, "contact_logged", cust, channel, channel=channel, case=cid))
    return {"ok": True, "op": "contact_log_add", "customer_id": cust, "contact_id": ctid,
            "link": (f"/timeline?id={cid}" if cid else "/customers")}


def _load_message(data_dir, mid: str) -> dict | None:
    import json as _json
    ap = Path(data_dir) / "audit_log.jsonl"
    if not ap.is_file():
        return None
    latest = None
    for line in ap.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if ev.get("action") == "message_state" and ev.get("target") == mid:
            st = ev.get("msg_state")
            if isinstance(st, dict):
                latest = st
    return latest


def followup_generate(data_dir, params, actor, role):
    """追客が必要な案件に追客ドラフトを自動生成（Tier2・ドラフト止まり=可逆・実送信しない）。
    today は params から（決定的）。生成したドラフトは M-sender の送信ゲート（人間）を通る。"""
    _check_role("followup_generate", role)
    from hub_core import followup as _fu
    import datetime as _dt
    today = (params.get("today") or _dt.date.today().isoformat()).strip()
    due = _fu.due_followups(data_dir, today=today)
    st = _store(data_dir)
    created = []
    for d in due[:50]:
        # 宛先は顧客の連絡先を解決（無ければ未設定placeholder＝送信前に解決が要る）
        to = "(連絡先未設定)"
        cases = st.query("cases", "case_id = ?", (d["case_id"],))
        cust_id = cases[0].get("customer_id") if cases else None
        if cust_id:
            custs = st.query("customers", "customer_id = ?", (cust_id,))
            if custs and (custs[0].get("contact") or "").strip():
                to = custs[0]["contact"].strip()
        r = message_draft(data_dir, {"to": to, "subject": "ご連絡", "body": d["draft_body"],
                                     "channel": "email", "ref": d["case_id"]}, actor, role)
        created.append({"case_id": d["case_id"], "message_id": r["message_id"],
                        "days_since": d["days_since"]})
    return {"ok": True, "op": "followup_generate", "due": len(due), "drafted": len(created),
            "drafts": created, "note": "追客ドラフトを生成しました（可逆）。送信は送信ゲート（人間確認）を通ります。"}


def message_draft(data_dir, params, actor, role):
    """アウトバウンドのドラフトを作成（追客/一次返信・可逆・¥0）。実送信しない。"""
    _check_role("message_draft", role)
    from hub_core import sender as _snd
    import hashlib as _h
    to = (params.get("to") or "").strip()
    body = (params.get("body") or "").strip()
    mid = "MSG-" + _h.sha256(f"{to}|{_now()}".encode("utf-8")).hexdigest()[:12].upper()
    try:
        msg = _snd.new_message(mid, to, (params.get("subject") or "").strip(), body,
                               channel=(params.get("channel") or "email"),
                               ref=(params.get("ref") or "").strip())
    except _snd.SendError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "message_state", mid, "draft", msg_state=msg))
    return {"ok": True, "op": "message_draft", "message_id": mid, "status": "draft"}


def message_queue(data_dir, params, actor, role):
    """ドラフトを送信キューへ（draft→queued・可逆）。実送信はまだしない。"""
    _check_role("message_queue", role)
    from hub_core import sender as _snd
    mid = (params.get("message_id") or "").strip()
    msg = _load_message(data_dir, mid)
    if msg is None:
        raise OpError(404, f"メッセージ {mid} が見つかりません。")
    try:
        _snd.transition(msg, "queued")
    except _snd.SendError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "message_state", mid, "queued", msg_state=msg))
    return {"ok": True, "op": "message_queue", "message_id": mid, "status": "queued"}


def _build_sender_provider(data_dir, channel):
    """BYO送信プロバイダを組み立てる（未設定=None=MockSender fail-closed）。テストで差替え可能。"""
    try:
        from hub_core import sender_provider as _sp
        return _sp.build_sender_provider(data_dir, channel)
    except Exception:
        return None


def message_send(data_dir, params, actor, role):
    """メッセージを送信（BYO SMTP/LINE・人間ゲート）。未接続=Mock=実送信しない（delivered False）。
    実送信は**二要素**=接続済みプロバイダ AND 明示承認(confirm_send)のとき**だけ**（接続=承認と同一視しない）。"""
    _check_role("message_send", role)
    from hub_core import sender as _snd
    mid = (params.get("message_id") or "").strip()
    msg = _load_message(data_dir, mid)
    if msg is None:
        raise OpError(404, f"メッセージ {mid} が見つかりません。")
    existing_attempt = _send_attempt_for(data_dir, "message", mid)
    if existing_attempt is not None:
        if _attempt_is_committed(existing_attempt):
            return {"ok": True, "op": "message_send", "message_id": mid, "status": "sent",
                    "delivered": True, "external_id": existing_attempt.get("external_id", ""),
                    "note": "既にプロバイダ受理・監査確定済みです。再送していません。"}
        raise _attempt_block_error(existing_attempt)
    prov = _build_sender_provider(data_dir, msg.get("channel"))
    # 二要素ゲート: 接続済み AND 明示承認(confirm_send=actor直接指示)。接続だけでは実送信しない。
    connected = bool(getattr(prov, "connected", False))
    confirmed = _send_confirmation(params.get("confirm_send"))
    allow = connected and confirmed
    if connected and not confirmed:
        raise OpError(403, "実送信には明示の承認（confirm_send）が必要です（人間ゲート・接続=承認ではありません）。")
    attempt = None
    try:
        if connected:
            attempt = _reserve_send_attempt(data_dir, "message", mid, str(prov.name))
        res = _snd.send_message(
            msg, provider=prov, allow_real_send=allow,
            idempotency_key=str((attempt or {}).get("idempotency_key") or ""),
        )
    except _snd.SendError as e:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(e).__name__, provider_finished_at=_now())
        raise OpError(e.code, e.msg)
    except Exception as exc:
        if attempt is not None:
            _update_send_attempt(data_dir, attempt, state="unknown",
                                 error_type=type(exc).__name__, provider_finished_at=_now())
        raise
    attempt_state = _record_provider_result(
        data_dir, attempt, msg, sent_key="delivered") if attempt is not None else "mock"
    _audit(data_dir, _event(actor, "message_state", mid, msg["status"], msg_state=msg,
                            send_attempt_id=(attempt or {}).get("attempt_id", ""),
                            idempotency_key=(attempt or {}).get("idempotency_key", ""),
                            provider_outcome=msg.get("provider_outcome", "")))
    if attempt is not None:
        _commit_send_attempt_audit(data_dir, attempt)
        if attempt_state != "accepted":
            raise OpError(409, "外部メッセージの受理を確認できません。二重送信防止のため自動再送しません。"
                               "プロバイダ側を照会してください。")
    return {"ok": True, "op": "message_send", "message_id": mid, "status": msg["status"],
            "delivered": res["delivered"], "external_id": msg.get("external_id", ""),
            "note": res.get("note", "")}


def proposal_draft(data_dir, params, actor, role):
    """物件PR文/提案文の下書きを生成（Tier2・AI臭ガード必須・可逆）。BYO-LLM任意・既定テンプレ¥0。
    下書きのみ＝顧客送付はM-senderの人間ゲートを通る。"""
    _check_role("proposal_draft", role)
    from hub_core import proposal as _pr
    pid = (params.get("property_id") or "").strip()
    st = _store(data_dir)
    rows = st.query("properties", "property_id = ?", (pid,)) if pid else []
    prop = rows[0] if rows else {k: params.get(k, "") for k in
                                 ("property_name", "address", "station", "walk_min", "layout",
                                  "area", "built_year", "structure", "pet")}
    # BYO-LLM generator は未接続なら None（テンプレ¥0）。実LLM呼出はBYO経路（サーバ直呼びしない）。
    res = _pr.build_proposal(prop, purpose=(params.get("purpose") or "pr"), generator=None)
    _audit(data_dir, _event(actor, "proposal_drafted", pid or "adhoc", "draft",
                            generated_by=res["generated_by"], hype=len(res["hype_flags"])))
    return {"ok": True, "op": "proposal_draft", **res}


def asset_attest(data_dir, params, actor, role):
    """Vault素材に自社(self)記名のprovenanceを発行（Tier2磨き・自社撮影/作成の宣言）。
    ※self=自分で撮った/作った宣言はサーバ検証不能＝記名責任＋監査追跡（設計の明示的裾）。"""
    _check_role("asset_attest", role)
    from hub_core import provenance as _pv
    if str(params.get("confirm_self") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise OpError(400, "自社で撮影または作成した素材であることの確認が必要です。")
    ref = (params.get("asset") or "").strip()   # asset_key（物件/…相対パス）
    f = _pv._resolve_vault_file(data_dir, ref)
    if f is None:
        raise OpError(404, "Vault内の素材が見つかりません（物件/<物件名>/素材/… に置いてください）。")
    allowed_rights = {"advertise", "portal", "obi_swap", "modify", "display"}
    rights = [r.strip() for r in (params.get("rights") or "advertise").split(",")
              if r.strip() in allowed_rights]
    if not rights:
        raise OpError(400, "素材の利用目的を選んでください。")
    try:
        rec = _pv.write_sidecar(f, origin="self", actor=actor, rights=rights)
    except _pv.ProvenanceError as e:
        raise OpError(e.code, e.msg)
    _audit(data_dir, _event(actor, "asset_attested", ref, "self", asset_sha256=rec["asset_sha256"]))
    return {"ok": True, "op": "asset_attest", "asset": ref, "origin": "self", "rights": rights}


def property_register(data_dir, params, actor, role):
    """物件をマスタ(properties)に登録/更新。PRS接続時は住所→災害リスクをcache(未接続なら空・捏造しない)。"""
    _check_role("property_register", role)
    addr = (params.get("address") or "").strip()
    pid = (params.get("property_id") or "").strip()
    if not addr and not pid:
        raise OpError(400, "address が必要です。")
    if not pid:
        pid = "PROP-" + hashlib.sha256((addr + _now()).encode("utf-8")).hexdigest()[:8].upper()
    risk = (params.get("risk_scores") or "").strip()
    prs_status = ""
    if not risk and addr:
        try:
            from . import prs as _prs
            if _prs.configured():
                res = _prs.assess(address=addr)
                risk = _prs.risk_summary(res)
                prs_status = res.get("prs_status", "")
                if res.get("prs_status") == "OK":
                    # 実呼出のみ従量記録（テナント=顧客/店舗。未接続や失敗は課金しない）
                    try:
                        from . import metering as _mtr
                        # tenant は認証コンテキスト（actor）から導出＝リクエスト入力での任意帰属を禁止（F1c是正）
                        _mtr.record_usage(data_dir, tenant=(actor or "self"),
                                          product="prs_assess", ref=pid)
                    except Exception:
                        pass  # 課金記録失敗で物件登録を妨げない（best-effort・後で再集計可）
        except Exception:
            pass
    row = {"property_id": pid, "address": addr, "deal_type": params.get("deal_type") or "",
           "rent_or_price": params.get("rent_or_price") or "", "layout": params.get("layout") or "",
           "area": params.get("area") or "", "built_year": params.get("built_year") or "",
           "structure": params.get("structure") or "", "station": params.get("station") or "",
           "walk_min": params.get("walk_min") or "", "pet": params.get("pet") or "",
           "source": params.get("source") or "自社", "risk_scores": risk, "status": "募集中"}
    st = _store(data_dir)
    if st.query("properties", "property_id = ?", (pid,)):
        st.update_row("properties", "property_id", pid, row)
    else:
        st.insert_row("properties", _full_row("properties", row))
    # 案件（cases）も作る。「物件」画面は案件を並べているので、マスタに入れるだけだと
    # 登録したのに一覧に出ない（利用者には登録できていないようにしか見えない）。
    # 案件があって初めて、その物件からマイソク・重説へ進める。
    name = (params.get("property_name") or "").strip() or addr
    case_id = "CASE-" + pid.replace("PROP-", "")
    if not st.query("cases", "case_id = ?", (case_id,)):
        st.insert_row("cases", _full_row("cases", {
            "case_id": case_id, "property_id": pid, "property_name": name,
            "deal_type": params.get("deal_type") or "", "status": "登録",
            "source_ref": pid, "source_tool": "property_register"}))
    _audit(data_dir, _event(actor, "property_registered", pid, "募集中",
                            address=addr, case=case_id))
    # 逆マッチング: 登録物件を既存顧客の希望条件で逆検索し先行提案候補を返す(CRMギャップp1)。
    # 提案エンジンの不調で物件登録自体を落とさない(best-effort・登録は成立済み)。
    try:
        from . import matching as _match
        rm = _match.match_customers(data_dir, row)
    except Exception:
        rm = {"candidates": [], "excluded": []}
    return {"ok": True, "op": "property_register", "property_id": pid, "case_id": case_id,
            "risk": risk or "(PRS未接続)", "prs_status": prs_status,
            "candidates": rm["candidates"], "ng_excluded": rm["excluded"], "link": "/properties"}


def requirement_check(data_dir, params, actor, role):
    """ステージ×書類の充足(present)を更新/追加。必須未充足は画面で朱表示=滞留検知(b)系。"""
    _check_role("requirement_check", role)
    cid = (params.get("case_id") or "").strip()
    kind = (params.get("doc_kind") or "").strip()
    present = "1" if str(params.get("present") or "1").strip() in ("1", "true", "present", "済", "on") else "0"
    if not cid or not kind:
        raise OpError(400, "case_id と doc_kind が必要です。")
    st = _store(data_dir)
    ex = st.query("document_requirements", "case_id = ? AND doc_kind = ?", (cid, kind))
    if ex:
        st.update_row("document_requirements", "req_id", ex[0]["req_id"], {"present": present})
    else:
        rid = "RQ-" + hashlib.sha256(f"{cid}|{kind}|{_now()}".encode("utf-8")).hexdigest()[:10].upper()
        st.insert_row("document_requirements", _full_row("document_requirements", {
            "req_id": rid, "case_id": cid, "doc_kind": kind, "required": "1", "present": present}))
    _audit(data_dir, _event(actor, "requirement_checked", cid, kind, doc_kind=kind, present=present))
    return {"ok": True, "op": "requirement_check", "case_id": cid, "doc_kind": kind,
            "present": present, "link": f"/timeline?id={cid}"}


def liff_publish(data_dir, params, actor, role):
    """物件をLIFF内見予約アプリへ公開opt-in（＋顧客表示フィールドを curate）。可逆・既定=非公開。
    公開フラグ／表示フィールドは物件マスタ(properties)に無い＝overlay(liff_export.set_publish)に持つ。
    property_id はマスタに実在する物件のみ（存在しないIDの公開を防ぐ）。published=false で非公開へ戻せる。"""
    _check_role("liff_publish", role)
    from hub_core import liff_export as _lx
    pid = (params.get("property_id") or "").strip()
    if not pid:
        raise OpError(400, "property_id が必要です。")
    st = _store(data_dir)
    if not st.query("properties", "property_id = ?", (pid,)):
        raise OpError(404, f"物件 {pid} が properties に見つかりません（先に物件登録が必要です）。")
    published = str(params.get("published", "1")).strip().lower() in ("1", "true", "yes", "はい", "on", "公開")
    # 顧客表示フィールド（マスタに無いもの）。未指定は None＝既存 overlay を保持（set_publish が last-wins）。
    fields = {}
    # deal_terms＝取引態様（貸主/売主/代理/媒介）。広告のたびに明示が要る（宅建業法34条）。
    # 物件マスタに欄が無く、公開のたびに担当が入れる値なので overlay に持つ。
    for key in ("name", "room_number", "line", "floor", "floors_total",
                "management_fee", "highlights", "vault_property", "deal_terms"):
        if key in params and params.get(key) not in (None, ""):
            fields[key] = params.get(key)
    # photos は asset_key のカンマ/改行区切り or 既にリスト。明示指定のみ（推測パス禁止）。
    if params.get("photos") not in (None, ""):
        ph = params.get("photos")
        if isinstance(ph, str):
            ph = [x.strip() for x in ph.replace("\n", ",").split(",") if x.strip()]
        fields["photos"] = ph
    ov = _lx.set_publish(data_dir, pid, published=published, fields=fields)
    _audit(data_dir, _event(actor, "liff_publish_set", pid,
                            "public" if published else "private", published=published))
    return {"ok": True, "op": "liff_publish", "property_id": pid,
            "published": published, "fields": sorted(fields.keys()), "link": "/line"}


def liff_export(data_dir, params, actor, role):
    """公開opt-in済の物件を LIFF の properties.json ＋ 物件写真へ書き出す（読取→書出・可逆）。
    捏造しない＝必須フィールドが揃った公開物件だけ出し、欠けは skipped に正直開示（0件でも正常終了）。
    出力を Cloudflare Pages のデプロイに同梱すると顧客のトーク内に実物件が並ぶ。"""
    _check_role("liff_export", role)
    from hub_core import liff_export as _lx
    res = _lx.build_export(data_dir)
    _audit(data_dir, _event(actor, "liff_exported", "liff-export", "ok",
                            exported=res["exported"], skipped=len(res["skipped"]),
                            photos=res["photos_copied"]))
    return {"ok": True, "op": "liff_export", "exported": res["exported"],
            "skipped": res["skipped"], "photos_copied": res["photos_copied"],
            "path": res["path"],
            "link": f"/line?exported={res['exported']}&skipped={len(res['skipped'])}"}


OPERATIONS = {
    "case_advance": case_advance,
    "task_done": task_done,
    "task_snooze": task_snooze,
    "task_unsnooze": task_unsnooze,
    "approval_decide": approval_decide,
    "hold_release": hold_release,
    "lead_convert": lead_convert,
    "customer_case_create": customer_case_create,
    "lead_quick_add": lead_quick_add,
    "inbox_ingest": inbox_ingest,
    "extraction_save": extraction_save,
    "ocr_extract": ocr_extract,
    "extraction_approve": extraction_approve,
    "esign_create": esign_create,
    "esign_send": esign_send,
    "application_create": application_create,
    "application_advance": application_advance,
    "screening_result": screening_result,
    "permission_record": permission_record,
    "obi_swap": obi_swap,
    "viewing_schedule": viewing_schedule,
    "viewing_list": viewing_list,
    "renewal_generate": renewal_generate,
    "contract_create": contract_create,
    "activity_report": activity_report,
    "it_session_create": it_session_create,
    "line_start_it_juusetsu": line_start_it_juusetsu,
    "it_check_requirement": it_check_requirement,
    "it_advance": it_advance,
    "it_gate_set": it_gate_set,
    "it_schedule_confirm": it_schedule_confirm,
    "juusetsu_consent_record": juusetsu_consent_record,
    "juusetsu_deliver": juusetsu_deliver,
    "schedule_slots": schedule_slots,
    "portal_link": portal_link,
    "billing_create": billing_create,
    "reconcile_deposits": reconcile_deposits,
    "moveout_settle": moveout_settle,
    "zoning_lookup": zoning_lookup,
    "ocr_read": ocr_read,
    "bukkaku_send": bukkaku_send,
    "fax_confirm_send": fax_confirm_send,
    "fax_receive": fax_receive,
    "line_send": line_send,
    "line_flex_property_send": line_flex_property_send,
    "line_confirm_send": line_confirm_send,
    "line_receive": line_receive,
    "line_harness_pull": line_harness_pull,
    "inquiry_create": inquiry_create,
    "inquiry_resolve": inquiry_resolve,
    "hearing_create": hearing_create,
    "caller_directory_add": caller_directory_add,
    "bukkaku_code_assign": bukkaku_code_assign,
    "call_receive": call_receive,
    "property_status_set": property_status_set,
    "reins_prepare": reins_prepare,
    "reins_record": reins_record,
    "overdue_reminders": overdue_reminders,
    "billing_reconcile": billing_reconcile,
    "stage_advance": stage_advance,
    "case_lose": case_lose,
    "attribute_update": attribute_update,
    "contact_log_add": contact_log_add,
    "followup_generate": followup_generate,
    "message_draft": message_draft,
    "message_queue": message_queue,
    "message_send": message_send,
    "proposal_draft": proposal_draft,
    "asset_attest": asset_attest,
    "property_register": property_register,
    "requirement_check": requirement_check,
    "liff_publish": liff_publish,
    "liff_export": liff_export,
}


_ATOMIC_WEBHOOK_OPS = frozenset({"fax_receive", "line_receive", "call_receive"})


def apply_operation(data_dir, op: str, params: dict, actor: str, role: str) -> dict:
    """単一エントリ。監査まで成功しない限り業務状態を進めない。"""
    fn = OPERATIONS.get(op)
    if fn is None:
        raise OpError(404, f"未知の操作: {op}")
    # 案件所有権ゲート。_check_role は「この役職が一般にこの操作をしてよいか」しか見ず、
    # 「この人がこの案件に触ってよいか」を見ない。全経路（/op・/api/op・batch・chat・
    # 内部呼び出し）が apply_operation を通るので、mutation の手前のここが唯一の関門。
    allowed, reason = op_scope.check(data_dir, op, params, actor, role)
    if not allowed:
        raise OpError(403, reason)
    with _operation_lock(data_dir):
        _require_healthy_audit(data_dir)
        snapshot = _snapshot_database(data_dir)
        journal = _WebhookJournal(data_dir) if op in _ATOMIC_WEBHOOK_OPS else None
        previous_journal = getattr(_OPERATION_LOCAL, "webhook_journal", None)
        if journal is not None:
            _OPERATION_LOCAL.webhook_journal = journal
        try:
            result = fn(data_dir, params, actor, role)
            if journal is not None:
                _commit_webhook_audit(data_dir, journal)
        except Exception:
            try:
                if journal is not None:
                    journal.rollback()
            finally:
                _restore_database(data_dir, snapshot)
            raise
        else:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)
            return result
        finally:
            if journal is not None:
                if previous_journal is None:
                    try:
                        delattr(_OPERATION_LOCAL, "webhook_journal")
                    except AttributeError:
                        pass
                else:
                    _OPERATION_LOCAL.webhook_journal = previous_journal
                journal.close()


# 一括実行から除外する操作（法定/金銭/記名の不可逆・重大op＝必ず単発で人間確認）。
# 監査P0(BULK-01)是正: batchは可逆・低リスクopのみ。承認/請求/帯替え出力/許諾記録/記名確定は除外。
BATCH_EXCLUDED = {"approval_decide", "billing_create", "obi_swap", "permission_record",
                  "it_gate_set",   # 会社の法定表示を一括で書き換えさせない
                  "extraction_approve", "esign_create", "esign_send", "screening_result",
                  "billing_reconcile", "message_send", "fax_confirm_send", "line_confirm_send",
                  "juusetsu_deliver", "asset_attest"}   # 法定交付/素材の権利宣言＝単発で人間確認
BATCH_MAX = 200


def batch_apply(data_dir, items: list, actor: str, role: str) -> dict:
    """複数opを一括実行（監査P0 BULK-01の基盤）。各opは apply_operation を通す＝
    役割ゲートとHMAC監査を1件ずつ温存。1件のOpErrorは他を止めず部分成功をレポート。
    法定/金銭/記名op（BATCH_EXCLUDED）は一括拒否（403）＝一括承認の暴発を防ぐ。"""
    if not isinstance(items, list):
        raise OpError(400, "batch は配列が必要です。")
    if len(items) > BATCH_MAX:
        raise OpError(400, f"一括は最大{BATCH_MAX}件までです（{len(items)}件）。")
    results = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": i, "ok": False, "code": 400, "error": "各要素は {op, params} が必要"})
            continue
        op = str(item.get("op") or "").strip()
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        if op in BATCH_EXCLUDED:
            results.append({"index": i, "op": op, "ok": False, "code": 403,
                            "error": f"一括実行できない操作です（法定/金銭/記名は単発で確認）: {op}"})
            continue
        try:
            r = apply_operation(data_dir, op, params, actor, role)  # 役割ゲート+HMAC監査は内部で
            results.append({"index": i, "op": op, "ok": True, "result": r})
        except OpError as e:
            results.append({"index": i, "op": op, "ok": False, "code": e.code, "error": e.msg})
    ok = sum(1 for r in results if r["ok"])
    return {"total": len(items), "ok": ok, "failed": len(items) - ok,
            "partial": 0 < ok < len(items), "results": results}
