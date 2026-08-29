#!/usr/bin/env python3
"""あいのて ローカルWeb UI (ループバック限定 / 外部資産0 / 外部送信は既定OFF)

ri-hub の出力 (out/ または --data-dir 指定) を読み込み、不動産業務ダッシュボードとして
通常画面の表示と、明示操作による業務更新を提供する。

設計の柱:
- 左サイドバー(業務動線でグループ化・件数バッジ) + 上部バー(検索/更新時刻) + メイン。
- ダッシュボード(home): KPIカード + 要対応アラート + 今日やること + 最近の監査。
- 色分けバッジ: gate_status(hold=赤/approval=橙/pass=緑/warn=黄)・ゲート種別チップ・
  優先度(P0=赤強調)・キューチップ。
- グローバル検索(?q=)で全列横断の部分一致フィルタ+ハイライト。
- 絞り込みチップ(?f=列:値)・列ソート(?sort=列&dir=asc/desc, vanilla JSでも可)。
- 案件串刺し /case?id=... : 物件参照/案件ID/反響ID をキーに tasks/hold/approval/docs/
  money/claims/audit を1画面集約。
- 336 capability 由来の統合台帳(governance/id_crosswalk/claims/contract_version/
  filename_standardization/original_disposal/approval_ledger/recurrence)も画面化。

制約 (ハード):
- Webサーバ本体は標準ライブラリ中心。Office出力・暗号化等の同梱機能は固定依存を使う。
- 外部資産0: CSS/JS/フォント/アイコンは全て同梱またはインライン。CDN・外部URL不使用。
  CSP は default-src 'none' を維持 (style/script は 'unsafe-inline')。絵文字はUnicode直書き。
- 状態変更は明示POST allowlistだけ。未知POSTと PUT/DELETE/PATCH は未実装 (=501)。
- 外部AI・送信連携は既定OFF。利用者が設定・実行した経路だけが外部へ接続する。
- 127.0.0.1 バインド。

使い方:
    python3 serve.py [--data-dir DIR] [--port 8765]   # サーバ起動
    python3 serve.py --selftest                        # 全ルート200+POST allowlist+外部資産0
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import html
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote, unquote

from hub_core.auth import (
    SESSION_ABSOLUTE_TTL_SECONDS, Viewer, authenticate, auth_required, create_session,
    destroy_session, get_session, is_configured, save_company, save_user, load_company,
)
from hub_core.viewlog import record_view
from hub_core import branding as _branding
from hub_core import ui  # 統一デザインシステム(単一の正本: APP_CSS / shell / sidebar)

# リクエスト毎の viewer(S0-3)。ThreadingMixIn=リクエスト毎スレッドなので thread-local で隔離。
# load_source / render_page が current_viewer() を読み、認可(行スコープ+PII列マスク)を効かせる。
_REQUEST = threading.local()
SESSION_COOKIE = "rihub_session"


def _session_cookie(sid: str) -> str:
    return (f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_ABSOLUTE_TTL_SECONDS}")


def current_viewer():
    return getattr(_REQUEST, "viewer", None)


ROOT = Path(__file__).resolve().parent
JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst_iso() -> str:
    return datetime.datetime.now(JST).replace(microsecond=0).isoformat()


# S0-7(§4.2): 集約画面を含む全画面でエクスポートを禁止する。平文PIIのダウンロードを作らない。
# 検出は大小文字・%エンコード・キー名/拡張子の揺れに頑健化(敵対検証2026-06-15: ?EXPORT= / ?fmt=csv /
# %2ecsv 等の素通り=監査回避穴を是正)。意図検出時は 403 で拒否し audit(action='export')に記録する。
_EXPORT_QUERY_KEYS = {"export", "download", "dump"}      # キー存在だけでエクスポート意図
_EXPORT_FORMAT_KEYS = {"format", "fmt", "type", "out", "as"}  # 値がフォーマットならエクスポート意図
_EXPORT_FORMATS = {"csv", "excel", "xls", "xlsx", "tsv", "json", "jsonl", "xml", "parquet", "pdf"}
_EXPORT_EXTS = tuple("." + f for f in _EXPORT_FORMATS)


def is_export_request(path: str) -> bool:
    split = urlsplit(path)
    # %エンコード回避(%2e=.)を潰すためデコードしてから判定。キーは小文字化(?EXPORT=対策)。
    p = unquote(split.path or "").lower()
    if "/export" in p or "/download" in p or p.endswith(_EXPORT_EXTS):
        return True
    # keep_blank_values: 値なしの bare key(?download / ?EXPORT)も検出する(監査回避穴を塞ぐ)。
    q = {k.lower().strip(): [v.lower() for v in vs]
         for k, vs in parse_qs(unquote(split.query or ""), keep_blank_values=True).items()}
    if any(k in q for k in _EXPORT_QUERY_KEYS):
        return True
    for k in _EXPORT_FORMAT_KEYS:
        if any(v in _EXPORT_FORMATS for v in q.get(k, [])):
            return True
    return False

# ---------------------------------------------------------------------------
# tasks.csv 列名 (BOM除去後)
# ---------------------------------------------------------------------------
TASKS_FILE = "tasks.csv"
COL_TASK_ID = "タスクID"
COL_QUEUE = "キュー"
COL_STATE = "状態"
COL_PRIORITY = "優先度"
COL_TITLE = "タイトル"
COL_PORTAL = "ポータル"
COL_CUSTOMER = "顧客名"
COL_PROP_REF = "物件参照"
COL_ASSIGNEE = "担当"
COL_GATE = "ゲート"
COL_HOLD_REASON = "保留理由"
COL_APPROVAL_ROLE = "承認役割"
COL_LEAD_ID = "元反響ID"
COL_CREATED = "作成日時"


def _q(row, value):
    return row.get(COL_QUEUE, "") == value


def _gate_in(row, values):
    return row.get(COL_GATE, "") in values


def _title_has(row, needles):
    t = row.get(COL_TITLE, "")
    return any(n in t for n in needles)


# ---------------------------------------------------------------------------
# 画面定義
#   group: サイドバーのグループ見出し
#   icon : Unicode 直書き絵文字 (外部画像を使わない)
#   source: "tasks" / "csv:<file>" / "jsonl:<file>"
#   important_cols: 先頭に寄せる重要列 (視認性)
# ---------------------------------------------------------------------------
SCREENS = [
    # ---  今日の対応 ---
    {"route": "/today", "label": "今日のタスク", "icon": "", "group": "今日の対応",
     "desc": "今日やること", "source": "tasks",
     "predicate": lambda r: _q(r, "Today")},
    {"route": "/hold", "label": "保留", "icon": "", "group": "今日の対応",
     "desc": "止まっている対象・理由・解除条件（状態表示のみ）",
     "source": "csv:hold_queue.csv", "predicate": lambda r: True},
    {"route": "/approval", "label": "承認待ち", "icon": "", "group": "今日の対応",
     "desc": "宅建士・士業・代表・経理の承認待ち（状態表示のみ）",
     "source": "csv:approval_queue.csv", "predicate": lambda r: True},

    # ---  反響・客付け ---
    {"route": "/inbox", "label": "反響", "icon": "", "group": "反響・客付け",
     "desc": "新着を分類する", "source": "tasks",
     "predicate": lambda r: _q(r, "Inbox")},
    {"route": "/leads", "label": "お客様の問い合わせ", "icon": "", "group": "反響・客付け",
     "desc": "ポータル・メール・電話の反響", "source": "csv:portal_leads.csv",
     "predicate": lambda r: True},
    {"route": "/viewings", "label": "内見", "icon": "", "group": "反響・客付け",
     "desc": "内見の前後・当日", "source": "tasks",
     "predicate": lambda r: _q(r, "Viewing")},
    {"route": "/applications", "label": "申込・資料請求", "icon": "", "group": "反響・客付け",
     "desc": "申込・資料請求",
     "source": "tasks",
     "predicate": lambda r: _q(r, "Applications") or _title_has(r, ("申込", "資料請求"))},

    # ---  物件・調査・広告 ---
    {"route": "/properties", "label": "物件", "icon": "", "group": "物件・調査・広告",
     "desc": "案件と物件の正本台帳", "source": "csv:cases.csv",
     "predicate": lambda r: True},
    {"route": "/research", "label": "物件の調査", "icon": "", "group": "物件・調査・広告",
     "desc": "行政資料・調査・原典・役所待ち", "source": "tasks",
     "predicate": lambda r: _q(r, "Research")},
    {"route": "/documents", "label": "書類", "icon": "", "group": "物件・調査・広告",
     "desc": "書類・OCR・個人情報・専門確認",
     "source": "tasks",
     "predicate": lambda r: _gate_in(r, ("privacy", "professional", "document"))},
    {"route": "/ads", "label": "広告の公開", "icon": "", "group": "物件・調査・広告",
     "desc": "マイソク・広告の公開ゲート",
     "source": "tasks",
     "predicate": lambda r: _q(r, "Ads") or _gate_in(r, ("publish", "go_live"))},

    # ---  契約・金銭 ---
    {"route": "/contracts", "label": "契約", "icon": "", "group": "契約・金銭",
     "desc": "重説・37条・特約・電子交付", "source": "tasks",
     "predicate": lambda r: _gate_in(r, ("contract",))},
    {"route": "/money", "label": "お金", "icon": "", "group": "契約・金銭",
     "desc": "請求・入金・返金・精算", "source": "tasks",
     "predicate": lambda r: _gate_in(r, ("money",))},

    # ---  管理・報告 ---
    {"route": "/management", "label": "管理業務", "icon": "", "group": "管理・報告",
     "desc": "修繕・退去・家主承認", "source": "tasks",
     "predicate": lambda r: _q(r, "Management")},
    {"route": "/reports", "label": "報告", "icon": "", "group": "管理・報告",
     "desc": "媒介活動報告・家主報告・日報", "source": "tasks",
     "predicate": lambda r: _q(r, "Reports")},

    # ---  監査・統合 ---
    {"route": "/audit", "label": "監査ログ", "icon": "", "group": "監査・統合",
     "desc": "改ざんを検知できる操作記録", "source": "jsonl:audit_log.jsonl",
     "predicate": lambda r: True},
]

# 統合台帳 (336 capability 由来) — Audit/統合グループに同居させる
LEDGERS = [
    {"route": "/ledger/crosswalk", "label": "名寄せ", "icon": "",
     "group": "監査・統合", "desc": "台帳をまたいだIDの対応表",
     "source": "csv:id_crosswalk.csv", "predicate": lambda r: True},
    {"route": "/ledger/governance", "label": "業務統制", "icon": "",
     "group": "監査・統合", "desc": "資格・権限・保管・教育の統制台帳",
     "source": "csv:governance_register.csv", "predicate": lambda r: True},
    {"route": "/ledger/claims", "label": "相談・苦情", "icon": "",
     "group": "監査・統合", "desc": "クレーム・近隣トラブルの受付台帳",
     "source": "csv:claims_register.csv", "predicate": lambda r: True},
    {"route": "/ledger/recurrence", "label": "再発防止", "icon": "",
     "group": "監査・統合", "desc": "再発防止チェックリスト",
     "source": "csv:recurrence_checklist.csv", "predicate": lambda r: True},
    {"route": "/ledger/contract-version", "label": "書類の版管理", "icon": "",
     "group": "監査・統合", "desc": "契約書類の版管理",
     "source": "csv:contract_version_register.csv", "predicate": lambda r: True},
    {"route": "/ledger/filenames", "label": "ファイル名", "icon": "",
     "group": "監査・統合", "desc": "ファイル名の標準化提案",
     "source": "csv:filename_standardization.csv", "predicate": lambda r: True},
    {"route": "/ledger/originals", "label": "原本管理", "icon": "",
     "group": "監査・統合", "desc": "原本の保管・返却台帳",
     "source": "csv:original_disposal_register.csv", "predicate": lambda r: True},
    {"route": "/ledger/approval-ledger", "label": "承認記録", "icon": "",
     "group": "監査・統合", "desc": "承認記録の確定台帳",
     "source": "csv:approval_ledger.csv", "predicate": lambda r: True},
    {"route": "/viewlog", "label": "閲覧監査", "icon": "",
     "group": "監査・統合", "desc": "誰が何を閲覧したかを確認する台帳",
     "source": "jsonl:view_audit.jsonl", "predicate": lambda r: True},
]

ALL_PAGES = SCREENS + LEDGERS
PAGE_BY_ROUTE = {s["route"]: s for s in ALL_PAGES}

# サイドバーのグループ表示順
GROUP_ORDER = [
    "今日の対応",
    "反響・客付け",
    "物件・調査・広告",
    "契約・金銭",
    "管理・報告",
    "監査・統合",
]

# 重要列を前に寄せる定義 (キーは route)。残りはCSV元順で後続。
IMPORTANT_COLS = {
    "tasks": [COL_PRIORITY, COL_QUEUE, COL_STATE, COL_GATE, COL_TITLE,
              COL_CUSTOMER, COL_PROP_REF, COL_ASSIGNEE, COL_HOLD_REASON],
}

# 案件串刺しキーとして扱う列 (これらの値はリンク化する)
CASE_KEY_COLS = {
    COL_PROP_REF, COL_LEAD_ID, "案件ID", "物件ID", "顧客ID", "反響ID",
    "タスクID", "物件参照", "受付ID", "書類ID", "Hubキー", "元反響ID",
}

# ---------------------------------------------------------------------------
# データ読み込み (読み取り専用)
# ---------------------------------------------------------------------------
def _strip_bom_keys(row):
    out = {}
    for k, v in row.items():
        if k is None:
            continue
        nk = k.lstrip("﻿") if isinstance(k, str) else k
        out[nk] = v
    return out


def read_csv(path: Path):
    """CSVを (headers, rows) で返す。存在しなければ ([], [])。"""
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [_strip_bom_keys(r) for r in reader]
        headers = [h.lstrip("﻿") for h in (reader.fieldnames or [])]
    return headers, rows


def read_jsonl(path: Path):
    """jsonl を (headers, rows) で返す。headers は全行キーの和集合 (出現順)。"""
    if not path.exists():
        return [], []
    rows, headers, seen = [], [], set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                obj = {"value": obj}
            rows.append(obj)
            for k in obj.keys():
                if k not in seen:
                    seen.add(k)
                    headers.append(k)
    return headers, rows


def load_source(data_dir: Path, source: str):
    """source 文字列に従い (headers, rows) を返す (predicate前)。

    S0-2: DB正本(out/hub.db)があり source が DB-backed テーブルなら store 経由で読む
    (英語キー→日本語ラベルに再キー化し、従来 read_csv と同形を返す=下流無改変)。
    DBが無い/対応外(audit jsonl 等)は従来通り CSV/jsonl ファイルを読む(フォールバック)。
    """
    if source == "tasks" or source.startswith("csv:"):
        try:
            from hub_core.views import query_page
            res = query_page(data_dir, source, current_viewer())  # S0-3: viewer認可を適用
            if res is not None:
                return res
        except Exception:
            pass  # store未整備でもファイル読みにフォールバック(可用性優先)
    # フォールバック(DB未整備時): ファイル読み。CSV経路でも認可をバイパスしない(S0-4)
    if source == "tasks":
        headers, rows = read_csv(data_dir / TASKS_FILE)
        return headers, _authz_rows(rows)
    if source.startswith("csv:"):
        headers, rows = read_csv(data_dir / source[len("csv:"):])
        return headers, _authz_rows(rows)
    if source.startswith("jsonl:"):
        return read_jsonl(data_dir / source[len("jsonl:"):])
    return [], []


def _authz_rows(rows):
    """CSVフォールバック行に現在の viewer 認可(行スコープ+PII)を適用する(S0-4)。"""
    from hub_core.auth import authorize_rows
    return authorize_rows(rows, current_viewer())


def load_page_data(data_dir: Path, page):
    """page 定義に従い (headers, rows) を返す。常に読み取りのみ。"""
    headers, rows = load_source(data_dir, page["source"])
    filtered = [r for r in rows if page["predicate"](r)]
    return headers, filtered


def count_page(data_dir: Path, page):
    _, rows = load_page_data(data_dir, page)
    return len(rows)


def data_mtime(data_dir: Path) -> str:
    """data-dir 配下CSV/jsonlの最終更新時刻を人間可読で返す。"""
    try:
        latest = 0.0
        for p in data_dir.glob("*"):
            if p.suffix in (".csv", ".jsonl") and p.is_file():
                latest = max(latest, p.stat().st_mtime)
        if latest == 0.0:
            return "データなし"
        return datetime.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "不明"


# ---------------------------------------------------------------------------
# 視認性ヘルパ (バッジ・チップ・ハイライト)
# ---------------------------------------------------------------------------
def _esc(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False)
    return html.escape(v)


_DOC_KIND_LABELS = {
    "juusetsu": "重要事項説明書",
    "maisoku": "マイソク",
    "contract": "契約書類",
    "document": "書類",
}
_SOURCE_TOOL_LABELS = {
    "ri-chousa": "物件調査データ",
    "chousa": "物件調査データ",
    "ri-crm": "顧客管理データ",
    "crm": "顧客管理データ",
    "portal": "問い合わせデータ",
    "mail": "問い合わせデータ",
}
_INTERNAL_PATH_RE = re.compile(
    r"(?:file://)?(?:/(?:Users|home|root|private|tmp|var|opt|etc|usr|bin|sbin|dev|proc|run|Applications|Volumes|mnt|srv|Library|System)(?:/[^\s<>'\"]+)+"
    r"|[A-Za-z]:[\\/](?:[^\s<>'\"]+[\\/])*[^\s<>'\"]*"
    r"|\\\\[^\s\\/]+[\\/][^\s<>'\"]+)"
)
_INTERNAL_DETAIL_RE = re.compile(
    r"(?:\b(?:RISK|PRS|LINE_HARNESS|RI_HUB|OPENAI|ANTHROPIC|FAX_WEBHOOK)_[A-Z0-9_]+\b"
    r"|\b(?:localhost|127\.0\.0\.1)(?::\d+)?\b|https?://|file://|(?:^|\s)~/"
    r"|\b(?:mock|harness|outbox|inbox)\b|\b(?:audit_log|view_audit)\.jsonl\b"
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b)",
    re.I,
)


def _doc_kind_label(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in _DOC_KIND_LABELS:
        return _DOC_KIND_LABELS[raw]
    if raw and re.fullmatch(r"[a-z0-9_.:/-]+", raw):
        return "書類"
    return str(value or "").strip() or "書類"


def _document_display_title(value) -> str:
    """技術的な書類IDはリンク値にだけ保持し、見出しは業務名で表示する。"""
    raw = str(value or "").strip()
    for prefix, label in (("JU-", "重要事項説明書"), ("MS-", "マイソク")):
        if raw.upper().startswith(prefix):
            suffix = raw[len(prefix):].strip()
            visible_suffix = _visible_data_value("書類名", suffix)
            if visible_suffix and visible_suffix == suffix:
                return f"{label}：{visible_suffix}"
            return label
    return _visible_data_value("書類名", raw) or "書類"


def _display_datetime(value) -> str:
    """ISO日時を画面向けにし、未知値は内部参照を除いてそのまま返す。"""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return f"{parsed.year}年{parsed.month}月{parsed.day}日 {parsed.hour:02d}:{parsed.minute:02d}"
    except (TypeError, ValueError):
        return _visible_data_value("日時", raw) or "—"


def _visible_data_value(col: str, value) -> str:
    """台帳由来の値を、業務画面に出してよい人向け表現へ正規化する。"""
    text = value if isinstance(value, str) else (
        "" if value is None else json.dumps(value, ensure_ascii=False))
    text = text.strip()
    if not text:
        return ""
    if Path(text).is_absolute() or text.startswith(("~/", "file://")) or _INTERNAL_PATH_RE.search(text):
        return "(内部参照)"

    if re.search(
            r"\b(?:[A-Z][A-Z0-9]*_)+(?:API_)?(?:KEY|TOKEN|SECRET|PASSWORD|URL|ENDPOINT|AUTH)\b",
            text):
        return "(設定情報)"
    if re.search(r"\b(?:localhost|127\.0\.0\.1)(?::\d+)?\b", text, re.I):
        return "(端末内接続)"
    if re.search(r"https?://", text, re.I):
        return "(外部接続)"
    if re.search(r"\b(?:mock|harness|outbox|inbox)\b", text, re.I):
        return "(開発情報)"
    if re.search(r"\b(?:audit_log|view_audit)\.jsonl\b", text, re.I):
        return "(内部台帳)"

    key = str(col or "").strip().lower()
    source_column = any(token in key for token in (
        "source", "元ツール", "元データ", "取得元", "参照元", "path", "file", "ファイル", "経路"))
    source_key = text.lower().strip()
    if source_column:
        if source_key in _SOURCE_TOOL_LABELS:
            return _SOURCE_TOOL_LABELS[source_key]
        if "chousa" in source_key:
            return "物件調査データ"
        if "crm" in source_key:
            return "顧客管理データ"
        if source_key.startswith("mail:") or "portal" in source_key:
            return "問い合わせデータ"
        if re.match(r"https?://", source_key):
            return "外部サービス"
        if "/" in text or "\\" in text or re.search(r"\.(?:csv|jsonl?|md|txt|xlsx?|docx?)$", text, re.I):
            return "移行元データ"
        if re.fullmatch(r"[a-z0-9_.:-]+", source_key):
            return "移行元データ"

    replacements = {
        "ri-chousa": "物件調査",
        "ri-crm": "顧客管理",
        "juusetsu_draft.md": "重要事項説明書の下書き",
        "kakunin_list.csv": "確認項目",
    }
    for old, new in replacements.items():
        text = re.sub(re.escape(old), new, text, flags=re.I)
    return text


def _signer_form_defaults(data_dir: Path, viewer: Viewer | None) -> tuple[str, str]:
    """記名確定フォームの初期値。任意入力ではなく設定済み本人/会社情報を促す。"""
    try:
        company = load_company(data_dir, strict=False)
    except Exception:
        company = {}
    name = ""
    reg = ""
    if viewer and viewer.role == "宅建士":
        name = str(getattr(viewer, "display_name", "") or "").strip()
        reg = str(getattr(viewer, "registration_no", "") or "").strip()
    return (
        name or str(company.get("staff") or "").strip(),
        reg or str(company.get("takkenshi_reg") or "").strip(),
    )


def _public_display_param(value) -> str:
    """クエリ由来の文字列は、安全な通常値だけを画面へ反映する。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    visible = _visible_data_value("画面入力", raw)
    return raw if visible == raw else ""


def _public_failure(message: str = "処理を完了できませんでした。入力内容を確認して、もう一度お試しください。") -> str:
    """予期しない例外の詳細や端末内パスをHTTPレスポンスへ返さない。"""
    return message


def _public_exception_message(exc, fallback: str | None = None) -> str:
    """明示的な日本語業務エラーだけを返し、実装詳細は固定文へ倒す。"""
    default = fallback or _public_failure()
    raw = str(getattr(exc, "public_message", "") or getattr(exc, "msg", "") or "").strip()
    if not raw or len(raw) > 240:
        return default
    if not re.search(r"[ぁ-んァ-ン一-龯]", raw):
        return default
    if re.search(r"\b(?:error|exception|traceback|failed|failure|http\s*\d{3})\b", raw, re.I):
        return default
    if _INTERNAL_PATH_RE.search(raw) or _INTERNAL_DETAIL_RE.search(raw):
        return default
    visible = _visible_data_value("error", raw)
    if visible != raw or "(内部参照)" in visible:
        return default
    return visible


def _public_notice_param(value: str, fallback: str = "処理を完了できませんでした。") -> str:
    """URLクエリ由来の通知に内部情報を反射しない。"""
    class _Notice:
        msg = value

    return _public_exception_message(_Notice(), fallback)


def _public_count_param(value, *, maximum: int = 99999) -> str:
    """URLクエリ由来の件数は非負整数だけを画面へ返す。"""
    try:
        return str(max(0, min(maximum, int(str(value or "0").strip()))))
    except (TypeError, ValueError):
        return "0"


# gate_status のような状態値 → CSSクラス
_STATUS_CLASS = {
    "hold": "b-red", "保留": "b-red", "未終結": "b-red", "overdue": "b-red",
    "expired": "b-red", "旧版": "b-red", "left": "b-red", "unassigned": "b-red",
    "deletion_requested": "b-red",
    "approval": "b-org", "pending": "b-org", "待ち": "b-org", "review": "b-org",
    "要": "b-org", "返却予定": "b-org",
    "pass": "b-green", "ok": "b-green", "approved": "b-green", "ready": "b-green",
    "done": "b-green", "最新": "b-green", "終結": "b-green", "active": "b-green",
    "assigned": "b-green", "opt_in": "b-green", "保管中": "b-green",
    "warn": "b-yellow", "warning": "b-yellow", "unknown": "b-gray",
    "open": "b-blue", "waiting": "b-org",
}

# ゲート種別 → チップ色クラス
_GATE_CLASS = {
    "send": "g-send", "publish": "g-publish", "go_live": "g-publish",
    "contract": "g-contract", "money": "g-money", "privacy": "g-privacy",
    "professional": "g-prof", "document": "g-doc", "evidence": "g-doc",
    "tos": "g-tos", "optin": "g-optin", "approval": "g-org",
    "compliance": "g-tos", "warning": "g-warn",
}


def _status_class(value: str) -> str:
    v = (value or "").strip()
    return _STATUS_CLASS.get(v, "b-gray")


def _highlight(text: str, q: str) -> str:
    """エスケープ済みテキスト前提で q (素の検索語) を <mark> 強調する。"""
    if not q:
        return text
    eq = html.escape(q)
    if not eq:
        return text
    # 大小無視で部分一致を mark で囲む (素朴な走査)
    low_text, low_eq = text.lower(), eq.lower()
    out, i = [], 0
    while True:
        idx = low_text.find(low_eq, i)
        if idx < 0:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        out.append('<mark>' + text[idx:idx + len(eq)] + '</mark>')
        i = idx + len(eq)
    return "".join(out)


def render_cell(col: str, value, q: str) -> str:
    """1セルを色分けバッジ/チップ/案件リンク付きで描画する。"""
    raw = _visible_data_value(col, value)
    esc = _esc(raw)

    # 空セル
    if raw == "" or raw is None:
        return '<span class="muted">—</span>'

    # 優先度バッジ
    if col == COL_PRIORITY:
        cls = "p0" if raw == "P0" else ("p1" if raw == "P1" else "p2")
        return f'<span class="prio {cls}">{esc}</span>'

    # ゲート種別チップ
    if col == COL_GATE or col == "ゲート":
        gcls = _GATE_CLASS.get(raw, "g-gray")
        return f'<span class="chip {gcls}">{_highlight(esc, q)}</span>'

    # キュー チップ
    if col == COL_QUEUE:
        return f'<span class="qchip">{_highlight(esc, q)}</span>'

    # 状態系の色バッジ (状態/判断/ゲート状態/最新判定/終結状態/同意状態/返信ゲート/原本状態 等)
    if col in ("状態", "判断", "ゲート状態", "最新判定", "終結状態", "同意状態",
               "返信ゲート", "原本状態", "リネーム要否", "緊急度", "reply_gate",
               "gate_status", "処理要否"):
        return f'<span class="badge {_status_class(raw)}">{_highlight(esc, q)}</span>'

    # 案件串刺しキー → /case リンク
    if col in CASE_KEY_COLS and raw:
        href = "/case?id=" + quote(raw)
        return f'<a class="caselink" href="{href}">{_highlight(esc, q)}</a>'

    # 長文は省略 + title 属性
    if len(raw) > 64:
        short = _highlight(html.escape(raw[:62]) + "…", q)
        return f'<span title="{esc}">{short}</span>'

    return _highlight(esc, q)


def order_headers(page, headers):
    """重要列を前に寄せる。"""
    src = page.get("source", "")
    key = "tasks" if src == "tasks" else None
    if key and key in IMPORTANT_COLS:
        front = [c for c in IMPORTANT_COLS[key] if c in headers]
        rest = [c for c in headers if c not in front]
        return front + rest
    return headers


# ---------------------------------------------------------------------------
# 絞り込み・検索・ソート (サーバ側)
# ---------------------------------------------------------------------------
# 各 source で facet に使う列
FACET_COLS = {
    "tasks": [COL_PRIORITY, COL_QUEUE, COL_STATE, COL_GATE, COL_ASSIGNEE],
    "csv:hold_queue.csv": ["保留種別", "解除役割", "ゲート", "ポータル"],
    "csv:approval_queue.csv": ["承認役割", "判断", "ポータル"],
    "csv:portal_leads.csv": ["ポータル", "問い合わせ種別", "同意状態", "返信ゲート"],
    "csv:cases.csv": ["取引種別", "状態", "ゲート状態"],
    "csv:governance_register.csv": ["カテゴリ", "状態"],
    "csv:claims_register.csv": ["種別", "緊急度", "終結状態"],
    "csv:approval_ledger.csv": ["ゲート", "確認役割", "判断"],
    "csv:contract_version_register.csv": ["書類種別", "最新判定"],
    "csv:id_crosswalk.csv": ["元ツール"],
    "csv:recurrence_checklist.csv": ["種別", "状態", "担当"],
    "csv:filename_standardization.csv": ["リネーム要否"],
    "csv:original_disposal_register.csv": ["書類種別", "原本状態", "処理要否"],
    "jsonl:audit_log.jsonl": ["action", "actor", "gate_status", "reply_gate"],
}


def row_matches_q(row, q: str) -> bool:
    if not q:
        return True
    low = q.lower()
    for v in row.values():
        if v is None:
            continue
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if low in s.lower():
            return True
    return False


def apply_query(headers, rows, params, source):
    """q / f(列:値) / sort / dir を適用し (headers, rows, applied) を返す。"""
    q = _public_display_param(params.get("q", [""])[0])
    fpairs = params.get("f", [])
    sort_col = params.get("sort", [""])[0]
    sort_dir = params.get("dir", ["asc"])[0]

    # 検索
    if q:
        rows = [r for r in rows if row_matches_q(r, q)]
    # facet 絞り込み (列:値 を AND)
    active_filters = []
    for fp in fpairs:
        if ":" not in fp:
            continue
        col, _, val = fp.partition(":")
        rows = [r for r in rows
                if str(r.get(col, "")) == val]
        active_filters.append((col, val))
    # ソート
    if sort_col and sort_col in (headers or []):
        rows = sorted(rows, key=lambda r: str(r.get(sort_col, "")),
                      reverse=(sort_dir == "desc"))
    return rows, q, active_filters, sort_col, sort_dir


def facet_counts(all_rows, col):
    counts = {}
    for r in all_rows:
        v = str(r.get(col, ""))
        if v == "":
            continue
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------------------
# インラインCSS / JS (外部参照ゼロ)
# ---------------------------------------------------------------------------
STYLE = """
:root{--bg:#f4f6f8;--panel:#fff;--ink:#1b2733;--muted:#6b7785;--line:#e2e7ec;
--side:#16222e;--side2:#1f2f3f;--accent:#2563eb;}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{font-family:-apple-system,"Hiragino Sans","Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--ink);line-height:1.5;font-size:19px;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
.layout{display:flex;min-height:100vh;}
/* sidebar */
.side{width:236px;background:var(--side);color:#cdd7e0;flex:0 0 236px;
padding:0 0 30px;position:sticky;top:0;height:100vh;overflow-y:auto;}
.brand{padding:16px 18px;font-weight:700;font-size:19px;color:#fff;
border-bottom:1px solid #2c3e50;letter-spacing:.3px;}
.brand .sub{display:block;font-weight:400;font-size:18px;color:#7d93a6;margin-top:2px;}
.navgrp{margin-top:14px;}
.navgrp .ghead{font-size:18px;color:#7d93a6;padding:4px 18px;letter-spacing:.5px;}
.side nav a{display:flex;align-items:center;gap:8px;padding:7px 18px;color:#cdd7e0;
font-size:18px;border-left:3px solid transparent;}
.side nav a:hover{background:var(--side2);text-decoration:none;}
.side nav a.active{background:var(--side2);border-left-color:var(--accent);color:#fff;}
.side nav a .ico{width:18px;text-align:center;}
.side nav a .lbl{flex:1;}
.side nav a .cnt{background:#33475b;color:#dfe8f0;border-radius:10px;
font-size:18px;padding:1px 7px;min-width:20px;text-align:center;}
.side nav a .cnt.hot{background:#c0392b;color:#fff;}
.side nav a .cnt.warm{background:#d68910;color:#fff;}
/* content */
.content{flex:1;min-width:0;display:flex;flex-direction:column;}
.topbar{background:var(--panel);border-bottom:1px solid var(--line);
padding:10px 22px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:5;}
.topbar h1{font-size:20px;margin:0;font-weight:600;white-space:nowrap;}
.search{flex:1;max-width:520px;}
.search input{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:8px;
font-size:18px;background:#f8fafc;}
.search input:focus{outline:none;border-color:var(--accent);background:#fff;}
.updated{font-size:18px;color:var(--muted);white-space:nowrap;}
.ro{font-size:18px;color:#7a5a00;background:#fff5e6;border:1px solid #f0d9a8;
border-radius:6px;padding:2px 8px;white-space:nowrap;}
main{padding:20px 22px 40px;flex:1;}
h2.page{font-size:19px;margin:0 0 2px;}
.pcount{font-size:18px;color:var(--muted);font-weight:400;}
.pdesc{color:var(--muted);font-size:18px;margin:2px 0 16px;}
/* KPI cards */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:6px 0 22px;}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;
display:block;color:inherit;}
.kpi:hover{text-decoration:none;box-shadow:0 1px 6px rgba(0,0,0,.07);}
.kpi .n{font-size:28px;font-weight:700;line-height:1.1;}
.kpi .l{font-size:18px;color:var(--muted);margin-top:3px;}
.kpi.red{border-left:4px solid #c0392b;} .kpi.red .n{color:#c0392b;}
.kpi.org{border-left:4px solid #d68910;} .kpi.org .n{color:#b9770e;}
.kpi.green{border-left:4px solid #1e8449;} .kpi.green .n{color:#1e8449;}
.kpi.blue{border-left:4px solid var(--accent);} .kpi.blue .n{color:var(--accent);}
.kpi.gray{border-left:4px solid #7f8c8d;}
/* panels (home) */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:18px;}
.panel h3{margin:0 0 10px;font-size:19px;}
.alist{list-style:none;margin:0;padding:0;}
.alist li{padding:7px 0;border-bottom:1px solid var(--line);font-size:18px;
display:flex;gap:8px;align-items:flex-start;}
.alist li:last-child{border-bottom:none;}
.alist .a-meta{color:var(--muted);font-size:18px;}
/* chips / badges */
.chip,.badge,.qchip,.prio{display:inline-block;border-radius:999px;padding:1px 9px;
font-size:18px;font-weight:600;white-space:nowrap;line-height:1.6;}
.qchip{background:#e8eef5;color:#33475b;}
.b-red{background:#fdecea;color:#922b21;} .b-org{background:#fef3e2;color:#9c640c;}
.b-green{background:#e8f6ee;color:#1e8449;} .b-yellow{background:#fef9e0;color:#9a7d0a;}
.b-blue{background:#e8f0fe;color:#1a56db;} .b-gray{background:#eef1f4;color:#5b6b7a;}
.prio.p0{background:#c0392b;color:#fff;} .prio.p1{background:#fde8c4;color:#8a5a00;}
.prio.p2{background:#eef1f4;color:#5b6b7a;}
.g-send{background:#e3f2fd;color:#1565c0;} .g-publish{background:#fce4ec;color:#ad1457;}
.g-contract{background:#ede7f6;color:#5e35b1;} .g-money{background:#e8f5e9;color:#2e7d32;}
.g-privacy{background:#fff3e0;color:#e65100;} .g-prof{background:#e0f7fa;color:#00838f;}
.g-doc{background:#f3e5f5;color:#6a1b9a;} .g-tos{background:#eceff1;color:#455a64;}
.g-optin{background:#e8eaf6;color:#3949ab;} .g-warn{background:#fff8e1;color:#9a7d0a;}
.g-gray{background:#eef1f4;color:#5b6b7a;}
/* facet chips */
.facets{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 14px;align-items:center;}
.facets .flabel{font-size:18px;color:var(--muted);margin-right:2px;}
.facet{display:inline-block;padding:3px 10px;border:1px solid var(--line);border-radius:999px;
font-size:18px;background:#fff;color:#33475b;}
.facet:hover{text-decoration:none;border-color:var(--accent);}
.facet.on{background:var(--accent);color:#fff;border-color:var(--accent);}
.facet .fc{color:#90a4b5;margin-left:4px;font-size:18px;}
.facet.on .fc{color:#cfe0ff;}
.clearf{font-size:18px;color:#c0392b;margin-left:6px;}
/* table */
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel);}
table{border-collapse:collapse;width:100%;font-size:18px;}
thead th{background:#eef2f6;position:sticky;top:0;z-index:1;text-align:left;
padding:8px 10px;border-bottom:2px solid #dce3ea;white-space:nowrap;}
thead th a{color:#33475b;display:inline-block;}
thead th a .arr{color:var(--accent);font-size:18px;}
tbody td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;
max-width:340px;word-break:break-word;}
tbody tr:nth-child(even){background:#fafcfe;}
tbody tr:hover{background:#f0f6ff;}
tr.p0row td{border-left:0;}
tr.p0row{box-shadow:inset 3px 0 0 #c0392b;}
tr.p0row td:nth-child(1){font-weight:700;}
.muted{color:#aab4bd;}
.caselink{font-family:ui-monospace,Menlo,monospace;font-size:18px;}
mark{background:#fff3b0;padding:0 1px;border-radius:2px;}
.empty{color:var(--muted);padding:26px;text-align:center;background:var(--panel);
border:1px dashed #c8d0d8;border-radius:10px;}
.case-sec{margin-bottom:20px;}
.case-sec h3{font-size:19px;margin:0 0 8px;border-bottom:2px solid var(--line);padding-bottom:4px;}
footer{padding:16px 22px;color:#9aa6b0;font-size:18px;}
@media(max-width:820px){
 .layout{flex-direction:column;}
 .side{width:100%;flex:none;height:auto;position:static;}
 .side nav{display:flex;flex-wrap:wrap;}
 .navgrp{margin-top:6px;width:100%;}
 .grid2{grid-template-columns:1fr;}
 .topbar{flex-wrap:wrap;}
}
"""

# あいのて 清書オーバーライド: 旧STYLEの上に重ねて全画面をモノクロ/Space Grotesk/明色サイドバーに再スキン(構造は保つ)

# 列ヘッダソートは ?sort= で完結する (JSなしでも動く)。
# JS は任意の体感向上のみ: 検索欄の Enter 送信は素のフォーム無しでも動くよう、
# location 書き換えで実現 (POSTを一切発生させない)。
SCRIPT = """
(function(){
 var box=document.getElementById('gsearch');
 if(box){box.addEventListener('keydown',function(e){
   if(e.key==='Enter'){
     var u=new URL(window.location.href);
     if(box.value){u.searchParams.set('q',box.value);}else{u.searchParams.delete('q');}
     u.searchParams.delete('sort');u.searchParams.delete('dir');
     window.location.assign(u.pathname+u.search);
   }
 });}
 // 一括操作（監査BULK-01のUI）: 選択タスクを /api/op {batch:[...]} で一括完了
 var cbs=Array.prototype.slice.call(document.querySelectorAll('.bulk-cb'));
 var all=document.getElementById('bulkAll');
 var btn=document.getElementById('bulkDone');
 var cnt=document.getElementById('bulkCount');
 var msg=document.getElementById('bulkMsg');
 if(cbs.length && btn){
   function selected(){return cbs.filter(function(c){return c.checked;});}
   function sync(){var n=selected().length;if(cnt)cnt.textContent=n+'件選択';btn.disabled=(n===0);}
   cbs.forEach(function(c){c.addEventListener('change',sync);});
   if(all){all.addEventListener('change',function(){cbs.forEach(function(c){c.checked=all.checked;});sync();});}
   btn.addEventListener('click',async function(){
     var ids=selected().map(function(c){return c.value;});
     if(!ids.length)return;
     btn.disabled=true;if(msg)msg.textContent='処理中…';
     var batch=ids.map(function(id){return {op:'task_done',params:{task_id:id}};});
     try{
       var r=await fetch('/api/op',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({batch:batch})});
       var j=await r.json();
       if(msg)msg.textContent=(j.ok||0)+'件完了'+((j.failed||0)?('・'+j.failed+'件失敗'):'');
       setTimeout(function(){window.location.reload();},700);
     }catch(e){if(msg)msg.textContent='通信エラー';btn.disabled=false;}
   });
   sync();
 }
})();
"""


# ---------------------------------------------------------------------------
# サイドバー / ページ枠
# ---------------------------------------------------------------------------
def render_topbar(data_dir: Path, title: str, q: str) -> str:
    q = _public_display_param(q)
    return (
        '<div class="topbar">'
        f'<h1>{_esc(title)}</h1>'
        '<div class="search">'
        f'<input id="gsearch" type="text" value="{_esc(q)}" '
        'placeholder="🔍 全列横断で検索 (Enterで実行)…" autocomplete="off">'
        '</div>'
        f'<div class="updated">更新 {_esc(data_mtime(data_dir))}</div>'
        '<div class="ro">読み取り専用 / 外部送信なし</div>'
        '</div>'
    )


def render_viewer_banner() -> str:
    """S0-3: 現在の viewer を明示。dev mode は警告バナー。"""
    v = current_viewer()
    if v is None:
        return ""
    if getattr(v, "is_dev", False):
        return ('<div style="background:#7a2e2e;color:#fff;padding:6px 14px;font-size:18px">'
                '開発用の確認モードです。ログイン確認を省略しています。</div>')
    return ('<div style="background:#15321f;color:#cfead8;padding:6px 14px;font-size:18px">'
            f'ログイン中: {_esc(v.user)} ／ 役割: {_esc(v.role)} ・ '
            '<a href="/logout" style="color:#9fd">ログアウト</a></div>')


import re as _re
# 絵文字を表示から除去(デザイン方針=絵文字なし)。矢印→等は温存(主要絵文字ブロックのみ)
_EMOJI_RE = _re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _deemoji(s: str) -> str:
    return _EMOJI_RE.sub("", s)


def render_page(data_dir: Path, active_route: str, title: str, body: str, q: str = "") -> str:
    """旧業務/台帳ページも統一シェルを通す。これにより28ページが一斉に同一デザインへ寄る。"""
    active = _nav_active(active_route)
    q = _public_display_param(q)
    search = ('<div class="search" style="width:300px">'
              f'<input id="gsearch" type="text" value="{_esc(q)}" '
              'placeholder="全列横断で検索 (Enterで実行)…" autocomplete="off"></div>')
    header = (f'<header class="ph"><div class="ph-l"><h1>{_esc(title)}</h1></div>'
              f'<div class="ph-actions">{search}'
              f'<span class="updated">更新 {_esc(data_mtime(data_dir))}</span>'
              '<span class="ro">読み取り専用</span></div></header>')
    main = (f'<div class="ri-ws">{_ri_nav(active)}'
            f'<main class="ri-main">{header}{body}</main></div>')
    return _deemoji(_ri_shell(active_route, title, main, scripts=SCRIPT))


# ---------------------------------------------------------------------------
# あいのて Phase 2 UI (workspace / juusetsu)
# ---------------------------------------------------------------------------


def _sovereignty_badge(data_dir: Path) -> str:
    """データ主権バッジ＝今データが端末外に出る経路を env の事実から開示（監査可能化）。"""
    from hub_core import sovereignty
    st = sovereignty.status(data_dir)
    if st["sovereign"]:
        return ('<div class="sov-badge sov-ok" title="保存先やOSの同期設定は別に確認してください">'
                '<span class="sov-dot"></span>あいのてからの外部送信なし</div>')
    flows = st.get("flows") or []
    detail = "／".join(f'{_esc(f["channel"])}→{_esc(f["to"])}' for f in flows)
    return ('<div class="sov-badge sov-warn" title="' + _esc(detail) + '">'
            '<span class="sov-dot"></span>' + _esc(st["label"]) + '</div>')


def _ai_key_state() -> str:
    for name in ("RI_OS_AI_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(name):
            return "AIキー検出済（NL解釈は外部クライアント側で実行）"
    return "AIは未接続（そのまま手動で使えます）"


def _nav_active(active_route: str) -> str:
    """ルート文字列 → サイドバーで点灯させる active キー。"""
    r = (active_route or "").rstrip("/") or "/"
    return ui.ROUTE_ACTIVE.get(r, ui.ROUTE_ACTIVE.get("/" + r.lstrip("/"), ""))


def _ri_shell(active: str, title: str, body: str, scripts: str = "") -> str:
    """統一シェル。body は <div class="ri-ws">{_ri_nav()}<main class="ri-main">…</main></div> 形。
    偽ブラウザ枠は撤去。全画面が ui.APP_CSS の単一スタイルを通る。"""
    script_html = f'<script>{scripts}</script>' if scripts else ""
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="icon" href="/favicon.svg">'
        f'<title>あいのて | {_esc(title)}</title><style>{ui.APP_CSS}</style></head><body>'
        f'{body}{script_html}</body></html>'
    )


def _ri_nav(active: str, juusetsu_case: str = "") -> str:
    """単一サイドバー(ui.sidebar)。ナビの行き先は全て同一シェルの実ページ。"""
    v = current_viewer()
    return ui.sidebar(
        active,
        viewer_role=(v.role if v else None),
        viewer_user=(v.user if v else None),
        show_logout=bool(v and not getattr(v, "is_dev", False)),
        juusetsu_case=juusetsu_case,
    )


def _load_rows_for_ui(data_dir: Path, source: str):
    try:
        return load_source(data_dir, source)
    except Exception:
        if source == "tasks":
            headers, rows = read_csv(data_dir / TASKS_FILE)
            return headers, _authz_rows(rows)
        if source.startswith("csv:"):
            headers, rows = read_csv(data_dir / source[len("csv:"):])
            return headers, _authz_rows(rows)
        if source.startswith("jsonl:"):
            return read_jsonl(data_dir / source[len("jsonl:"):])
    return [], []


def _row_title(row: dict) -> str:
    return (row.get(COL_TITLE) or row.get("確認対象") or row.get("action") or
            row.get("物件名") or row.get("title") or "作業")


def _status_badge(status: str) -> str:
    s = (status or "").lower()
    if s in ("pass", "done", "ok", "finalized", "確定済", "approved"):
        return '<span class="ri-badge ok">確定済</span>'
    if s in ("hold", "blocked", "expired"):
        return '<span class="ri-badge bad">停止</span>'
    return '<span class="ri-badge warn">要確認</span>'


# 業務の流れ（反響→契約）。各段はその作業を担う画面へのリンク。ホームに1枚置き、どの画面が
# どの工程かを一目で分かるようにする（迷子防止）。すべて内部リンク（外部URLを出さない）。
_FLOW_STAGES = [("反響", "/line"), ("ヒアリング", "/customers"), ("物件提案", "/properties"),
                ("内見", "/viewings"), ("申込", "/applications"), ("IT重説", "/it"), ("契約", "/contracts")]


def _flow_map_html() -> str:
    chip = ('display:inline-flex;align-items:center;min-height:var(--ai-hit,48px);'
            'padding:0 18px;border:1px solid var(--line,#e2e6ea);'
            'border-radius:999px;background:var(--panel,#fff);font-size:18px;'
            'color:var(--sumi,#0a2540);text-decoration:none;white-space:nowrap')
    parts = []
    for i, (label, href) in enumerate(_FLOW_STAGES):
        if i:
            parts.append('<span aria-hidden="true" style="color:var(--muted,#98a2b3);'
                         'font-weight:700">→</span>')
        parts.append(f'<a href="{href}" style="{chip}">{_esc(label)}</a>')
    return ('<div class="ri-sech">業務の流れ</div>'
            '<div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap;'
            'padding:10px 0 2px">' + "".join(parts) + '</div>'
            '<div class="gn" style="margin-top:5px">各段をクリックすると担当の画面に移動します。'
            '反響はLINEのほか<a href="/leads" style="text-decoration:underline;display:inline-flex;align-items:center;min-height:var(--ai-hit,48px);padding:0 4px">ポータル反響</a>'
            'からも入ります。</div>')


def _AI_FUDAS(case_id: str):
    """窓口の大札。動詞（〜する）で並べる。機能名でなく、その人がやりたいことで呼ぶ。"""
    jq = ("?case=" + quote(case_id)) if case_id else ""
    return [
        ("/maisoku/new-form", "マイソクを作る", "販売図面をつくって印刷する"),
        ("/juusetsu/new" + jq, "重要事項説明書を作る", "35条の書面を下書きして、記名して確定する"),
        ("/leads", "お客様の問い合わせを見る", "ポータル・メール・電話・LINE の反響"),
        ("/keisan", "お金を計算する", "仲介手数料・初期費用・精算を出す"),
        ("/properties", "物件を調べる", "台帳の物件・行政資料・ハザードを見る"),
        ("/maisoku", "書類を印刷する", "作った書類を開いて印刷（PDF）する"),
    ]


_AI_STEPS = ("えらぶ", "入れる", "たしかめる", "できあがり")


def _ai_rail4(step: int) -> str:
    """接ぎ木②「いまここバー」。業務動線の一本線で、いまどの段にいるかを示す。"""
    li = []
    for i, name in enumerate(_AI_STEPS):
        cls = "done" if i < step else ("now" if i == step else "")
        current = ' aria-current="step"' if i == step else ""
        li.append(
            f'<li class="{cls}"{current}>'
            '<span class="ai-knot" aria-hidden="true"><i></i><i></i></span>'
            f'<span>{_esc(name)}</span></li>'
        )
    return '<ol class="ai-rail4" aria-label="書類づくりの進み具合">' + "".join(li) + "</ol>"


def _first_run_knotline(data_dir: Path, cases: list[dict], audit: list[dict]) -> str:
    """実データだけで示す初回一周。装飾用の偽進捗は作らない。"""
    _, customers = _load_rows_for_ui(data_dir, "csv:customers.csv")
    has_property = False
    try:
        from hub_core.store import SqliteStore
        db = Path(data_dir) / "hub.db"
        has_property = bool(SqliteStore(db).query("properties")) if db.is_file() else False
    except Exception:
        has_property = False
    has_customer = bool(customers)
    has_link = any(
        str(row.get("顧客ID") or row.get("customer_id") or "").strip()
        and str(row.get("物件ID") or row.get("property_id") or "").strip()
        for row in cases
    )
    has_line = any(
        row.get("action") == "connection_tested"
        and row.get("target") == "line"
        and row.get("gate_status") == "connected"
        for row in audit
    )
    try:
        from hub_core.documents import list_documents
        real_kinds = {
            str(row.get("kind") or "") for row in list_documents(data_dir)
            if not row.get("sample")
        }
    except Exception:
        real_kinds = set()
    has_documents = {"maisoku", "juusetsu"}.issubset(real_kinds)
    audit_actions = {str(row.get("action") or "") for row in audit}
    has_confirmable_flow = {
        "property_registered", "customers_imported", "customer_case_created", "connection_tested",
    }.issubset(audit_actions)

    stages = [
        ("会社", True, "/profile", "会社情報を見る"),
        ("物件", has_property, "/properties", "物件を登録する"),
        ("顧客", has_customer, "/migrate", "顧客名簿を取り込む"),
        ("接続", has_link and has_line, "/connections", "顧客と物件・LINEを接続する"),
        ("書類", has_documents, "/maisoku/new-form", "マイソクと重説を作る"),
        ("確認", has_documents and has_confirmable_flow, "/audit", "監査と出力を確認する"),
    ]
    first_open = next((i for i, (_label, done, _href, _next) in enumerate(stages) if not done),
                      len(stages))
    nodes = []
    for i, (label, done, _href, _next) in enumerate(stages):
        cls = "done" if done and i < first_open else ("now" if i == first_open else "")
        current = ' aria-current="step"' if i == first_open else ""
        nodes.append(
            f'<li class="{cls}"{current}>'
            '<span class="ai-knot" aria-hidden="true"><i></i><i></i></span>'
            f'<span>{_esc(label)}</span></li>'
        )
    if first_open < len(stages):
        _label, _done, href, next_label = stages[first_open]
        next_html = f'次は <a href="{_esc(href)}">{_esc(next_label)} →</a>'
    else:
        next_html = '<span>一周つながりました。監査ログで記録を確認できます。</span>'
    return (
        '<section class="ai-first" aria-labelledby="ai-first-title">'
        '<div class="ai-first-head"><div id="ai-first-title" class="ai-first-title">'
        'はじめの一周</div>'
        f'<div class="ai-first-next">{next_html}</div></div>'
        '<ol class="ai-knotline" aria-label="初回利用の進み具合">'
        + "".join(nodes) + '</ol></section>'
    )


def ai_held(name: str, back_href: str = "/home") -> str:
    """接ぎ木②の後半。選んだ札を横倒しの帯として作業画面の上部に残す（迷子防止）。"""
    return ('<div class="ai-held"><span class="lab">いましている仕事</span>'
            f'<span class="nm">{_esc(name)}</span>'
            f'<a class="ai-btn quiet bk" href="{_esc(back_href)}">窓口にもどる</a></div>')


# 滞留の帯の満尺（日）。これを超えたら朱に反転する。
_AI_STUCK_FULL_DAYS = 7.0
_AI_STUCK_ALERT_DAYS = 3.0


def _ai_days_since(iso_ts: str):
    """作成日時(ISO) からの経過日数。読めなければ None（推測しない）。"""
    s = (iso_ts or "").strip()
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return max(0.0, (datetime.datetime.now(JST) - dt).total_seconds() / 86400.0)


def _ai_dl_bar(days) -> str:
    """接ぎ木①「締切の帯」を、実データに合わせて**滞留の帯**として実装する。

    台帳(tasks/hold/approval/cases)に期限列が存在しないため、締切を発明しない（捏造ガード）。
    代わりに実在する 作成日時 からの経過を長さで見せる。数字を読ませず、長いほど悪い。
    経過が読めない行では帯を出さない（空の帯を描かない）。
    """
    if days is None:
        return ""
    pct = min(100, int(round(days / _AI_STUCK_FULL_DAYS * 100)))
    over = days >= _AI_STUCK_ALERT_DAYS
    n = int(days)
    label = (f"{n}日 止まっています" if over else
             ("きょう受け付け" if n == 0 else f"{n}日目"))
    cls = "ai-dl over" if over else "ai-dl"
    return (f'<div class="{cls}"><span class="bar"><i style="width:{max(pct, 3)}%"></i></span>'
            f'<span class="lb">{_esc(label)}</span></div>')


def _ai_stuck_section(data_dir: Path, tasks, approval_rows, holds) -> str:
    """手が止まっているもの（保留＋承認待ち）を、滞留の帯つきで上位に出す。"""
    by_task = {(r.get(COL_TASK_ID) or ""): r for r in tasks}

    def created_of(row):
        t = by_task.get((row.get("タスクID") or "").strip())
        return _ai_days_since(t.get(COL_CREATED, "")) if t else None

    items = []
    for r in holds:
        items.append(("保留", r.get("理由") or r.get("保留種別") or "理由未記入",
                      r.get("解除条件") or "", created_of(r), "/hold"))
    for r in approval_rows:
        items.append((f'{r.get("承認役割") or "担当"}の承認待ち', r.get("理由") or "",
                      "", created_of(r), "/approval"))
    if not items:
        return ""
    items.sort(key=lambda x: (-(x[3] or 0)))
    rows = "".join(
        f'<a class="row" href="{href}"><p class="rt">{_esc(kind)}</p>'
        f'<p class="rm">{_esc(why)}{("　解除条件: " + _esc(cond)) if cond else ""}</p>'
        f'{_ai_dl_bar(days)}</a>'
        for kind, why, cond, days, href in items[:5])
    more = (f'<a class="row" href="/hold"><p class="rt">ほか {len(items) - 5} 件を見る</p></a>'
            if len(items) > 5 else "")
    return ('<div class="ai-sech">手が止まっているもの</div>'
            f'<div class="ai-stuck">{rows}{more}</div>')


def _ai_case_ledger(cases) -> str:
    """接ぎ木③「台帳SPEC部品」。横罫のみ・右寄せ等幅・物件名は明朝。
    mono はラテン内容（案件ID）にだけ当てる（和文に当てると等幅にならず書体が不揃いになる）。"""
    if not cases:
        return ui.empty("物件はまだありません。「ことばで頼む」から登録できます。")
    tr = []
    for r in cases[:8]:
        cid = (r.get("案件ID") or "").strip()
        tr.append(
            f'<tr onclick="location.href=\'/case?id={quote(cid)}\'" style="cursor:pointer">'
            f'<td class="mono" data-label="案件">{_esc(cid)}</td>'
            f'<td data-label="物件"><span class="nm">{_esc(r.get("物件名") or "(物件名未設定)")}</span>'
            f'<span class="sub">{_esc(r.get("顧客名") or "")}</span></td>'
            f'<td data-label="種別">{_esc(r.get("取引種別") or "")}</td>'
            f'<td data-label="状態">{_esc(r.get("状態") or "")}</td></tr>')
    return ('<div class="ai-specwrap"><table class="ai-spec"><thead><tr>'
            '<th>案件</th><th>物件</th><th>種別</th><th>状態</th>'
            f'</tr></thead><tbody>{"".join(tr)}</tbody></table></div>')


def render_ri_workspace(data_dir: Path, params) -> str:
    _, tasks = _load_rows_for_ui(data_dir, "tasks")
    _, approvals = _load_rows_for_ui(data_dir, "csv:approval_queue.csv")
    _, holds = _load_rows_for_ui(data_dir, "csv:hold_queue.csv")
    _, cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
    _, audit = _load_rows_for_ui(data_dir, "jsonl:audit_log.jsonl")
    today_rows = [r for r in tasks if _q(r, "Today")][:6]
    approval_rows = [r for r in approvals if (r.get("判断") or "pending") == "pending"]
    ju_ctx = load_juusetsu_context(data_dir, (params.get("case", [""])[0] or ""))

    kpis = [
        (sum(1 for r in tasks if _q(r, "Today")), "今日のタスク", "/today"),
        (len(approval_rows), "承認待ち", "/approval"),
        (len(holds), "保留", "/hold"),
        (len(cases), "進行中案件", "/properties"),
    ]
    kpi_html = "".join(
        f'<a class="kpi" href="{href}"><div class="n">{n}</div><div class="l">{_esc(label)}</div></a>'
        for n, label, href in kpis
    )

    # ---- S1 台帳ホーム（Ledger & Paper）: 要対応帳 + 案件帳 + 監査台帳レール ----
    # 要対応帳: P0/P1 の Today/Hold タスク（期限列はデータに無いものは出さない=捏造しない）
    attn_rows = [r for r in tasks if r.get(COL_PRIORITY) in ("P0", "P1")
                 and (_q(r, "Today") or _q(r, "Hold"))]
    attn_rows.sort(key=lambda r: (r.get(COL_PRIORITY, "P9"), r.get("キュー", "")))
    attn_tr = []
    for r in attn_rows[:8]:
        prio = r.get(COL_PRIORITY, "")
        ref = r.get(COL_PROP_REF) or r.get(COL_LEAD_ID) or ""
        href = "/case?id=" + quote(ref) if ref else "/today"
        _plabel = {"P0": "至急", "P1": "要対応", "P2": "通常"}.get(prio, prio)
        pcell = (f'<span class="due-over">{_esc(_plabel)}</span>' if prio == "P0" else _esc(_plabel))
        attn_tr.append(
            f'<tr onclick="location.href=\'{href}\'" style="cursor:pointer">'
            f'<td>{pcell}</td><td>{_esc(r.get(COL_TITLE, ""))}</td>'
            f'<td class="caselink hm">{_esc(ref)}</td>'
            f'<td class="hm">{_esc(r.get("キュー", ""))}</td><td>{_esc(r.get("担当", ""))}</td></tr>')
    attn_tbl = ('<div class="tablewrap"><table><thead><tr><th>優先</th><th>作業</th>'
                '<th class="hm">参照</th><th class="hm">キュー</th><th>担当</th></tr></thead>'
                f'<tbody>{"".join(attn_tr)}</tbody></table></div>' if attn_tr
                else '<div class="ri-empty">いま要対応の案件はありません。</div>')

    # 案件帳: cases.csv の実列のみ
    case_tr = []
    for r in cases[:8]:
        cid = r.get("案件ID", "")
        case_tr.append(
            f'<tr onclick="location.href=\'/case?id={quote(cid)}\'" style="cursor:pointer">'
            f'<td class="caselink">{_esc(cid)}</td><td>{_esc(r.get("物件名", ""))}</td>'
            f'<td>{_esc(r.get("顧客名", ""))}</td><td>{_esc(r.get("取引種別", ""))}</td>'
            f'<td>{_esc(r.get("状態", ""))}</td></tr>')
    case_tbl = ('<div class="tablewrap"><table><thead><tr><th>案件</th><th>物件</th>'
                '<th>顧客</th><th>種別</th><th>状態</th></tr></thead>'
                f'<tbody>{"".join(case_tr)}</tbody></table></div>' if case_tr
                else '<div class="ri-empty">案件はありません（「ことばで頼む」から登録できます）</div>')

    # 監査台帳レール（右端常設）: 直近の署名イベント。時刻+操作+短縮ハッシュ。
    def _aev_label(act: str) -> str:
        return _audit_action_label(act)

    # 連続する同種イベント（取込バッチ等）は1行に集約＝開発ログの生ダンプを製品面に出さない
    grouped = []
    for ev in list(reversed(audit))[:40]:
        act = ev.get("action", "")
        kind = "取込" if act.endswith("_outputs_ingested") else act
        if grouped and grouped[-1]["kind"] == kind and kind == "取込":
            grouped[-1]["count"] += 1
        else:
            grouped.append({"kind": kind, "ev": ev, "count": 1})
    aev_html = []
    for g in grouped[:10]:
        ev = g["ev"]
        ts = (ev.get("timestamp") or "")
        tshort = ts[11:16] if len(ts) >= 16 else ts
        act = ev.get("action", "")
        fin = " fin" if act == "finalized_with_signature" else ""
        if g["kind"] == "取込" and g["count"] > 1:
            label, hdiv = "サンプル出力の取込", f'<span class="n">×{g["count"]}</span>'
        else:
            label = _aev_label(act)
            hdiv = '<div class="h">記録済み</div>'
        aev_html.append(f'<div class="aev{fin}"><span class="t">{_esc(tshort)}</span> '
                        f'<span class="act">{_esc(label)}</span>{hdiv}</div>')
    rail = ('<div class="ri-right-h">最近の作業・監査台帳</div>'
            + ("".join(aev_html) or '<div class="rc-empty">まだ記録はありません。確定や入金を行うとここに残ります。</div>')
            + '<div class="gn" style="margin-top:10px">確定・入金などの記録は改ざん検知つきで残ります。全件は<a href="/audit" style="text-decoration:underline">監査ログ</a>へ。</div>')

    # ---- 窓口型ホーム ----
    # 動詞の大札を1枚選ぶと、その仕事だけが目の前に出る。数字の一覧は主役から降ろす。
    fudas = "".join(
        f'<a class="ai-fuda" href="{href}"><p class="t">{_esc(t)}</p>'
        f'<p class="d">{_esc(d)}</p><span class="go">えらぶ →</span></a>'
        for href, t, d in _AI_FUDAS(ju_ctx["case"]))

    body = (
        '<div class="ri-ws ri-ws3">'
        f'{_ri_nav("home", ju_ctx["case"])}'
        '<div class="ri-main">'
        f'{_first_run_knotline(data_dir, cases, audit)}'
        '<h1 class="ai-ask">きょうは何をしますか？</h1>'
        '<p class="ai-ask-s">札を1枚えらぶと、その仕事だけを順に聞いていきます。</p>'
        f'<div class="ai-fudas">{fudas}</div>'
        f'{_ai_stuck_section(data_dir, tasks, approval_rows, holds)}'
        '<a class="ri-cmd" href="/console"><div class="ri-ai"></div>'
        '<div class="ri-cmd-text">ことばで頼む（話しかけて操作します）</div>'
        '<span class="ri-send" aria-hidden="true"></span></a>'
        f'<div class="ri-ai-state">{_esc(_ai_key_state())}</div>'
        f'{_sovereignty_badge(data_dir)}'
        f'{_home_attn(data_dir)}'
        f'{_flow_map_html()}'
        # 平易な語を先に、専門語は括弧で併記する（brandbook トーン&ボイス）。
        '<div class="ai-sech">きょうの数（ダッシュボード）</div>'
        f'<div class="ri-kpis">{kpi_html}</div>'
        '<div class="ai-sech">要対応</div>'
        f'{attn_tbl}'
        '<div class="ai-sech" style="margin-top:22px">物件の帳面</div>'
        f'{_ai_case_ledger(cases)}'
        '<div class="ri-trust"><span class="gn">この仕組みが必ず守ること：'
        '書面は担当者の記名で確定・抽出値は出典ページを明記・個人情報の外部送信経路は画面に表示・'
        '記録は改ざん検知つきの台帳に残ります。</span></div>'
        '</div>'
        f'<aside class="ri-right">{rail}</aside>'
        '</div>'
    )
    return _ri_shell("/home", "ホーム", body)


def _resolve_source_path(data_dir: Path, value: str) -> Path | None:
    raw = (value or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    candidates = [p]
    if not p.is_absolute():
        candidates += [data_dir / raw, ROOT / raw]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_kakunin_path(data_dir: Path, draft_path: Path | None) -> Path | None:
    candidates = []
    if draft_path is not None:
        candidates.append(draft_path.parent / "kakunin_list.csv")
    candidates += [
        data_dir / "kakunin_list.csv",
        data_dir / "chousa" / "kakunin_list.csv",
    ]
    if os.environ.get("AINOTE_SHOW_SAMPLES") == "1":
        candidates.append(ROOT / "sample_chousa_out" / "kakunin_list.csv")
    for p in candidates:
        if p.exists():
            return p
    return None


def _draft_paths(data_dir: Path) -> list[Path]:
    """重説ドラフトの置き場。**同梱の見本はデータが1件も無いときだけ**見せる。

    見本（sample_chousa_out）を常に混ぜると、自分で作った覚えのない「架空市の戸建」が
    『現在のドラフト』として出て、そのまま記名確定できてしまう。確定は監査台帳に
    実在の宅建士名で残り、append-only なので消せない。初日の店主が最初に触る画面で
    これが起きるのは事故に近い。
    """
    paths = []
    for base in (data_dir, data_dir / "chousa"):
        if base.exists():
            paths.extend(sorted(base.glob("juusetsu_draft_*.md")))
    if paths:
        return paths
    if os.environ.get("AINOTE_SHOW_SAMPLES") == "1":
        return sorted((ROOT / "sample_chousa_out").glob("juusetsu_draft_*.md"))
    return []


def load_juusetsu_context(data_dir: Path, case_id: str = "") -> dict:
    case_id = (case_id or "").strip()
    _, ledger_rows = read_csv(data_dir / "approval_ledger.csv")
    candidate_rows = [
        r for r in ledger_rows
        if "重説" in (r.get("確認対象", "") + r.get("理由", "") + r.get("元データ", ""))
        or "juusetsu_draft" in r.get("元データ", "")
    ]
    if case_id:
        matched = [r for r in candidate_rows if any(case_id in str(v) for v in r.values())]
        if matched:
            candidate_rows = matched

    draft_path = None
    ledger_row = candidate_rows[0] if candidate_rows else {}
    if ledger_row:
        draft_path = _resolve_source_path(data_dir, ledger_row.get("元データ", ""))
    if draft_path is None:
        paths = _draft_paths(data_dir)
        if case_id:
            paths = [p for p in paths if case_id in p.name or case_id in p.stem] or paths
        draft_path = paths[0] if paths else None

    draft_text = ""
    if draft_path is not None and draft_path.exists():
        draft_text = draft_path.read_text(encoding="utf-8")
        # 表示層の正規化: ri-chousa生成ドラフトの定型警告バナー先頭の警告グリフ(U+26A0)は
        # UIでは文字装飾で表現する（GATE-PV R2/敵対レンズのliteral emoji re-cap条件）。
        # 書類の実質文言は変えない（グリフのみ・根本対応=ri-chousaテンプレ修正はWave1 M-jusetsu）。
        draft_text = draft_text.replace("\u26a0 ", "").replace("\u26a0\ufe0f ", "").replace(" as_of ", " 基準日 ").replace("as_of ", "基準日 ")
    if not draft_text:
        draft_text = (
            "重説ドラフトが見つかりません。"
            "「重要事項説明書を新規作成」から下書きを作成してください。"
        )

    kakunin_path = _find_kakunin_path(data_dir, draft_path)
    _, checks = read_csv(kakunin_path) if kakunin_path else ([], [])
    if case_id:
        scoped = [r for r in checks if any(case_id in str(v) for v in r.values())]
        checks = scoped or checks

    selected_case = case_id
    if not selected_case and draft_path is not None:
        selected_case = draft_path.stem.replace("juusetsu_draft_", "") or draft_path.stem
    if not selected_case and ledger_row:
        selected_case = ledger_row.get("タスクID") or ledger_row.get("承認ID") or "JUUSETSU"
    selected_case = selected_case or "JUUSETSU"
    target_id = selected_case
    if draft_path is not None:
        target_id = draft_path.stem
    return {
        "case": selected_case,
        "target_id": target_id,
        "draft_path": draft_path,
        "draft_text": draft_text,
        "checks": checks,
        "kakunin_path": kakunin_path,
        "ledger_row": ledger_row,
    }


def _highlight_review_terms(text: str) -> str:
    out = _esc(text)
    for word in ("要確認", "出典未確認", "未確定", "ドラフト", "交付不可"):
        out = out.replace(_esc(word), f'<span class="hl">{_esc(word)}</span>')
    return out


def _render_draft_preview(text: str) -> str:
    clauses = []
    current_title = "ドラフト"
    current_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if current_lines:
                clauses.append((current_title, " ".join(current_lines)))
                current_lines = []
            current_title = line.lstrip("#").strip() or current_title
        else:
            current_lines.append(line)
    if current_lines:
        clauses.append((current_title, " ".join(current_lines)))
    if not clauses:
        clauses = [("ドラフト", text)]
    html_parts = []
    for title, body in clauses[:7]:
        html_parts.append(
            '<div class="ri-clause">'
            f'<div class="cn">{_esc(title)}</div><div class="cb">{_highlight_review_terms(body)}</div>'
            '</div>'
        )
    return "".join(html_parts)


def _latest_finalization(data_dir: Path, target_id: str) -> dict | None:
    _, rows = read_jsonl(data_dir / "audit_log.jsonl")
    for ev in reversed(rows):
        target = str(ev.get("target") or "")
        is_same_document = target == target_id
        if target.startswith(target_id + "#v"):
            is_same_document = target.removeprefix(target_id + "#v").isdigit()
        if ev.get("action") == "finalized_with_signature" and is_same_document:
            return ev
    return None


_PRS_JP = {
    "earthquake": "地震", "future_flood": "水害(浸水)", "flood": "洪水", "tsunami": "津波",
    "landslide": "土砂", "liquefaction": "液状化", "typhoon_wind": "暴風", "storm_surge": "高潮",
    "volcano": "火山", "heavy_snow": "大雪", "sea_level_rise": "海面上昇",
}


# 較正済みで数値表示してよいPRS種別（金メッキ禁止・DZ-2是正）。現状の較正健全種別=洪水のみ
# （current_flood_score・prs.py と同一規律）。他種別は未較正=grade非表示で「査定に含めない」に倒し、
# 物件詳細 _case_prs_block と規律を一本化する。
_PRS_CALIBRATED = ("flood", "future_flood")


def _prs_score_summary(resp: dict) -> str:
    """PRS実レスポンスを重説向けに清書（**較正済種別のみ数値表示**・金メッキ禁止）。
    未較正種別のgradeや「A側=低リスク」等の断定は出さない（法定ハザードマップ確認が正）。"""
    risks = resp.get("risks") or {}
    flood_shown = None
    for k in _PRS_CALIBRATED:
        e = risks.get(k)
        if not isinstance(e, dict):
            continue
        facts = e.get("facts") or {}
        sc = facts.get("current_flood_score")
        if sc is not None:
            flood_shown = "洪水 " + _esc(str(sc)) + "/100（較正済・浸水想定）"
            break
        g = (e.get("score") or {}).get("grade")
        if g is not None:
            flood_shown = "洪水 " + _esc(str(g)) + "（較正済）"
            break
    uncalibrated = [k for k in ("earthquake", "tsunami", "landslide", "liquefaction",
                                "typhoon_wind", "storm_surge")
                    if isinstance(risks.get(k), dict)]
    parts = []
    if flood_shown:
        parts.append(flood_shown)
    if uncalibrated:
        names = "・".join(_PRS_JP.get(k, k) for k in uncalibrated)
        parts.append('<span style="color:#8493a8">' + _esc(names)
                     + '=未較正（査定に含めません・法定ハザードマップで確認）</span>')
    if not parts:
        return ("較正済みの数値は取得できませんでした。水害ハザードは当該自治体公表の"
                "ハザードマップで確認・記載してください。")
    return "　".join(parts)



_ASSESS_CACHE: dict[str, dict] = {}


def _assess_cached(addr: str) -> dict:
    """住所ごとに1回だけ外部査定を呼び、以降はプロセス内で使い回す。

    同じ画面を開き直すたびに課金される状態を避ける。失敗も覚えて叩き直さない
    （壊れた接続に何度も投げない）。
    """
    key = addr.strip()
    if key in _ASSESS_CACHE:
        return _ASSESS_CACHE[key]
    try:
        from hub_core import prs as _prs
        assessed = _prs.assess(address=key)
        if assessed.get("prs_status") == "OK":
            r = {
                "prs_status": "connected",
                "geocoded": {"matched": key},
                "request": {"lat": assessed.get("lat"), "lon": assessed.get("lon")},
                "response": assessed.get("result") or {},
            }
        else:
            r = assessed
    except Exception:  # noqa: BLE001
        r = {"prs_status": "PRS未接続", "reason": "不動産リスク情報を取得できませんでした。"}
    _ASSESS_CACHE[key] = r
    return r


def _render_hazard_block(params, ctx: dict) -> str:
    connected = bool(os.environ.get("RISK_API_KEY") or os.environ.get("PRS_API_KEY"))
    addr = (params.get("addr", [""])[0] or "").strip()
    if not addr:
        lr = ctx.get("ledger_row") or {}
        for k in ("所在地", "住所", "物件所在地", "物件住所"):
            if lr.get(k):
                addr = str(lr[k]).strip()
                break
    src = ('<div class="src" style="margin-top:7px">出典: 不動産リスクスコア連携と国土地理院の位置情報。'
           '連携できない場合は数値を生成しません。最終判断は記名宅建士が行います。</div>')
    head = '<div class="hazard"><div class="hl">災害リスク欄'
    if not connected:
        return (head + '</div><div class="hv">水害ハザード／浸水想定・土砂・地震 '
                '<span class="soon">不動産リスクスコアの接続設定が未完了です</span></div>' + src + '</div>')
    if not addr:
        return (head + '</div><div class="hv">水害ハザード／浸水想定・土砂・地震 '
                '<span class="soon">住所を登録するとリスクを確認できます</span></div>' + src + '</div>')
    # 外部への問い合わせは**押したときだけ**。画面を開いただけで走ると、
    # 再読込のたびに外部送信と課金が積み上がる（GET中心=読み取り、という設計とも矛盾する）。
    if (params.get("assess", [""])[0] or "") != "1":
        from urllib.parse import urlencode as _ue
        q = _ue({"addr": addr, "assess": "1"})
        return (head + '</div><div class="hv">水害ハザード／浸水想定・土砂・地震 '
                f'<a class="ri-go ghost" href="?{q}">この住所でリスクを取得する</a>'
                '<span class="gn" style="margin-left:10px">押したときだけ外部へ問い合わせます</span>'
                '</div>' + src + '</div>')
    r = _assess_cached(addr)
    if r.get("prs_status") != "connected":
        return (head + '</div><div class="hv">水害ハザード／浸水想定・土砂・地震 '
                '<span class="soon">リスク情報を取得できませんでした。接続設定を確認してください。</span>'
                '</div>' + src + '</div>')
    geo = r.get("geocoded") or {}
    req = r.get("request") or {}
    resp = r.get("response") or {}
    models = (resp.get("input_analysis") or {}).get("models_enabled") or []
    matched = geo.get("matched") or addr
    lat, lon = req.get("lat"), req.get("lon")
    coord = f"{lat:.5f}, {lon:.5f}" if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else ""
    model_line = ""
    if models:
        model_line = (f'<div class="id" style="margin-top:4px;color:#6b7280">評価モデル {len(models)}項目: '
                      f'{_esc("・".join(str(m) for m in models[:8]))} …</div>')
    return (head + ' <span class="ri-badge ok">PRS接続済</span></div>'
            f'<div class="hv">{_esc(matched)}　<span style="color:#6b7280">{_esc(coord)}</span></div>'
            f'<div class="id" style="margin-top:6px">{_prs_score_summary(resp)}</div>'
            + model_line + src + '</div>')


# 抽出済み調査データ(登記等)のキー → 重説フィールドへの写像（broker が持つ書類から再入力を消す）。
_EXTRACT_TO_JU = {
    "所在": "address", "所在地": "address", "住居表示": "address",
    "構造": "structure", "種類及び構造": "structure", "建物の名称": "property_name",
    "床面積": "area", "専有面積": "area", "面積": "area", "間取り": "layout",
    "築年月": "built", "新築年月日": "built", "用途地域": "youto",
}


def _property_choices(data_dir: Path) -> list:
    """台帳の案件一覧（案件ID, 表示名, 取引種別）。フォームの「お客様・物件から選ぶ」ドロップダウン用。
    物件名があればそれを表示名に、無ければ**顧客名（物件未定）**にフォールバックする＝会話から作った
    顧客起点の案件（物件未定）もお客様名で選べる（案件IDを覚えなくてよい・会話ファースト）。"""
    _, rows = _load_rows_for_ui(data_dir, "csv:cases.csv")
    out = []
    for r in (rows if isinstance(rows, list) else []):
        cid = r.get("案件ID") or r.get("案件id") or ""
        if not cid:
            continue
        name = (r.get("物件名") or "").strip()
        if not name:
            cust = (r.get("顧客名") or "").strip()
            name = (cust + "（物件未定）") if cust else (cid + "（物件未定）")
        out.append({"case_id": cid, "name": name, "deal": r.get("取引種別") or ""})
    return out


def _property_options(data_dir: Path) -> list:
    """物件を名前で選ぶための一覧（cases.csv ＋ OCR収集の property_info を統合・案件ID重複は台帳優先）。"""
    opts = {}
    for c in _property_choices(data_dir):
        opts[c["case_id"]] = c["name"]
    import json as _j
    pdir = Path(data_dir) / "property_info"
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.json")):
            try:
                d = _j.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cid = p.stem
            opts.setdefault(cid, (d.get("property_name") or cid))
    return [{"case_id": k, "name": v} for k, v in opts.items()]


def _property_select_html(data_dir: Path, field_name: str, *, required: bool = True,
                          empty_hint: str = "") -> str:
    """案件を名前で選ぶ <select>（案件IDを覚えなくてよい）。
    案件が0件のとき: empty_hint があれば**会話から始める導線**を出す（案件IDを打たせない＝会話ファースト）。
    empty_hint 未指定は従来どおり手入力（CASE-...）へフォールバックする（後方互換）。"""
    style = 'font-size:18px;padding:6px 9px;border:1px solid var(--line);border-radius:6px;max-width:280px'
    req = " required" if required else ""
    opts = _property_options(data_dir)
    if not opts:
        if empty_hint:
            return ('<span class="gn" style="display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap">'
                    f'{_esc(empty_hint)}'
                    '<a class="ri-go ghost" href="/line" style="padding:3px 9px;font-size:18px">'
                    'LINEの会話から始める</a></span>')
        return (f'<input type="text" name="{field_name}" placeholder="CASE-..."{req} '
                f'style="{style}">')
    o = "".join(f'<option value="{_esc(c["case_id"])}">{_esc(c["name"])}</option>' for c in opts)
    return (f'<select name="{field_name}"{req} style="{style}">'
            f'<option value="">（お客様・物件を選ぶ）</option>{o}</select>')


def _property_prefill(data_dir: Path, case_id: str) -> dict:
    """案件ID → 重説フィールドの事前入力 dict（案件の物件名/取引種別＋物件フォルダの承認済み抽出データ）。"""
    fields = {}
    _, rows = _load_rows_for_ui(data_dir, "csv:cases.csv")
    prop_name = ""
    for r in (rows if isinstance(rows, list) else []):
        if (r.get("案件ID") or "") == case_id:
            prop_name = r.get("物件名") or ""
            if prop_name:
                fields["property_name"] = prop_name
            deal = r.get("取引種別") or ""
            if deal:
                fields["deal_type"] = deal
            break
    if not prop_name:
        return fields
    # 物件フォルダの承認済み抽出(登記等)から所在/構造/面積等を補完
    import json as _json
    folder = Path(data_dir) / "物件" / prop_name / "調査"
    if folder.is_dir():
        for jf in sorted(folder.glob("*.extract.json")):
            try:
                data = _json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("status") != "approved":
                continue
            for f in (data.get("fields") or []):
                jk = _EXTRACT_TO_JU.get(str(f.get("key") or "").strip())
                v = str(f.get("value") or "").strip()
                if jk and v and not fields.get(jk):
                    fields[jk] = v
    return fields


def _juusetsu_deal_axis_html(case_q: str, deal_effective: str, tx, pk_raw: str, pk, case_derived: bool) -> str:
    """会話ファーストの取引軸ブロック（設計 §5・M4/M5）。

    - transaction 確定時: 取引態様を編集不可の確定表示にし「取引種別を変更」でのみ解除（二重選択の解消＝M4）。
    - transaction 未確定時: 「売買ですか、賃貸ですか」の1問だけ聞く（M5 Q1）。
    - property_kind: transaction 確定後にだけ、未確定なら「土地/戸建/区分/一棟」を1問聞く（M5 Q2）。確定なら確定表示。
    様式選択そのものは deal_taxonomy（正規形 transaction × property_kind）に寄せる。推測補完はしない。
    """
    from hub_core import deal_taxonomy as _tax
    parts = []
    # --- 取引の別（transaction）---
    if tx is not None:
        yoshiki = "賃貸" if tx == "lease" else "売買"
        src = "案件から確定" if case_derived else "選択済み"
        unlock = ("var e=document.getElementById('j-deal_type');e.readOnly=false;"
                  "e.style.background='#fff';e.focus();return false;")
        parts.append(
            '<div class="pf-f" style="grid-column:1/-1">'
            '<label class="pf-l" for="j-deal_type">取引態様</label>'
            f'<input type="text" id="j-deal_type" name="deal_type" value="{_esc(deal_effective)}" readonly '
            'style="background:#eef1f4;color:#111418">'
            f'<div class="gn" style="font-size:18px;margin-top:3px">{src}（{yoshiki}様式で作成）・'
            f'<a href="#" onclick="{unlock}">取引種別を変更</a></div></div>')
    else:
        nav = "location.href='/juusetsu/new?" + case_q + "deal_type='+encodeURIComponent(this.value)"
        parts.append(
            '<div class="pf-f pf-ask" style="grid-column:1/-1">'
            '<label class="pf-l">取引態様 — まず1つだけ確認します</label>'
            '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:19px;margin-top:4px">'
            '<span>売買ですか、賃貸ですか？</span>'
            f'<label class="ms-choice"><input type="radio" name="deal_type" value="売買" onchange="{nav}"><span>売買</span></label>'
            f'<label class="ms-choice"><input type="radio" name="deal_type" value="賃貸" onchange="{nav}"><span>賃貸</span></label>'
            '</div></div>')
    # --- 物件種別（property_kind）--- transaction 確定後にだけ扱う（1問ずつ・§5.2）
    if tx is not None:
        if pk is not None:
            klabel = _tax.PROPERTY_KIND_LABELS.get(pk, pk_raw)
            unlockp = ("var e=document.getElementById('j-property_kind');e.readOnly=false;"
                       "e.style.background='#fff';e.focus();return false;")
            parts.append(
                '<div class="pf-f" style="grid-column:1/-1">'
                '<label class="pf-l" for="j-property_kind">物件の種別</label>'
                f'<input type="text" id="j-property_kind" name="property_kind" value="{_esc(pk_raw or klabel)}" readonly '
                'style="background:#eef1f4;color:#111418">'
                f'<div class="gn" style="font-size:18px;margin-top:3px">確定（{_esc(klabel)}）・'
                f'<a href="#" onclick="{unlockp}">変更</a></div></div>')
        else:
            navp = ("location.href='/juusetsu/new?" + case_q + "deal_type=" + quote(deal_effective)
                    + "&property_kind='+encodeURIComponent(this.value)")
            radios = "".join(
                f'<label class="ms-choice"><input type="radio" name="property_kind" value="{_esc(v)}" onchange="{navp}">'
                f'<span>{_esc(v)}</span></label>'
                for v in ("土地", "戸建", "区分", "収益一棟"))
            parts.append(
                '<div class="pf-f pf-ask" style="grid-column:1/-1">'
                '<label class="pf-l">物件の種別（あと1問）</label>'
                '<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:19px;margin-top:4px">'
                '<span>土地・戸建・区分所有・一棟のどれですか？</span>' + radios + '</div></div>')
    return "".join(parts)




def _juusetsu_axis(data_dir, params, prefill=None):
    """取引軸（売買/賃貸・物件種別）の確定状態と描画HTMLを返す。窓口型と一枚もので共用。"""
    from hub_core import deal_taxonomy as _tax
    sel_case = (params.get("case", [""])[0] or "").strip()
    dt_param = (params.get("deal_type", [""])[0] or "").strip()
    pk_param = (params.get("property_kind", [""])[0] or "").strip()
    pf = prefill if prefill is not None else (_property_prefill(data_dir, sel_case) if sel_case else {})
    case_deal = pf.get("deal_type", "") if sel_case else ""
    deal_effective = dt_param or case_deal
    tx = _tax.normalize_transaction(deal_effective)
    pk = _tax.normalize_property_kind(pk_param)
    case_derived = bool(sel_case and _tax.normalize_transaction(case_deal))
    case_q = ("case=" + quote(sel_case) + "&") if sel_case else ""
    return _juusetsu_deal_axis_html(case_q, deal_effective, tx, pk_param, pk, case_derived)


def _take_ocr_prefill(data_dir: Path, params, stash_name: str, mapper) -> tuple[dict, str]:
    """Consume one OCR handoff and return form fields plus a user-visible review note."""
    if (params.get("from", [""])[0] or "") != "ocr":
        return {}, ""
    stash = Path(data_dir) / stash_name
    if not stash.is_file():
        return {}, ""
    raw = {}
    try:
        raw = json.loads(stash.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {}
    finally:
        try:
            stash.unlink()
        except OSError:
            pass
    fields = {k: v for k, v in mapper(raw).items() if str(v or "").strip()}
    n_read = len([v for v in raw.values() if v])
    if n_read:
        note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #2e7d32">'
                f'写真から <b>{n_read}項目</b>を読み取って下に入れました'
                '（無料・ローカル・クラウド送信なし）。'
                '<b>値は必ず現物と照合してください</b>（OCRは下書き補助）。</div>')
    else:
        note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
                'この画像からは物件情報を読み取れませんでした（<b>推測で埋めません</b>）。'
                '販売図面の写真を使うか、下に手で入力してください。</div>')
    return fields, note


def _params_with_prefill(params, prefill: dict) -> dict:
    """Copy parse_qs-style params and fill only values the user has not already supplied."""
    merged = {
        key: list(value) if isinstance(value, list) else [value]
        for key, value in (params or {}).items()
    }
    for key, value in (prefill or {}).items():
        current = (merged.get(key, [""])[0] or "").strip()
        if value and not current:
            merged[key] = [str(value)]
    return merged


def _wizard_page(*, steps, idx, labels, answered, extra_hidden, post_action, list_url,
                 quit_label, nav_url, title_for_shell, active, note_html="",
                 head_html_for_step=""):
    """窓口型（1画面1動作）の共通描画。マイソクと重説で同じ体験にする。

    steps = [(key, 見出し, (項目キー…), 補足文), …]
    途中の段は GET で次の段へ、最終段だけ post_action へ既存と同じ形で送る
    （後端を変えない＝回帰リスクを持ち込まない・戻っても再送信が起きない）。
    """
    key, title, fields, help_text = steps[idx]
    dots = "".join(f'<span class="ms-dot{" on" if i <= idx else ""}"></span>'
                   for i in range(len(steps)))
    bar = (f'<div class="ms-steps"><div class="ms-dots">{dots}</div>'
           f'<div class="ms-count">{idx + 1} / {len(steps)}</div></div>')
    rows = ""
    for k in fields:
        rows += (f'<div class="ms-row"><label class="ms-l" for="w-{k}">'
                 f'{_esc(labels.get(k, k))}</label>'
                 f'<input class="ms-i" type="text" id="w-{k}" name="{k}" '
                 f'value="{_esc(answered.get(k, ""))}"></div>')
    hidden = "".join(f'<input type="hidden" name="{k}" value="{_esc(v)}">'
                     for k, v in answered.items() if k not in fields and v)
    hidden += extra_hidden
    last = idx == len(steps) - 1
    nxt = "" if last else f'<input type="hidden" name="step" value="{steps[idx + 1][0]}">'
    carry = "".join(f"&{k}={quote(v)}" for k, v in answered.items() if v)
    back = (f'<a class="ms-back" href="{list_url}">{_esc(quit_label)}</a>' if idx == 0 else
            f'<a class="ms-back" href="{nav_url}?step={steps[idx - 1][0]}{carry}">ひとつ前へ</a>')
    body = (f'{bar}<h1 class="ms-h">{_esc(title)}</h1>'
            f'<p class="ms-help">{_esc(help_text)}</p>{head_html_for_step}'
            f'<form class="ms-form" method="{"post" if last else "get"}" '
            f'action="{post_action if last else nav_url}">{rows}{hidden}{nxt}'
            f'<div class="ms-actions"><button class="ms-go" type="submit">'
            f'{"つくる" if last else "つぎへ"}</button>{back}</div></form>'
            f'{note_html if last else ""}')
    return _wrap_main(active, list_url, title_for_shell, f'<div class="ms-wrap">{body}</div>')


# 重説の窓口型。宅建業者・宅建士の欄は業者情報から自動で入るので**聞かない**。
JUUSETSU_STEPS = [
    ("torihiki", "どんな取引ですか", ("torihiki_keitai",),
     "売買か賃貸か、そして自社の立場（媒介・代理など）を選びます。"),
    ("bukken", "物件のことを教えてください", ("property_name", "address", "floors",
                                              "structure", "area", "layout", "built"),
     "登記や元の図面から写せる項目です。分からない欄は飛ばして構いません。"),
    # 欄名は juusetsu_draft.FIELD_KEYS と一致させる。ここが食い違うと入力が
    # どこにも入らず、画面には英語のキーがそのまま項目名として出る（renewal→koushinryo）。
    ("okane", "お金の条件を入れます", ("price", "rent", "kanri_fee", "shikikin", "reikin",
                                        "term", "koushinryo"),
     "売買は売買代金、賃貸は賃料など、該当する欄だけ入力します。"),
    ("setsubi", "設備のことを", ("water", "electric", "gas", "drainage"),
     "上下水道・電気・ガスの状況です。"),
    ("houki", "最後に、法令とハザード", ("youto", "kenpei_yoseki", "flood", "landslide"),
     "用途地域・建ぺい率/容積率・水害/土砂の区域です。ここは必ず一次資料で確認してください。"),
]


def render_juusetsu_step(data_dir: Path, params) -> str:
    """重説作成の窓口型。最終段で既存の POST /juusetsu/new/create へ同じ形で送る。"""
    from hub_core import juusetsu_draft as _jd
    from hub_core.auth import load_company
    company = load_company(data_dir, strict=True)
    ocr_prefill, ocr_note = _take_ocr_prefill(
        data_dir, params, ".juusetsu_ocr_prefill.json", _ocr_to_juusetsu)
    params = _params_with_prefill(params, ocr_prefill)
    labels = {k: l for k, l, _g in _jd.FIELDS}
    keys = [k for k, _l, _g in _jd.FIELDS]
    idx = 0
    want = (params.get("step", ["torihiki"])[0] or "torihiki").strip()
    for i, (k, _t, _f, _h) in enumerate(JUUSETSU_STEPS):
        if k == want:
            idx = i
            break
    answered = {k: (params.get(k, [""])[0] or "") for k in keys}
    # 宅建業者・宅建士は業者情報から自動。聞かずに hidden で持たせる。
    auto = {"company_name": company.get("name", ""), "license_no": company.get("license_no", ""),
            "company_address": company.get("address", ""), "company_tel": company.get("tel", ""),
            "association": company.get("association", ""),
            "takkenshi_name": company.get("staff", ""),
            "takkenshi_reg": company.get("takkenshi_reg", "")}
    for k, v in auto.items():
        if v and not answered.get(k):
            answered[k] = v

    extra = ""
    for k in ("case", "deal_type", "property_kind"):
        v = (params.get(k, [""])[0] or "").strip()
        if v:
            extra += f'<input type="hidden" name="{k}" value="{_esc(v)}">'

    note = ('<div class="ms-note">この重説は '
            f'<b>{_esc(company.get("name") or "（業者情報を登録してください）")}</b> '
            f'{_esc(company.get("license_no") or "")} の名義で作られます。'
            '宅地建物取引士が内容を確認し、記名して確定してください。</div>')
    # 取引軸（売買/賃貸・物件種別）は第1段で選ぶ。ここが決まらないと法定必須欄が決まらない。
    axis = _juusetsu_axis(data_dir, params) if idx == 0 else ""
    return _wizard_page(steps=JUUSETSU_STEPS, idx=idx, labels=labels, answered=answered,
                        extra_hidden=extra, post_action="/juusetsu/new/create",
                        head_html_for_step=ocr_note + axis,
                        list_url="/juusetsu", quit_label="やめる",
                        nav_url="/juusetsu/new", title_for_shell="重要事項説明書を作る",
                        active="juusetsu", note_html=note)


def render_juusetsu_new(data_dir: Path, params) -> str:
    """重要事項説明書の新規作成フォーム＝物件・契約情報を入力→決定論的に35条の下書きを生成。
    弱いLLMに本文を書かせない（法定様式に沿って組む）。貼り付けからの取り込みも可。"""
    from hub_core import juusetsu_draft as _jd
    from hub_core.auth import load_company
    company = load_company(data_dir, strict=True)   # 交付物の元。壊れた正本で空欄のまま進めない
    # 会社情報を初期値に（業者プロフィールから）
    prefill = {"company_name": company.get("name", ""), "license_no": company.get("license_no", ""),
               "company_address": company.get("address", ""), "company_tel": company.get("tel", ""),
               "association": company.get("association", ""), "takkenshi_name": company.get("staff", "")}
    # 台帳の物件を選んでいれば、案件＋登記等の抽出データから物件フィールドを事前入力（再入力を削る）。
    sel_case = (params.get("case", [""])[0] or "").strip()
    if sel_case:
        for k, v in _property_prefill(data_dir, sel_case).items():
            if v:
                prefill[k] = v
    # 写真OCRの読取結果があれば取り込み（マイソクと同じ無料ローカルOCR・1回きり・読んだら消す）。
    ocr_note = ""
    if (params.get("from", [""])[0] or "") == "ocr":
        stash = Path(data_dir) / ".juusetsu_ocr_prefill.json"
        if stash.is_file():
            try:
                import json as _j
                st = _j.loads(stash.read_text(encoding="utf-8"))
                for k, v in _ocr_to_juusetsu(st).items():
                    if v:
                        prefill[k] = v
                ocr_note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #2e7d32">'
                            f'写真から <b>{len([v for v in st.values() if v])}項目</b>を読み取って下に入れました'
                            '（無料・ローカル・クラウド送信なし）。<b>値は必ず現物・登記と照合してください</b>'
                            '（OCRは下書き補助・宅建士が交付前に確認）。</div>')
            except (OSError, ValueError):
                pass
            try:
                stash.unlink()
            except OSError:
                pass
    choices = _property_choices(data_dir)
    picker = ""
    if choices:
        opts = '<option value="">（新規に入力）</option>' + "".join(
            f'<option value="{_esc(c["case_id"])}"{" selected" if c["case_id"] == sel_case else ""}>'
            f'{_esc(c["name"])}（{_esc(c["deal"])}）</option>' for c in choices)
        picker = ('<div class="pf-set" style="padding:12px 16px"><label class="pf-l">台帳の物件から読み込む</label>'
                  '<select onchange="location.href=\'/juusetsu/new?case=\'+encodeURIComponent(this.value)" '
                  'style="padding:8px 11px;border:1px solid var(--line);border-radius:6px;font-size:19px;max-width:420px">'
                  + opts + '</select>'
                  '<div style="font-size:18px;color:var(--muted);margin-top:5px">'
                  '選ぶと、案件と登記等の調査データ（所在・構造・面積など）が自動で入ります。</div></div>')
    # 取引種別の会話ファースト処理（M4 確定表示・M5 1問フロー）。案件由来の deal_type は打ち直させない。
    from hub_core import deal_taxonomy as _tax
    dt_param = (params.get("deal_type", [""])[0] or "").strip()
    pk_param = (params.get("property_kind", [""])[0] or "").strip()
    case_deal = prefill.get("deal_type", "") if sel_case else ""
    deal_effective = dt_param or case_deal
    tx = _tax.normalize_transaction(deal_effective)
    pk = _tax.normalize_property_kind(pk_param)
    case_derived = bool(sel_case and _tax.normalize_transaction(case_deal))
    case_q = ("case=" + quote(sel_case) + "&") if sel_case else ""
    axis_html = _juusetsu_deal_axis_html(case_q, deal_effective, tx, pk_param, pk, case_derived)

    groups = {}
    for key, label, grp in _jd.FIELDS:
        groups.setdefault(grp, []).append((key, label))
    blocks = ""
    for grp, items in groups.items():
        fs = ""
        for key, label in items:
            if key == "deal_type":
                continue   # 取引軸は axis_html で描画（編集可能テキスト欄の二重選択を解消＝M4/M5）
            val = _esc(prefill.get(key, ""))
            fs += (f'<div class="pf-f"><label class="pf-l" for="j-{key}">{_esc(label)}</label>'
                   f'<input type="text" id="j-{key}" name="{key}" value="{val}"></div>')
        if grp == "取引":
            fs = axis_html + fs
        blocks += f'<fieldset class="pf-set"><legend>{_esc(grp)}</legend><div class="pf-grid">{fs}</div></fieldset>'
    paste = ('<fieldset class="pf-set"><legend>貼り付けから取り込む（任意）</legend>'
             '<div class="conn-guide">物件・契約情報をそのまま貼り付けて「取り込む」を押すと、'
             '分かる項目を上のフォームに自動で入れます（入らない項目は手で補ってください）。</div>'
             '<textarea id="jPaste" style="width:100%;min-height:110px;padding:9px 11px;border:1px solid var(--line);'
             'border-radius:6px;font-size:18px" placeholder="物件名: ...\n所在地: ...\n賃料: ..."></textarea>'
             '<button type="button" class="ri-qbtn" style="margin-top:8px" onclick="jParse()">取り込む</button>'
             '<span id="jParseMsg" class="gn"></span></fieldset>')
    js = ('<script>'
          'async function jParse(){var el=document.getElementById("jPaste");var m=document.getElementById("jParseMsg");'
          'm.textContent="取り込み中…";try{var r=await fetch("/juusetsu/new/parse",{method:"POST",'
          'headers:{"Content-Type":"application/json"},body:JSON.stringify({text:el.value})});var j=await r.json();'
          'var n=0;for(var k in j.fields){var f=document.getElementById("j-"+k);if(f&&!f.value){f.value=j.fields[k];n++;}}'
          'm.textContent=n+"項目を取り込みました";}catch(e){m.textContent="取り込みに失敗";}}'
          '</script>')
    also = ('<div class="pf-set" style="padding:12px 16px"><label style="display:flex;align-items:center;gap:9px;font-size:18px;font-weight:600;color:var(--sumi)">'
            '<input type="checkbox" name="also_maisoku" value="1" checked> 同じ情報でマイソク（販売図面）も一緒に作成する</label>'
            '<div class="conn-guide" style="margin-top:8px">1回の入力で重説とマイソクの両方を作ります（帯は業者情報から自動で入ります）。</div></div>')
    # 写真・PDFから自動入力（無料ローカルOCR）。同じ情報がマイソクにも流れる。
    from hub_core import local_ocr as _loc
    if _loc.available():
        photo = ('<fieldset class="pf-set"><legend>写真・PDFから取り込む（任意）</legend>'
                 '<form method="post" action="/juusetsu/from-photo" enctype="multipart/form-data" '
                 'style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
                 '<label class="pf-l" style="margin:0">販売図面・登記等の写真/PDF</label>'
                 '<input type="file" name="photo" accept="image/*,application/pdf" required style="font-size:18px">'
                 '<button class="ri-qbtn" type="submit">読み取ってフォームに入れる</button>'
                 f'<span class="gn">無料・端末内（{_esc(_loc.engine())}）・クラウド送信なし。値は宅建士が確認</span>'
                 '</form></fieldset>')
    else:
        photo = ''
    # 注意: photo は独立した <form> なのでメインフォームの外に置く（フォーム入れ子は不正＝内側が無視される）。
    form = ('<form method="post" action="/juusetsu/new/create" class="pf-wrap">' + picker + paste + blocks + also
            + '<div class="pf-actions"><button class="ri-go" type="submit">重説（とマイソク）の下書きを作成</button>'
            '<a class="ri-qbtn" href="/juusetsu" style="margin-left:8px">戻る</a></div></form>')
    hint = ('<div class="conn-guide" style="margin:0 0 14px">最低限、<b>物件名</b>と主な<b>契約条件</b>を入れれば下書きが出ます。'
            '会社情報は業者情報から自動、台帳の物件を選べば登記の所在・構造・面積も自動で入ります。'
            '空欄は下書きに ☐（要記入）として残ります。</div>')
    inner = (ui.page_head("重要事項説明書の新規作成",
             "物件・契約の情報を入力すると、宅建業法35条の様式に沿った下書きを作成します。"
             "作成後にプレビュー・編集し、宅地建物取引士が確認して記名確定してください。")
             + ocr_note + hint + photo + form + js)
    return _wrap_main("juusetsu", "/juusetsu", "重説の新規作成", inner)


def render_juusetsu(data_dir: Path, params, message: str = "", error: str = "") -> str:
    case_id = (params.get("case", [""])[0] or "").strip()
    ctx = load_juusetsu_context(data_dir, case_id)
    final_event = _latest_finalization(data_dir, ctx["target_id"])
    finalized = final_event is not None
    draft_path = ctx.get("draft_path")
    finalizable = bool(
        draft_path and draft_path.is_file()
        and draft_path.resolve().is_relative_to(Path(data_dir).resolve()))
    if finalized:
        pill = '<span class="pill done">確定済</span>'
    elif finalizable:
        pill = '<span class="pill draft">ドラフト — 未確定</span>'
    else:
        pill = '<span class="pill draft">記名できるドラフトなし</span>'
    # 保存名や連携モジュール名は顧客向け画面に出さず、出典の意味だけを伝える。
    source_label = "この端末の物件調査データ" if ctx["draft_path"] else "物件調査データ未作成"
    hazard_html = _render_hazard_block(params, ctx)

    check_items = []
    for row in ctx["checks"][:5]:
        reason = row.get("理由", "")
        tick = "tick q" if ("要確認" in reason or "未確認" in reason) else "tick"
        check_items.append(
            f'<div class="ri-item"><span class="{tick}"></span><div>'
            f'<div class="it">{_esc(row.get("項目", "確認項目"))}</div>'
            f'<div class="id">{_esc(reason or "確認済み候補")}</div>'
            f'<div class="src">出典: {_esc(row.get("source_hint") or "物件調査データ")}</div>'
            '</div></div>'
        )
    if not check_items:
        check_items.append('<div class="ri-item"><span class="tick q"></span><div>'
                           '<div class="it">確認項目</div>'
                           '<div class="id">物件調査の確認項目はまだありません</div>'
                           '<div class="src">出典: 物件調査データ未作成</div></div></div>')

    gate = ""
    if finalized:
        gate = (
            '<div class="ri-gate"><div><div class="gt">宅建士として記名済み</div>'
            '<div class="gd">確定した内容は、改ざん検知つきの台帳に記録しました。</div></div>'
            '<span class="ri-badge ok">確定済</span></div>'
        )
    elif finalizable:
        signer_name, signer_reg = _signer_form_defaults(data_dir, current_viewer())
        gate = (
            '<form class="ri-gate" method="post" action="/juusetsu/finalize">'
            '<div><div class="gt">宅建士として記名して確定する</div>'
            '<div class="gd">確定すると内容ハッシュで束縛し、監査ログに記録します。'
            'この操作だけが認証済み書込みです。</div></div>'
            f'<input type="hidden" name="case" value="{_esc(ctx["case"])}">'
            f'<input type="hidden" name="target_id" value="{_esc(ctx["target_id"])}">'
            f'<div class="sig"><input name="takkenshi_name" value="{_esc(signer_name)}" placeholder="宅建士名" required>'
            f'<input name="license_no" value="{_esc(signer_reg)}" placeholder="宅建士の登録番号" required>'
            '<button class="ri-btn" type="submit">記名して確定</button></div></form>'
        )
    else:
        gate = (
            '<div class="ri-gate"><div><div class="gt">記名できるドラフトがありません</div>'
            '<div class="gd">「重要事項説明書を新規作成」から実データの下書きを作成し、'
            '内容と出典を確認してから記名してください。見本や存在しない書類は確定できません。'
            '</div></div></div>'
        )

    audit_html = ""
    if final_event:
        audit_html = (
            '<div class="audit"><div class="auditline">'
            f'監査ログ: 記録 {_esc(final_event.get("seq", ""))} / '
            f'{_esc(_audit_action_label(final_event.get("action", "")))} / '
            f'{_esc(_display_datetime(final_event.get("timestamp", "")))} / '
            '改ざん検知済み</div></div>'
        )
    elif ctx["draft_path"]:
        audit_html = '<div class="audit"><div class="auditline">監査ログ: 未確定（ドラフト表示のみ）</div></div>'

    alert = ""
    if error:
        alert = f'<div class="ri-alert err">{_esc(error)}</div>'
    elif message or params.get("finalized"):
        alert = f'<div class="ri-alert ok">{_esc(message or "記名確定しました")}</div>'

    body = (
        '<div class="ri-ju">'
        f'<div class="ri-doc"><div class="dh"><h2>重要事項説明書（35条）</h2>{pill}</div>'
        f'{_render_draft_preview(ctx["draft_text"])}</div>'
        '<div class="ri-insp"><h3>AIが下書きした項目</h3>'
        '<div class="ih">出典と要確認点を確認してください。</div>'
        f'{"".join(check_items)}'
        f'{hazard_html}</div>'
        f'{gate}{alert}{audit_html}'
        f'<div class="credit">出典クレジット: {_esc(source_label)} / '
        'あいのては下書きを補助します。重要事項説明の最終責任は記名した宅地建物取引士に帰属します。</div>'
        '</div>'
    )
    lib = _doc_library_html(
        data_dir, "juusetsu", open_label="全文を開く",
        empty_msg="保存済みの重要事項説明書はまだありません。「ことばで頼む」から作成・保存できます。")
    usage = _usage_strip(
        "重要事項説明書（35条）の全文表示と、AIが下書きしたドラフトの確認・記名確定を行う画面です。",
        "物件調査の結果からドラフトと要確認一覧を作ります。災害リスク欄は不動産リスクスコア連携が有効なときだけ"
        "数値が入ります（無ければ空欄・捏造しません）。",
        ["「重要事項説明書を新規作成」でドラフトを作る",
         "各項目の出典と要確認点を確認する",
         "宅地建物取引士として記名して確定する"],
        [("/juusetsu/new", "重説を新規作成"), ("/maisoku", "マイソク"), ("/reins", "REINS")])
    top = (ui.page_head("重説", "保存済みの重要事項説明書を全文表示／AIが生成中のドラフトを確認し記名確定。")
           + usage
           + '<div class="ri-quick" style="margin:2px 0 14px"><span class="ri-quick-l">新しく作る</span>'
             '<a class="ri-go" href="/juusetsu/new">重要事項説明書を新規作成</a></div>'
           + ui.section("保存済みの重要事項説明書") + lib
           + ui.section("現在のドラフト（AI生成・未確定）"))
    return _ri_shell("/juusetsu", "重説", '<div class="ri-ws">' + _ri_nav("juusetsu", ctx["case"]) +
                     '<main class="ri-main">' + top + body + '</main></div>')


def finalize_juusetsu(data_dir: Path, form: dict, viewer: Viewer):
    allowed = {"宅建士", "責任者", "代表"}
    case_id = (form.get("case", [""])[0] or "").strip()
    target_id = (form.get("target_id", [""])[0] or "").strip()
    name = (form.get("takkenshi_name", [""])[0] or "").strip()
    license_no = (form.get("license_no", [""])[0] or "").strip()
    params = {"case": [case_id]} if case_id else {}
    if viewer.role not in allowed:
        return 403, render_juusetsu(data_dir, params, error="この操作は宅建士・責任者・代表のみ実行できます。"), None
    if not target_id or not name or not license_no:
        return 400, render_juusetsu(data_dir, params, error="宅建士名・登録番号・対象IDが必要です。"), None

    ctx = load_juusetsu_context(data_dir, case_id)
    target_id = target_id or ctx["target_id"]
    draft_path = ctx.get("draft_path")
    if not (draft_path and draft_path.is_file()
            and draft_path.resolve().is_relative_to(Path(data_dir).resolve())):
        return 409, render_juusetsu(
            data_dir, params,
            error="記名できる実データのドラフトがありません。見本や存在しない書類は確定できません。"), None
    if target_id != ctx["target_id"]:
        return 409, render_juusetsu(
            data_dir, params, error="表示中のドラフトと記名対象が一致しないため確定しませんでした。"), None
    # 外部調査Markdownも書類ストアへ取り込み、主導線と同じ中核処理で
    # 「記名済みの新しい版」そのもののhashを監査へ束縛する。
    from hub_core import chat_bridge as _cb, documents as _docs
    try:
        try:
            stored = _docs.get_version(data_dir, target_id)
        except _docs.DocError as exc:
            if exc.code != 404:
                raise
            stored = None
        stored_case = str(((stored or {}).get("meta") or {}).get("case_id") or "")
        if (stored is None or stored["body"] != ctx["draft_text"]
                or (case_id and stored_case != case_id)):
            saved = _docs.save_version(
                data_dir, target_id, ctx["draft_text"], kind="juusetsu", fmt="md",
                author="外部調査ドラフト取込", case_id=case_id)
            source_version = saved["version"]
        else:
            source_version = stored["meta"]["version"]
        _cb.finalize(
            data_dir, "", name, license_no, None, viewer, confirm=True,
            doc_id=target_id, version=source_version)
    except _docs.DocError as exc:
        return exc.code, render_juusetsu(
            data_dir, params,
            error=_public_exception_message(exc, "重要事項説明書を確定できませんでした。入力内容と版を確認してください。"),
        ), None
    except _cb.BridgeError as exc:
        status = 403 if exc.code == 400 else exc.code
        return status, render_juusetsu(
            data_dir, params,
            error=_public_exception_message(exc, "重要事項説明書を確定できませんでした。入力内容と権限を確認してください。"),
        ), None
    loc = "/juusetsu?case=" + quote(ctx["case"]) + "&finalized=1"
    return 303, "", loc



def _doc_is_finalized(data_dir: Path, doc_id: str) -> bool:
    """その書類が記名確定済みかを監査台帳で調べる。"""
    log = Path(data_dir) / "audit_log.jsonl"
    if not log.is_file() or not doc_id:
        return False
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return False
    if "finalized_with_signature" not in text:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("action") != "finalized_with_signature":
            continue
        tgt = str(ev.get("target") or "")
        if tgt == doc_id or tgt.startswith(doc_id + "#"):
            return True
    return False


def _stamp_signature_into_body(data_dir: Path, doc_id: str, name: str, reg: str) -> None:
    """記名確定した宅建士の氏名・登録番号を、書面の記名欄へ実際に書き込む。

    空欄（＿＿＿＿）のままだと、交付される重要事項説明書が無記名になる。
    元の版は消さず、記名済みの新しい版として積む（append-only を壊さない）。
    """
    from hub_core import documents as _docs
    try:
        cur = _docs.get_version(data_dir, doc_id)
    except Exception:      # noqa: BLE001  本文が読めなくても記名の監査記録は残っている
        return
    body = cur.get("body") or ""
    if not body:
        return
    replaced = _docs.signature_body(body, name, reg)
    if replaced == body:
        return
    try:
        _docs.save_version(data_dir, doc_id, replaced,
                           kind=(cur.get("meta") or {}).get("kind") or "juusetsu",
                           author=f"記名確定({name})")
    except Exception:      # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# 操作OS (S1): 状態遷移を hub.db に書く + HMAC監査。RBAC権限ゲートで操作×役割を制御。
# ---------------------------------------------------------------------------
def _op_error(msg: str, back: str) -> str:
    return ("<!doctype html><meta charset=utf-8>"
            "<body style='font-family:system-ui,sans-serif;padding:48px;color:#111418'>"
            f"<p>エラー: {html.escape(msg)}</p>"
            f"<p><a href='{html.escape(back)}'>← 戻る</a></p></body>")


def handle_op(data_dir: Path, op: str, form: dict, viewer: Viewer):
    """专门UI の操作POST を統一コア(hub_core.operations)に委譲し (status, body, location) へ写像。
    serve と mcp_server が同一コア・同一 hub.db を呼ぶ(ロジック二重化なし)。"""
    from hub_core.operations import OpError, apply_operation
    params = {k: (v[0] if isinstance(v, list) else v) for k, v in (form or {}).items()}
    cid = (params.get("case_id") or "").strip()
    prop = (params.get("property") or "").strip()
    back = (("/case?id=" + quote(cid)) if cid else
            (("/materials?property=" + quote(prop)) if op == "asset_attest" and prop else "/"))
    try:
        res = apply_operation(data_dir, op, params, viewer.user, viewer.role)
    except OpError as exc:
        return exc.code, _op_error(
            _public_exception_message(exc, "この操作を完了できませんでした。入力内容と権限を確認してください。"),
            back,
        ), None
    link = res.get("link") or back
    sep = "&" if "?" in link else "?"
    return 303, "", link + sep + "op=" + quote(op)


def advance_case(data_dir: Path, form: dict, viewer: Viewer):
    """後方互換(/case/advance・既存テスト): 統一コアの case_advance に委譲。"""
    return handle_op(data_dir, "case_advance", form, viewer)


# ---------------------------------------------------------------------------
# テーブル描画 (ソートリンク・facet・ハイライト・案件リンク)
# ---------------------------------------------------------------------------
# 状態enum→日本語ラベル（開発語彙を製品面に漏らさない=GATE-PV W1-PV deficit#1）。
# 参照ID（SUUMO-B-001等）は実IDなので翻訳しない。未知値はそのまま（隠さない・正直）。


from hub_core.labels import jp as _jp  # 状態enum→日本語（単一正本=hub_core/labels.py）


def render_facets(all_rows, source, params, route, q):
    cols = FACET_COLS.get(source, [])
    if not cols:
        return ""
    active = set()
    for fp in params.get("f", []):
        if ":" in fp:
            active.add(fp)
    chips = ['<div class="facets">']
    base_kept = []
    if q:
        base_kept.append(("q", q))
    for col in cols:
        pairs = facet_counts(all_rows, col)
        if not pairs:
            continue
        chips.append(f'<span class="flabel">{_esc(col)}:</span>')
        _rest = max(0, len(pairs) - 5)
        shown = 0
        for val, cnt in pairs[:5]:
            shown += 1
            token = f"{col}:{val}"
            on = token in active
            # クリックでトグル
            kept = [("f", x) for x in active if x != token]
            if not on:
                kept.append(("f", token))
            kept += base_kept
            qs = "&".join(f"{k}={quote(str(v))}" for k, v in kept)
            href = route + (("?" + qs) if qs else "")
            chips.append(
                f'<a class="facet{" on" if on else ""}" href="{href}">'
                f'{_esc(_jp(val))}<span class="fc">{cnt}</span></a>'
            )
    if active:
        kept = [("q", q)] if q else []
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in kept)
        chips.append(f'<a class="clearf" href="{route + (("?" + qs) if qs else "")}">'
                     '絞り込み解除 ✕</a>')
        if _rest:
            chips.append(f'<span class="fc" style="font-size:18px;color:var(--muted)">他{_rest}種は詳細表で</span>')
    chips.append('</div>')
    return "".join(chips)


def render_table(page, headers, rows, params, route, q, active_filters,
                 sort_col, sort_dir):
    if not headers and rows:
        keys, seen = [], set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        headers = keys
    if not rows:
        return '<div class="empty">該当データなし（条件に合う行がありません）</div>'

    ordered = order_headers(page, headers)

    # ヘッダ (ソートリンク) — 現在の q/f を保持
    keep = []
    if q:
        keep.append(("q", q))
    for col, val in active_filters:
        keep.append(("f", f"{col}:{val}"))

    th = []
    for h in ordered:
        nxt = "desc" if (h == sort_col and sort_dir == "asc") else "asc"
        arr = ""
        if h == sort_col:
            arr = ' <span class="arr">▲</span>' if sort_dir == "asc" else ' <span class="arr">▼</span>'
        params_pairs = keep + [("sort", h), ("dir", nxt)]
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params_pairs)
        th.append(f'<th><a href="{route}?{qs}">{_esc(h)}{arr}</a></th>')

    body_rows = []
    for r in rows:
        is_p0 = r.get(COL_PRIORITY, "") == "P0"
        cells = "".join(f"<td>{render_cell(h, r.get(h, ''), q)}</td>" for h in ordered)
        body_rows.append(f'<tr class="{"p0row" if is_p0 else ""}">{cells}</tr>')

    return (
        '<div class="tablewrap"><table><thead><tr>'
        + "".join(th)
        + '</tr></thead><tbody>'
        + "".join(body_rows)
        + '</tbody></table></div>'
    )


# ---------------------------------------------------------------------------
# ダッシュボード (home)
# ---------------------------------------------------------------------------
def _safe_dt(s):
    return s or ""


def render_home(data_dir: Path, params) -> str:
    _, tasks = read_csv(data_dir / TASKS_FILE)
    _, holds = read_csv(data_dir / "hold_queue.csv")
    _, approvals = read_csv(data_dir / "approval_queue.csv")
    _, leads = read_csv(data_dir / "portal_leads.csv")
    _, audit = read_jsonl(data_dir / "audit_log.jsonl")

    q = _public_display_param(params.get("q", [""])[0])

    # KPI 集計 (実データから)
    today_n = sum(1 for r in tasks if _q(r, "Today"))
    p0_hold_n = sum(1 for r in tasks if _q(r, "Hold") and r.get(COL_PRIORITY) == "P0")
    hold_n = len(holds)
    approval_n = sum(1 for r in approvals if r.get("判断", "pending") == "pending")
    new_leads_n = len(leads)
    p0_total = sum(1 for r in tasks if r.get(COL_PRIORITY) == "P0")
    gate_priv_prof = sum(1 for r in tasks if r.get(COL_GATE) in ("privacy", "professional"))

    # 色の規律: 朱(red)=至急/期限超過のみ・それ以外は濃紺(既定)。緑は確定/入金のみ＝件数KPIには使わない。
    kpis = [
        ("", today_n, "今日のタスク", "/today"),
        ("red" if p0_hold_n else "", p0_hold_n, "至急の保留", "/hold"),
        ("", approval_n, "承認待ち", "/approval"),
        ("", new_leads_n, "新着反響", "/leads"),
        ("red" if hold_n else "", hold_n, "保留 合計", "/hold"),
        ("", gate_priv_prof, "個人情報・専門確認", "/documents"),
    ]
    kpi_html = "".join(
        f'<a class="kpi {c}" href="{href}"><div class="n">{n}</div>'
        f'<div class="l">{_esc(label)}</div></a>'
        for c, n, label, href in kpis
    )

    # 要対応アラート: P0 Hold + privacy/professional gate を上位に
    alerts = []
    for r in tasks:
        if r.get(COL_PRIORITY) == "P0" and _q(r, "Hold"):
            alerts.append(("P0 HOLD", "b-red", r))
    for r in tasks:
        if r.get(COL_GATE) in ("privacy", "professional") and r not in [a[2] for a in alerts]:
            alerts.append(("GATE", "b-org", r))
    alerts = alerts[:12]
    alert_items = []
    if alerts:
        for tag, cls, r in alerts:
            title = r.get(COL_TITLE, "")
            ref = r.get(COL_PROP_REF, "") or r.get(COL_LEAD_ID, "")
            reflink = (f' · <a class="caselink" href="/case?id={quote(ref)}">{_esc(ref)}</a>'
                       if ref else "")
            gate = r.get(COL_GATE, "")
            gchip = (f' <span class="chip {_GATE_CLASS.get(gate, "g-gray")}">{_esc(gate)}</span>'
                     if gate else "")
            alert_items.append(
                f'<li><span class="badge {cls}">{tag}</span>'
                f'<span>{_esc(title)}{gchip}'
                f'<span class="a-meta">{reflink}</span></span></li>'
            )
    else:
        alert_items.append('<li><span class="badge b-green">OK</span>'
                           '<span>要対応アラートはありません</span></li>')

    # 今日やること (Top タスク): P0 優先で Today
    today_rows = [r for r in tasks if _q(r, "Today")]
    today_rows.sort(key=lambda r: (r.get(COL_PRIORITY, "P9")))
    todo_items = []
    if today_rows:
        for r in today_rows[:10]:
            prio = r.get(COL_PRIORITY, "")
            pcls = "p0" if prio == "P0" else ("p1" if prio == "P1" else "p2")
            ref = r.get(COL_PROP_REF, "")
            reflink = (f' · <a class="caselink" href="/case?id={quote(ref)}">{_esc(ref)}</a>'
                       if ref else "")
            todo_items.append(
                f'<li><span class="prio {pcls}">{_esc(prio)}</span>'
                f'<span>{_esc(r.get(COL_TITLE, ""))}'
                f'<span class="a-meta">{reflink}</span></span></li>'
            )
    else:
        todo_items.append('<li><span class="muted">Today のタスクはありません</span></li>')

    # 最近の監査 (直近N件・末尾が新しい想定で逆順)
    audit_items = []
    recent = list(reversed(audit))[:8]
    if recent:
        for ev in recent:
            act = ev.get("action", "")
            ts = ev.get("timestamp", "")
            gs = ev.get("gate_status", "") or ev.get("reply_gate", "")
            gchip = (f' <span class="badge {_status_class(gs)}">{_esc(gs)}</span>'
                     if gs else "")
            audit_items.append(
                f'<li><span class="badge b-blue">{_esc(act)}</span>{gchip}'
                f'<span class="a-meta">{_esc(ts)}</span></li>'
            )
    else:
        audit_items.append('<li><span class="muted">監査ログがありません</span></li>')

    body = (
        '<h2 class="page">ダッシュボード</h2>'
        '<p class="pdesc">ri-hub の出力を統合した不動産業務コントロールタワー。'
        '数字は out/ の実データから集計しています（読み取り専用）。</p>'
        f'<div class="kpis">{kpi_html}</div>'
        '<div class="panel"><h3>要対応（至急の保留・個人情報や専門の確認を優先）</h3>'
        f'<ul class="alist">{"".join(alert_items)}</ul></div>'
        '<div class="grid2">'
        '<div class="panel"><h3>今日やること（Top タスク）</h3>'
        f'<ul class="alist">{"".join(todo_items)}</ul></div>'
        '<div class="panel"><h3>🔐 最近の監査（直近8件）</h3>'
        f'<ul class="alist">{"".join(audit_items)}</ul></div>'
        '</div>'
    )
    return render_page(data_dir, "/", "ダッシュボード", body, q)


# ---------------------------------------------------------------------------
# 通常画面 (一覧)
# ---------------------------------------------------------------------------
def _screen_guide(rows) -> str:
    n = len(rows)
    if not n:
        return '<div class="lguide">該当する項目はありません。</div>'
    p0 = sum(1 for r in rows if (r.get(COL_PRIORITY, "") == "P0"))
    tail = (f'うち<b>要対応(P0)が{p0}件</b>。上から順に対応しましょう。' if p0
            else '上から順に確認しましょう。')
    return f'<div class="lguide">{n}件あります。{tail}<span class="lguide-sub">専門用語の全項目は下の「詳細表」で見られます。</span></div>'


def _render_card_list(rows) -> str:
    """ワイドテーブルの代わりに、優先度・タイトル・状態・要点だけの読みやすいカード列。"""
    if not rows:
        return ""
    cards = []
    for r in rows[:40]:
        prio = (r.get(COL_PRIORITY, "") or "").strip()
        pcls = "p0" if prio == "P0" else ("p1" if prio == "P1" else "p2")
        title = _esc(r.get(COL_TITLE) or r.get("確認対象") or r.get("物件名")
                     or r.get("顧客名") or r.get("action") or r.get("タスクID") or "項目")
        status = (r.get("状態") or r.get("キュー") or r.get("ゲート") or "").strip()
        sbadge = (f'<span class="badge {_status_class(status)}">{_esc(_jp(status))}</span>'
                  if status else "")
        ref = (r.get(COL_PROP_REF) or "").strip()
        reftag = f'<span class="lref">{_esc(ref)}</span>' if ref else ""
        meta_parts = []
        for k in ("担当", "顧客名", "保留理由", "理由", "期限", "金額"):
            v = r.get(k)
            if v and str(v).strip():
                meta_parts.append(_esc(_jp(str(v).strip())))
        meta = " ／ ".join(meta_parts[:3])
        pchip = f'<span class="lc-prio {pcls}">{_esc(prio)}</span>' if prio else ""
        inner = (f'<div class="lc-head">{pchip}'
                 f'<span class="lc-title">{title}</span>{sbadge}</div>'
                 f'<div class="lc-meta">{reftag}{(" · " + meta) if meta else ""}</div>')
        # 見えるだけで終わらせない。その行から始められる仕事を1つ出す。
        cust = str(r.get("顧客名") or "").strip()
        act = ""
        if cust:
            ask = f"{cust}様への一次返信の下書きを作って"
            act = (f'<a class="lc-act" href="/console?prefill={quote(ask)}">お返事の下書きを作る</a>')
        elif ref:
            act = f'<a class="lc-act" href="/case?id={quote(ref)}">この案件の全部を見る</a>'
        if ref:
            cards.append(f'<div class="lcard"><a class="lc-body" href="/case?id={quote(ref)}">'
                         f'{inner}</a>{act}</div>')
        else:
            cards.append(f'<div class="lcard"><div class="lc-body">{inner}</div>{act}</div>')
    more = (f'<div class="lc-more">ほか {len(rows) - 40} 件（詳細表で全件表示）</div>'
            if len(rows) > 40 else "")
    return '<div class="lcards">' + "".join(cards) + "</div>" + more


def render_screen(data_dir: Path, page, params) -> str:
    headers, all_rows = load_page_data(data_dir, page)
    rows, q, active_filters, sort_col, sort_dir = apply_query(
        headers, all_rows, params, page["source"])

    facets = render_facets(all_rows, page["source"], params, page["route"], q)
    cnt_note = (f'<span class="pcount">({len(rows)} / 全{len(all_rows)} 件)</span>'
                if (q or active_filters) else
                f'<span class="pcount">({len(all_rows)} 件)</span>')
    table = render_table(page, headers, rows, params, page["route"], q,
                         active_filters, sort_col, sort_dir)

    worklist_html = _worklist_section(data_dir) if page["route"] == "/today" else ""
    if page["route"] == "/today":
        worklist_html += _bulk_today_section(data_dir)
    if page["route"] == "/leads":
        worklist_html = _inbox_section(data_dir)
    body = (
        f'<h2 class="page">{_esc(page["label"])} {cnt_note}</h2>'
        f'<p class="pdesc">{_esc(page["desc"])}</p>'
        f'{worklist_html}'
        f'{_screen_guide(rows)}'
        f'{facets}'
        f'{_render_card_list(rows)}'
        f'<details class="tabledetails"><summary>詳細表（全項目）を開く</summary>{table}</details>'
    )
    return render_page(data_dir, page["route"], page["label"], body, q)


def _inbox_section(data_dir: Path) -> str:
    """S-leads冒頭: 反響取込（M-inbox）。①反響/フォルダ=.eml取込 ②手動その場で登録。
    権限のある viewer にだけ操作を出す（無権限=説明のみ・fail-closed）。"""
    from hub_core.operations import OP_ROLES
    viewer = current_viewer()
    can = bool(viewer and viewer.role in OP_ROLES.get("lead_quick_add", set()))
    ingest_btn = _op_button("inbox_ingest", {"_": "1"}, "反響フォルダを取り込む", viewer)
    quick = ""
    if can:
        quick = (
            '<form method="post" action="/op" class="ri-actform" style="margin-top:0">'
            '<input type="hidden" name="op" value="lead_quick_add">'
            '<span class="lbl">その場で登録</span>'
            '<input name="customer_name" placeholder="お客様名（必須）" required>'
            '<input name="contact" placeholder="連絡先">'
            '<select name="channel"><option>電話</option><option>紹介</option>'
            '<option>店頭</option><option>メール</option></select>'
            '<input name="property_ref" placeholder="物件参照（任意）" style="width:130px">'
            '<button class="ri-go" type="submit">登録</button></form>')
    return ('<div class="ri-guide" style="margin-bottom:14px">'
            '<div class="gh">お客様からの問い合わせを、ここに集めます</div>'
            '<div class="gb">電話やご来店は、下の「その場で登録」から。'
            'メールで来た問い合わせは、まとめて取り込めます。</div>'
            '<details style="margin-top:8px"><summary style="font-size:18px;cursor:pointer;'
            'min-height:var(--ai-hit);display:flex;align-items:center">'
            'メールの取り込み方（くわしく）</summary>'
            '<div class="gb" style="margin-top:8px">受信箱の問い合わせメールを <b>反響/</b> フォルダに'
            ' .eml で保存し、下の「まとめて取り込む」を押してください。台帳に載り、'
            '「お返事する」というやることが自動で生まれます。'
            'SUUMO・LIFULL HOME\'S の通知メールの形にも対応しています。</div></details>'
            f'<div style="margin-top:10px;display:flex;gap:14px;flex-wrap:wrap;align-items:center">{ingest_btn}{quick}</div></div>')


def _bulk_today_section(data_dir: Path) -> str:
    """一括操作（監査BULK-01のUI）: Today の未完了タスクを複数選択→一括完了。
    batch基盤(/api/op {batch:[...]})へfetch。権限のある viewer のみ・法定/金銭/記名は除外済み。"""
    from hub_core.operations import OP_ROLES
    viewer = current_viewer()
    if not (viewer and viewer.role in OP_ROLES.get("task_done", set())):
        return ""
    _, tasks = _load_rows_for_ui(data_dir, "tasks")
    open_today = [r for r in tasks if _q(r, "Today")
                  and (r.get("status") or "").strip() not in ("done", "解決")][:30]
    if not open_today:
        return ""
    rows = []
    for r in open_today:
        tid = r.get("task_id") or r.get(COL_TITLE)
        if not tid:
            continue
        prio = (r.get(COL_PRIORITY) or "").strip()
        pchip = f'<span class="lc-prio {"p0" if prio=="P0" else ("p1" if prio=="P1" else "p2")}">{_esc(prio)}</span>' if prio else ""
        rows.append(
            f'<label class="bulk-row"><input type="checkbox" class="bulk-cb" value="{_esc(tid)}">'
            f'{pchip}<span class="bulk-t">{_esc(r.get(COL_TITLE, ""))}</span>'
            f'<span class="bulk-m">{_esc(_jp(r.get("担当", "")))}</span></label>')
    return (
        '<div class="ri-sech" style="margin-top:22px">一括操作（複数選択して一度に処理）</div>'
        '<div class="bulk-bar"><label class="bulk-all"><input type="checkbox" id="bulkAll"> 全選択</label>'
        '<span id="bulkCount" class="gn">0件選択</span>'
        '<button class="ri-go" id="bulkDone" type="button" disabled>選択したタスクを完了にする</button>'
        '<span id="bulkMsg" class="gn"></span></div>'
        '<div class="bulk-list">' + "".join(rows) + '</div>'
        '<div class="gn" style="margin-top:6px">一括対象は可逆・低リスク操作のみ。承認・請求・記名など法定/金銭の操作は一括できません（単発で確認）。</div>')


def _worklist_section(data_dir: Path) -> str:
    """S-today冒頭: 今週アプローチ（KAS-08型ワークリスト）。理由つき・スヌーズ操作（可逆）つき。"""
    from datetime import date, timedelta
    from hub_core.worklist import build_worklist
    from hub_core.operations import snoozed_task_ids
    try:
        rows = build_worklist(data_dir)
    except Exception:
        return ""
    viewer = current_viewer()
    next_week = (date.today() + timedelta(days=7)).isoformat()
    items = []
    for r in rows:
        snooze_ctl = ""
        if r.get("task_id"):
            snooze_ctl = _op_button(
                "task_snooze", {"task_id": r["task_id"], "until": next_week},
                "1週間後に", viewer)
        items.append(
            '<div class="wl-row">'
            f'<a class="wl-main" href="{r["link"]}">'
            f'<span class="wl-title">{_esc(r["title"])}</span>'
            f'<span class="wl-reason">{_esc(r["reason"])}</span></a>'
            f'<span class="wl-meta">{_esc(r.get("who") or "")}'
            + (f'<span class="lref">{_esc(r["ref"])}</span>' if r.get("ref") else "")
            + f'</span>{snooze_ctl}</div>')
    snoozed = snoozed_task_ids(data_dir)
    sn_note = ""
    if snoozed:
        sn_items = []
        for tid, until in sorted(snoozed.items(), key=lambda x: x[1])[:6]:
            ctl = _op_button("task_unsnooze", {"task_id": tid}, "今すぐ戻す", viewer)
            sn_items.append(f'<div class="wl-snoozed"><span class="lref">{_esc(tid)}</span> '
                            f'{_esc(until)} まで非表示 {ctl}</div>')
        sn_note = ('<details class="wl-det"><summary>スヌーズ中 '
                   f'{len(snoozed)}件</summary>{"".join(sn_items)}</details>')
    if not items and not sn_note:
        return ""
    return ('<div class="ri-sech">今週アプローチ（台帳から自動抽出・理由つき）</div>'
            '<div class="wl">' + "".join(items) + '</div>' + sn_note)


# ---------------------------------------------------------------------------
# 案件串刺し /case?id=...
# ---------------------------------------------------------------------------
def _row_refs_id(row, cid: str) -> bool:
    """row のいずれかの値が cid に一致 (案件串刺し)。"""
    for v in row.values():
        if isinstance(v, str) and v == cid:
            return True
    return False


ROOT_DIR = Path(__file__).resolve().parent
# 取得先(資料請求)として許可する公的ドメイン。これ以外への /go リダイレクトは拒否。
ACQUISITION_ALLOW_HOST = {"www1.touki.or.jp", "www.touki.or.jp", "touki.or.jp"}
ACQUISITION_ALLOW_SUFFIX = (".go.jp", ".lg.jp")


def _is_allowed_acquisition_url(u: str) -> bool:
    try:
        host = (urlsplit(u).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and (host in ACQUISITION_ALLOW_HOST or host.endswith(ACQUISITION_ALLOW_SUFFIX))


def _load_request_tasks(data_dir: Path):
    for p in (Path(data_dir) / "request_tasks.csv", ROOT_DIR / "sample_gyosei_out" / "request_tasks.csv"):
        if p.exists():
            _, rows = read_csv(p)
            return rows
    return []


def _case_request_tasks(data_dir: Path, cid: str):
    rows = _load_request_tasks(data_dir)
    if not cid:
        return rows
    return [r for r in rows
            if cid in (r.get("property_id", ""), r.get("property_name", "")) or _row_refs_id(r, cid)]


def resolve_acquisition_url(data_dir: Path, params):
    """/go?doc=&case= を request_tasks の source_url(allowlist検証済)へ解決。許可外は None。"""
    doc = (params.get("doc", [""])[0] or "").strip()
    cid = (params.get("case", [""])[0] or "").strip()
    if not doc:
        return None
    for r in _load_request_tasks(data_dir):
        if r.get("doc_id") == doc and (
                not cid or cid in (r.get("property_id", ""), r.get("property_name", "")) or _row_refs_id(r, cid)):
            u = (r.get("source_url") or "").strip()
            return u if (u and _is_allowed_acquisition_url(u)) else None
    return None


def _office_label(t: dict) -> str:
    cat = (t.get("category") or "").strip()
    return {"税務": "市区町村役場 税務課（資産税）", "道路": "市区町村役場 道路管理課"}.get(cat, "市区町村役場")


def _map_query(t: dict) -> str:
    name = (t.get("property_name") or "").strip()
    loc = name.split()[0] if name else ""
    return (loc + " " + _office_label(t)).strip()




# POST はこの明示一覧以外を 501 で拒否する。実装分岐との一致は test_serve.py が AST で検証する。
_POST_ROUTES = frozenset({
    "/api/backup", "/api/backup/recovery-key", "/api/conn-test", "/api/op",
    "/brand/restore", "/calls/code", "/calls/directory", "/case/advance",
    "/chat", "/chat/stream", "/connections/save", "/connections/save-fax",
    "/doc/finalize", "/doc/save", "/fax/new", "/fax/send", "/fax/webhook",
    "/file/upload", "/it/advance", "/it/check", "/it/consent", "/it/create",
    "/it/deliver", "/it/gate/save", "/it/keiyaku37", "/it/propose", "/it/schedule",
    "/juusetsu/finalize", "/juusetsu/from-photo", "/juusetsu/new/create",
    "/juusetsu/new/parse", "/line/harness-webhook", "/line/hearing", "/line/inquiry",
    "/line/inquiry-resolve", "/line/it-start", "/line/new", "/line/property-card",
    "/line/pull", "/line/send", "/line/viewing", "/line/webhook", "/llm/save",
    "/login", "/logout", "/maisoku/edit", "/maisoku/from-photo", "/maisoku/new",
    "/maisoku/new-create", "/migrate/apply", "/migrate/preview", "/op",
    "/portal/apply", "/portal/request", "/profile/save", "/property/collect",
    "/reins/prepare", "/reins/record", "/setup", "/setup/step", "/telephony/webhook",
})

# セッション無しで到達できるPOSTは、この3種類だけ。追加時は認証方式と負例を同時に更新する。
_BOOTSTRAP_POST_ROUTES = frozenset({"/login", "/setup", "/setup/step"})
_TOKEN_AUTH_POST_ROUTES = frozenset({"/portal/apply", "/portal/request"})
_SIGNED_WEBHOOK_POST_ROUTES = frozenset({
    "/fax/webhook", "/line/harness-webhook", "/line/webhook", "/telephony/webhook",
})
_PUBLIC_POST_ROUTES = (
    _BOOTSTRAP_POST_ROUTES | _TOKEN_AUTH_POST_ROUTES | _SIGNED_WEBHOOK_POST_ROUTES
)


def _post_route_allowed(route: str) -> bool:
    return route in _POST_ROUTES


# 操作フォーム(POST)を持つ画面。CSP form-action 'self' を許可する(他は 'none')。
_FORM_ROUTES = {"/juusetsu", "/case", "/approval", "/hold", "/today", "/inbox", "/agent",
                "/console", "/maisoku", "/maisoku/edit", "/customers", "/timeline", "/leads", "/llm", "/search", "/reconcile", "/portal", "/profile", "/connections", "/juusetsu/new", "/maisoku/new-form", "/property/collect", "/fax", "/calls", "/line", "/reins", "/it"}
_CONNECT_ROUTES = {"/console", "/today"}  # fetch を許可する画面(connect-src 'self')
_FRAME_ROUTES = {"/maisoku", "/maisoku/edit"}  # /doc/preview を iframe 埋込(frame-src 'self')
_IMG_ROUTES = {"/timeline"}  # 受領写真(/file/raw)を表示する画面(img-src 'self')


def _case_stage_section(data_dir: Path, cid: str) -> str:
    """案件ステージ表示＋次段階へ進める操作ボタン(権限ある viewer のみ・統一 /op へPOST)。"""
    from hub_core.operations import CASE_STAGES, OP_ROLES
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    try:
        crows = SqliteStore(db).query("cases", "case_id = ?", (cid,)) if db.exists() else []
    except Exception:
        crows = []
    if not crows:
        return ""
    cur = (crows[0].get("status") or "").strip()
    pills = " → ".join((f"<b>{_esc(s)}</b>" if s == cur else f'<span style="color:var(--muted2)">{_esc(s)}</span>')
                       for s in CASE_STAGES)
    v = current_viewer()
    can = bool(v) and v.role in OP_ROLES["case_advance"]
    if cur in CASE_STAGES and CASE_STAGES.index(cur) < len(CASE_STAGES) - 1:
        nxt = CASE_STAGES[CASE_STAGES.index(cur) + 1]
        if can:
            ctrl = ('<form method="post" action="/op" style="margin-top:10px">'
                    '<input type="hidden" name="op" value="case_advance">'
                    f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
                    f'<input type="hidden" name="to_status" value="{_esc(nxt)}">'
                    f'<button class="ri-go" type="submit">「{_esc(nxt)}」へ進める →</button></form>')
        else:
            ctrl = '<div class="gn" style="margin-top:8px">前進操作の権限がありません（担当/宅建士/責任者/代表）。</div>'
    elif cur in CASE_STAGES:
        ctrl = '<div class="gn" style="margin-top:8px">最終段階（管理）です。</div>'
    else:
        ctrl = '<div class="gn" style="margin-top:8px">ステージ未設定。</div>'
    return ('<div class="ri-guide" style="margin-bottom:18px"><div class="gh">案件ステージ</div>'
            f'<div class="gb" style="font-size:19px">{pills}</div>{ctrl}'
            f'{_case_action_forms(cid, v)}</div>')


def _case_action_forms(cid: str, viewer) -> str:
    """案件に対する内見予約・請求作成フォーム(権限あるロールにのみ表示・統一 /op へPOST)。
    会話(Console)からも同じ操作ができるが、UIからも一気通貫で操作できるようにする。"""
    from hub_core.operations import OP_ROLES
    role = getattr(viewer, "role", None)
    out = []
    if role in OP_ROLES.get("viewing_schedule", set()):
        out.append(
            '<form method="post" action="/op" class="ri-actform">'
            '<input type="hidden" name="op" value="viewing_schedule">'
            f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
            '<span class="lbl">内見日時</span>'
            '<input type="datetime-local" name="event_at" required>'
            '<button class="ri-go ghost" type="submit">内見を予約</button></form>')
    if role in OP_ROLES.get("billing_create", set()):
        out.append(
            '<form method="post" action="/op" class="ri-actform">'
            '<input type="hidden" name="op" value="billing_create">'
            f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
            '<span class="lbl">請求</span>'
            '<select name="kind"><option>請求</option><option>入金</option><option>返金</option></select>'
            '<input type="text" name="amount" inputmode="numeric" placeholder="金額（円）" required>'
            '<button class="ri-go" type="submit">請求を作成</button>'
            '<span class="lbl" style="color:var(--muted2)">※金銭・監査に記録</span></form>')
    return "".join(out)



def _extract_chip(f: dict, rec: dict) -> str:
    """出典チップ（AIS-06型の最小実装=hoverで出典詳細）。"""
    src = rec.get("source", "")
    bbox = f.get("bbox")
    tip = f"出典: {src} p.{f.get('page')}" + (f" / 領域 {bbox}" if bbox else "")
    return (f'<span class="ex-chip" title="{_esc(tip)}">'
            f'{_esc(Path(src).name)} p.{f.get("page")}</span>')


def _case_screening_section(data_dir: Path, cid: str) -> str:
    """S2: 申込〜審査の状態表示（契約クローズ後半・保証会社BYO）。実与信は人間ゲート。"""
    from hub_core.operations import applications_for_case
    try:
        apps = applications_for_case(data_dir, cid)
    except Exception:
        return ""
    if not apps:
        return ""
    _JP = {"received": "申込受付", "docs_ok": "書類確認済", "screening": "保証審査中",
           "approved": "承認（契約可）", "declined": "否認", "withdrawn": "取下げ"}
    rows = []
    for a in apps:
        st = a.get("status", "")
        badge = ('<span class="ri-badge ok">承認</span>' if st == "approved"
                 else ('<span class="ri-badge bad">否認</span>' if st == "declined"
                       else '<span class="ri-badge warn">' + _esc(_JP.get(st, st)) + '</span>'))
        g = a.get("guarantor") or "未依頼"
        if g == "mock":
            g = "保証会社未接続"
        gnote = "（実際の審査は行いません）" if a.get("guarantor") == "mock" else ""
        rows.append(
            f'<tr><td class="caselink">{_esc(a.get("application_id",""))}</td>'
            f'<td>{_esc(a.get("applicant",""))}</td><td>{badge}</td>'
            f'<td>{_esc(g)}{_esc(gnote)}</td></tr>')
    table = ('<div class="tablewrap"><table><thead><tr><th>申込</th><th>申込者</th>'
             '<th>状態</th><th>保証会社</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')
    return ('<div class="ri-sech" style="margin-top:18px">申込・審査（保証会社）</div>' + table
            + '<div class="gn" style="margin-top:6px">保証会社が未接続の間は、実際の審査・与信は行いません。'
            'サービス接続と承認確定には担当者の確認が必要です。契約に進めるのは保証会社が承認したときだけです。</div>')


def _case_extract_section(data_dir: Path, cid: str, prop_name: str) -> str:
    """S2: 原典から抽出した値（M-extract）。承認済=通常表示・draft=要確認・改竄=朱で失効。"""
    from hub_core import extract as _ex
    try:
        recs = _ex.load_extractions(data_dir)
    except Exception:
        return ""
    if prop_name:
        hits = [r for r in recs if r.get("property") and r["property"] in prop_name] or                [r for r in recs if prop_name in (r.get("property") or "")]
    else:
        hits = []
    if not hits:
        return ""
    body = []
    for rec in hits[:6]:
        st = rec.get("status")
        badge = {"approved": '<span class="ri-badge ok">承認済</span>',
                 "draft": '<span class="ri-badge warn">下書き（未承認）</span>',
                 "tampered": '<span class="ri-badge bad">失効（原本が変更された）</span>'}.get(
            st, '<span class="ri-badge warn">不明</span>')
        rows = "".join(
            f'<tr><td>{_esc(f["key"])}</td><td>{_esc(f["value"])}</td>'
            f'<td>{_extract_chip(f, rec)}</td></tr>'
            for f in rec.get("fields", [])[:12])
        meta = (f'{_esc(Path(rec.get("source", "")).name)} {badge} · 抽出者 {_esc(rec.get("extractor") or "")}'
                + (f' · 承認 {_esc(rec.get("approved_by"))}' if rec.get("approved_by") else ""))
        body.append(f'<div class="gn" style="margin:8px 0 6px">{meta}</div>'
                    '<div class="tablewrap"><table><thead><tr><th>項目</th><th>値</th>'
                    f'<th>出典</th></tr></thead><tbody>{rows}</tbody></table></div>')
    return ('<div class="ri-sech" style="margin-top:18px">原典から抽出した値（出典つき・承認制）</div>'
            + "".join(body)
            + '<div class="gn" style="margin-top:6px">値は必ず出典（ページ・領域）に束縛。原本が差し替わると自動で失効します。'
            '承認前の値は書類に流れません。</div>')


def _hazard_links_html(lat, lon, addr: str) -> str:
    """物件座標→外部公式ハザードマップの deep-link 導線（DZ-1）。数値は出さず公式図へ飛ばす。
    全リンクは .go.jp/.lg.jp 許可ドメインで、_is_allowed_acquisition_url で二重検証してから描画。"""
    from hub_core import hazard_links as _hz
    links = _hz.hazard_links(lat, lon)
    muni = _hz.municipality_hint(addr)
    items = []
    for L in links:
        if _is_allowed_acquisition_url(L["url"]):
            items.append(f'<a class="ri-go ghost" href="{_esc(L["url"])}" target="_blank" '
                         f'rel="noopener">{_esc(L["title"])} →</a>')
    if addr and muni.get("url") and _is_allowed_acquisition_url(muni["url"]):
        items.append(f'<a class="ri-go ghost" href="{_esc(muni["url"])}" target="_blank" '
                     f'rel="noopener">{_esc(muni["title"])} →</a>')
    if not items:
        return ""
    return ('<div class="ri-sech" style="margin-top:14px;font-size:18px">公式ハザードマップで確認（座標つき）</div>'
            '<div style="display:flex;gap:8px;flex-wrap:wrap">' + "".join(items) + '</div>'
            '<div class="gn" style="margin-top:6px">自治体公表の最新ハザードマップが法定確認の対象（施行規則16条の4の3）。'
            'これらは公式サイトへ物件座標で飛ぶ導線です（あいのては数値を生成しません）。</div>')


def _case_prs_block(data_dir: Path, cid: str, params, prop_name: str) -> str:
    """S2: 物件詳細のPRS災害リスク第一級ブロック（Wave0）。
    - 取得は明示アクション（?prs=1&addr=）のみ＝ページ描画で勝手に外部を呼ばない。
    - 表示は較正済の洪水スコアのみ（金メッキ禁止）。高潮・液状化等の未較正種別は
      数値を出さず「未較正（査定に含めません）」を固定表示。未接続は理由つきで正直に。"""
    from hub_core import prs as _prs
    addr = (params.get("addr", [""])[0] or "").strip()
    want = (params.get("prs", [""])[0] or "").strip() == "1"
    inner = ""
    if want and addr:
        res = _prs.assess(address=addr)
        hz_links = ""
        _lat, _lon = res.get("lat"), res.get("lon")
        if isinstance(_lat, (int, float)) and isinstance(_lon, (int, float)):
            hz_links = _hazard_links_html(_lat, _lon, addr)
        if res.get("prs_status") == "OK":
            sc = _prs._flood_score(res)
            if sc is not None:
                band = _prs._band(sc)
                inner = (
                    '<div class="prs">'
                    f'<div class="peril"><div class="name">洪水</div><div class="score">{sc}<small> /100（{_esc(band)}）</small></div>'
                    f'<div class="bar"><i style="width:{max(2, min(100, sc))}%"></i></div>'
                    '<div class="cal">較正済（浸水想定・実績照合）</div></div>'
                    '<div class="peril na"><div class="name">地震・土砂ほか</div><div class="score">この画面では未表示</div>'
                    '<div class="cal">全種別は「ことばで頼む」から</div></div>'
                    '<div class="peril na"><div class="name">高潮・液状化・熱</div><div class="score">未較正</div>'
                    '<div class="cal">査定に含めません</div></div>'
                    '</div>'
                    f'<div class="prs-note">出典 不動産リスクスコア連携 ・ 対象住所 {_esc(addr)} ・ '
                    '本表示は参考の事前スクリーニングであり、法定の水害ハザードマップ確認（施行規則16条の4の3）の代替ではありません。 '
                    f'<a href="/juusetsu?case={quote(cid)}" style="text-decoration:underline">重説の災害リスク欄へ</a></div>'
                    + hz_links)
            else:
                inner = ('<div class="prs-note">較正済みの洪水スコアを取得できませんでした。'
                         'スコアは表示しません（推定値を捏造しない）。自治体公表のハザードマップで確認してください。</div>'
                         + hz_links)
        else:
            inner = ('<div class="prs-note">不動産リスク情報を取得できませんでした。接続設定を確認してください。'
                     'スコアは表示しません（捏造しない）。水害ハザードは当該自治体公表のハザードマップで確認・記載してください。</div>'
                     + hz_links)
    else:
        # 読み取り専用ページ契約（form/button禁止）＝取得アクションはGETリンクで明示起動
        fetch_link = ""
        if prop_name:
            q = f"/case?id={quote(cid)}&prs=1&addr={quote(prop_name)}"
            fetch_link = f'<a class="ri-go" href="{q}">「{_esc(prop_name)}」の災害リスクを取得 →</a> '
        inner = (
            '<div class="prs-note">未取得。'
            f'{fetch_link}'
            f'住所を指定して取得する場合は <a href="/console?prefill={quote("この物件の住所でリスク査定: ")}" style="text-decoration:underline">ことばで頼む</a> で'
            '「〈住所〉のリスクを査定して」と話してください。取得は明示操作のみ・較正済種別だけを表示します（未較正は査定に含めません）。</div>')
    return '<div class="ri-sech" style="margin-top:18px">災害リスク</div>' + inner


def _case_completion_section(data_dir: Path, cid: str, prop_name: str, *, form_mode: bool = False) -> str:
    """物件情報の完成度ボード: 合流済みレコード→揃っている項目(出典つき)＋不足(取得先リンク)＋書類追加。
    取得先リンクは既存の _is_allowed_acquisition_url を通過したものだけ実リンク化（安全側）。
    form_mode=False（/case=読み取り専用）は書類追加を専用ページへのリンクに、True（専用ページ）はフォームに。"""
    from hub_core import property_info as _pi, local_ocr as _loc
    merged = _pi.load_property_info(data_dir, cid)
    c = _pi.property_completion(merged)
    if not _loc.available():
        up = '<div class="gn">書類からの自動収集は macOS / Windows で使えます。</div>'
    elif form_mode:
        up = ('<form method="post" action="/property/collect?case=' + quote(cid) + '" '
              'enctype="multipart/form-data" style="margin:12px 0 2px;display:flex;gap:10px;'
              'align-items:center;flex-wrap:wrap">'
              '<label class="pf-l" style="margin:0">📄 書類を追加（図面・登記・調査報告書・設備表）</label>'
              '<input type="file" name="docs" accept="image/*,application/pdf" required style="font-size:18px">'
              '<button class="ri-go" type="submit">読み取って情報を集める</button>'
              f'<span class="gn">無料・端末内（{_esc(_loc.engine())}）・クラウド送信なし・1件ずつ足すと合流します</span></form>')
    else:
        up = ('<div style="margin:12px 0 2px"><a class="ri-go" href="/property/collect?case='
              + quote(cid) + '">📄 書類を追加して情報を集める →</a>'
              '<span class="gn" style="margin-left:8px">図面・登記・調査報告書・設備表（無料・端末内）</span></div>')
    if not merged:
        return ('<div class="ri-sech" style="margin-top:24px">物件情報の完成度</div>'
                '<div class="ri-card">まだ書類を読み込んでいません。図面・登記・調査報告書などを入れると、'
                '情報が自動で集まり、足りない項目が取得タスクになります。' + up + '</div>')
    pct = c["pct"]
    bar = ('<div style="background:#eee;border-radius:6px;height:10px;overflow:hidden;max-width:360px">'
           f'<div style="background:#2e7d32;height:100%;width:{pct}%"></div></div>')
    filled_rows = "".join(
        f'<tr><td>{_esc(f["label"])}</td><td><b>{_esc(f["value"])}</b></td>'
        f'<td class="gn">{_esc(f["source"] or "入力")}</td></tr>' for f in c["filled"])
    filled_tbl = (f'<div class="cw-tw"><table><thead><tr><th>項目</th><th>値</th><th>出典</th></tr></thead>'
                  f'<tbody>{filled_rows}</tbody></table></div>') if c["filled"] else ""
    miss_items = []
    for m in c["missing"]:
        url = (m.get("url") or "").strip()
        if url and _is_allowed_acquisition_url(url):
            link = (f' <a class="ri-go alt" href="{_esc(url)}" target="_blank" rel="noopener" '
                    f'style="font-size:18px;padding:4px 10px">取得先を開く →</a>')
        elif url:
            link = f' <span class="gn">取得先: 自治体の都市計画図で確認</span>'
        else:
            link = ' <span class="gn">現地 / 売主 / 管理会社に請求</span>'
        miss_items.append(f'<li>☐ {_esc(m["label"])}{link}</li>')
    miss_html = ('<ul style="line-height:1.9;margin:6px 0">' + "".join(miss_items) + '</ul>'
                 if miss_items else '<div class="gn">すべて揃っています。マイソク/重説を作成できます。</div>')
    addr = str(merged.get("address") or prop_name or "").strip()
    mapl = (f'<a class="ri-go alt" href="/map?q={quote(addr)}" target="_blank" rel="noopener" '
            f'style="font-size:18px;padding:4px 10px">📍 地図で現地確認</a>') if addr else ""
    return ('<div class="ri-sech" style="margin-top:24px">物件情報の完成度 '
            f'<span class="gn">{c["have"]}/{c["total"]}（{pct}%）</span></div>'
            f'<div class="ri-card">{bar}'
            f'<div style="margin-top:10px;font-weight:600">揃っている情報 {mapl}</div>{filled_tbl}'
            f'<div style="margin-top:12px;font-weight:600">❌ 足りない情報（クリックで取得先へ）</div>{miss_html}'
            f'{up}</div>')


def render_fax(data_dir: Path, params) -> str:
    """FAX（物確）ハーネス画面: outbox（作成→送信ゲート→送信）＋inbox（着信→物確回答）。既定Mock=実送信しない。"""
    from hub_core import operations as _ops, fax as _fax
    outbox = _ops.load_fax_outbox(data_dir)
    inbox = _ops.load_fax_inbox(data_dir)
    fax_connected = bool(getattr(_fax.build_fax_provider(data_dir), "connected", False))
    note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
            'FAXは<b>物件確認（物確）・マイソク送受信</b>に利用します。'
            '接続設定が完了するまでは<b>テスト動作となり、FAXは実送信しません</b>。サービス接続には'
            '責任者の承認が必要です。送信は必ず担当者が内容を確認して実行します。</div>')
    # 物確FAX作成フォーム
    create = ('<fieldset class="pf-set"><legend>物確FAXを作る</legend>'
              '<form method="post" action="/fax/new" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
              '<label class="pf-l" style="margin:0">物件</label>'
              + _property_select_html(data_dir, "case_id")
              + '<label class="pf-l" style="margin:0">送信先FAX番号</label>'
              '<input type="text" name="to_number" placeholder="03-1234-5678" required style="font-size:18px;padding:6px 9px">'
              '<button class="ri-go" type="submit">物確FAXを作成</button>'
              '<span class="gn">物件を選ぶと合流情報から物確FAX本文を自動生成します</span></form></fieldset>')
    # outbox 表
    ob_rows = ""
    for j in reversed(outbox):
        st = j.get("status", "")
        badge = {"queued": '<span class="ri-badge warn">送信待ち</span>',
                 "gated": '<span class="ri-badge warn">確認済</span>',
                 "sent": '<span class="ri-badge ok">処理済み</span>'}.get(
                     st, _esc(_visible_data_value("状態", st)))
        act = ""
        if st in ("queued", "gated"):
            approval = (
                '<label class="pf-l" style="display:block;margin:0 0 6px">'
                '<input type="checkbox" name="allow_real_send" value="1" required> '
                'このFAXを今ここで1回だけ実送信する</label>'
                if fax_connected else ""
            )
            act = ('<form method="post" action="/fax/send" style="margin:0">'
                   f'<input type="hidden" name="job_id" value="{_esc(j.get("job_id",""))}">'
                   + approval
                   +
                   '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">送信を確認して送る</button></form>')
        elif st == "sent":
            act = ('<span class="gn">実送信済み</span>' if j.get("sent")
                   else '<span class="gn">テスト動作のため未送信</span>')
        ob_rows += (f'<tr><td>{_esc(j.get("title",""))}</td><td>{_esc(j.get("to_number",""))}</td>'
                    f'<td>{badge}</td><td>{act}</td></tr>')
    outbox_tbl = ('<div class="cw-tw"><table><thead><tr><th>件名</th><th>送信先</th><th>状態</th><th>操作</th></tr></thead>'
                  f'<tbody>{ob_rows}</tbody></table></div>' if outbox
                  else '<div class="ri-card cm">まだ物確FAXはありません。</div>')
    # inbox 表
    ib_rows = ""
    for i in reversed(inbox):
        rep = i.get("reply") or {}
        summ = " ／ ".join(
            f"{_visible_data_value('項目', k)}: {_visible_data_value(str(k), v)}"
            for k, v in rep.items()) or "（回答未抽出）"
        ib_rows += (f'<tr><td>{_esc(i.get("fax_id",""))}</td><td>{_esc(i.get("from_number",""))}</td>'
                    f'<td>{_esc(summ)}</td><td class="gn">{_esc(_display_datetime(i.get("received_at", "")))}</td></tr>')
    inbox_tbl = ('<div class="cw-tw"><table><thead><tr><th>受信ID</th><th>差出</th><th>物確回答</th><th>受信</th></tr></thead>'
                 f'<tbody>{ib_rows}</tbody></table></div>' if inbox
                 else '<div class="ri-card cm">着信FAXはありません。サービス接続後は自動で取り込みます。</div>')
    usage = _usage_strip(
        "物件確認（物確）FAXの作成・送信・着信の取込を行う画面です。物確やマイソクのやり取りにFAXがよく使われます。",
        "物件を選ぶと合流情報から物確FAX本文を自動で下書きします。着信FAXは受信取込で台帳に入り、回答を抽出します。",
        ["物件を選んで物確FAXを作る",
         "内容を人が確認して送る",
         "着信FAXの物確回答を確認する"],
        [("/line", "LINE"), ("/calls", "物確電話"), ("/properties", "物件")])
    inner = (ui.page_head("FAX（物件確認）",
             "物件確認FAXの作成・送信確認・着信取込をまとめて行います。")
             + usage + note + create
             + ui.section("送信待ち・送信済み") + outbox_tbl
             + ui.section("受信した物件確認FAX") + inbox_tbl)
    return _wrap_main("properties", "/properties", "FAX（物確）", inner)


def render_calls(data_dir: Path, params) -> str:
    """物確電話（着信IVR自動応答）画面: 通話ログ＋発信者台帳(電話→業者)＋物確番号割当＋簡易分析。既定Mock。"""
    from hub_core import operations as _ops
    calls = _ops.load_calls(data_dir)
    directory = _ops.load_caller_directory(data_dir)
    note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
            '物確（物件確認）電話を<b>取らずに自動応答</b>する仕組み（ぶっかくん型）。業者向けマイソクに物確番号を'
            '載せ、業者が電話でダイヤル→物件を特定→<b>あいのてのデータから現在の状態</b>（取扱中/成約済）を返し、'
            '詳細希望なら自動FAX返信。<b>実テレフォニー業者の接続・実架電は人間ゲート</b>。状態はデータにある時だけ'
            '返します（無ければ担当へ・捏造しません）。')
    note += '</div>'
    # 発信者台帳（電話→業者）登録＋物確番号割当
    forms = ('<fieldset class="pf-set"><legend>設定</legend>'
             '<form method="post" action="/calls/directory" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
             '<label class="pf-l" style="margin:0">発信者台帳: 電話番号</label>'
             '<input type="text" name="number" placeholder="03-1234-5678" required style="font-size:18px;padding:6px 9px">'
             '<label class="pf-l" style="margin:0">→ 業者名</label>'
             '<input type="text" name="company" placeholder="株式会社◯◯不動産" required style="font-size:18px;padding:6px 9px">'
             '<button class="ri-qbtn" type="submit">登録</button></form>'
             '<form method="post" action="/calls/code" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
             '<label class="pf-l" style="margin:0">物確番号を割当: 物件</label>'
             + _property_select_html(data_dir, "case_id")
             + '<button class="ri-qbtn" type="submit">物確番号を発行</button>'
             '<span class="gn">この番号を業者向けマイソクに載せます</span></form></fieldset>')
    # 分析（業者別着信数）
    by_co: dict = {}
    for c in calls:
        by_co[c.get("company") or "(未登録)"] = by_co.get(c.get("company") or "(未登録)", 0) + 1
    ana = " ／ ".join(f"{k}: {v}件" for k, v in sorted(by_co.items(), key=lambda x: -x[1])[:6]) or "着信なし"
    # 通話ログ
    rows = "".join(
        f'<tr><td>{_esc(c.get("from_number",""))}</td><td>{_esc(c.get("company") or "(未登録)")}</td>'
        f'<td>{_esc(c.get("property_name",""))}</td><td>{_esc(c.get("status",""))}</td>'
        f'<td>{_esc(c.get("action",""))}</td><td class="gn">{_esc(c.get("received_at",""))}</td></tr>'
        for c in reversed(calls))
    log_tbl = ('<div class="cw-tw"><table><thead><tr><th>発信</th><th>業者</th><th>物件</th><th>状態</th>'
               f'<th>応答</th><th>着信</th></tr></thead><tbody>{rows}</tbody></table></div>' if calls
               else '<div class="ri-card cm">着信はまだありません。サービス接続後は自動で取り込みます。</div>')
    dir_rows = "".join(f'<tr><td>{_esc(r.get("number",""))}</td><td>{_esc(r.get("company",""))}</td></tr>'
                       for r in directory)
    dir_tbl = (f'<div class="cw-tw"><table><thead><tr><th>電話番号</th><th>業者</th></tr></thead>'
               f'<tbody>{dir_rows}</tbody></table></div>' if directory
               else '<div class="ri-card cm">発信者台帳は空です。着信のたびに登録すると業者特定できます。</div>')
    usage = _usage_strip(
        "物確（物件確認）電話をIVRで自動応答し、通話ログと業者台帳を残す画面です。",
        "業者向けマイソクに載せた物確番号から物件を特定し、あいのてのデータにある現在の状態を返します。"
        "着信は取込で通話ログに入ります。",
        ["発信者台帳に電話番号と業者名を登録する",
         "物件に物確番号を発行してマイソクに載せる",
         "通話ログで業者別の着信を確認する"],
        [("/fax", "FAX物確"), ("/maisoku", "マイソク"), ("/properties", "物件")])
    inner = (ui.page_head("物確電話（自動応答）",
             "物確電話に自動応答し、物確番号から現在の状態を案内します。接続前は実際の応答を行いません。")
             + usage + note + forms
             + ui.section("着信分析") + f'<div class="gn" style="margin:-6px 0 10px">{_esc(ana)}</div>' + log_tbl
             + ui.section("発信者台帳（電話→業者）") + dir_tbl)
    return _wrap_main("properties", "/properties", "物確電話", inner)


def _line_uid_short(uid: str) -> str:
    uid = str(uid or "")
    return ("…" + uid[-6:]) if len(uid) > 8 else (uid or "（不明）")


def _published_property_choices(data_dir: Path) -> list:
    """LIFFエクスポート済みの公開物件（liff-export/properties.json）を [(id, label)] で返す。
    Flexカード送信の候補＝op が引くのと同一ソース。無ければ空（先にエクスポートが要る）。"""
    import json as _json
    p = Path(data_dir) / "liff-export" / "properties.json"
    if not p.is_file():
        return []
    try:
        rows = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        name = str(r.get("name") or "").strip()
        room = str(r.get("roomNumber") or "").strip()
        title = f"{name} {room}".strip() if room else name
        try:
            man = f"{float(r.get('rent'))/10000:g}万円" if r.get("rent") else ""
        except (TypeError, ValueError):
            man = ""
        label = " ".join(x for x in (title, str(r.get("station") or "").strip(), man) if x)
        out.append((str(r.get("id")), label or str(r.get("id"))))
    return out


def _property_card_form(data_dir: Path, uid: str, choices: list) -> str:
    """受信メッセージ相手へ公開物件をFlexカードで送る導線（チェックボックス→outboxへqueued・送信は別ゲート）。
    候補が無ければ「先にLIFFエクスポート」を案内。折り畳み（details）でUIを圧迫しない。"""
    if not uid:
        return ""
    if not choices:
        return ('<details style="margin-top:6px"><summary style="cursor:pointer;font-size:18px;color:var(--muted,#5C6B63)">'
                '物件カードを送る</summary>'
                '<div class="gn" style="margin:6px 0 0">公開物件がありません。上の「公開用データを準備」で'
                '公開許可を確認済みの物件を準備すると、ここから写真つきカードを送れます。</div></details>')
    boxes = "".join(
        f'<label style="display:flex;gap:6px;align-items:center;font-size:18px;margin:2px 0">'
        f'<input type="checkbox" name="property_ids" value="{_esc(pid)}">{_esc(label)}</label>'
        for pid, label in choices[:30])
    return ('<details style="margin-top:6px"><summary style="cursor:pointer;font-size:18px;color:var(--muted,#5C6B63)">'
            '物件カードを送る</summary>'
            '<form method="post" action="/line/property-card" style="margin-top:6px;padding:8px;'
            'background:var(--washi,#F5F1E8);border-radius:6px">'
            f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
            '<div style="max-height:180px;overflow:auto">' + boxes + '</div>'
            '<div style="display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap">'
            '<input type="text" name="badge" placeholder="バッジ（任意・例: 新着）" maxlength="8" '
            'style="font-size:18px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;width:150px">'
            '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
            'カードを送信待ちに追加</button>'
            '<span class="gn">選んだ物件を写真つきカードで送信待ちに積みます（送信は確認ゲート経由）。</span>'
            '</div></form></details>')


def _inquiry_block(uid: str, raw_text: str, qi: dict, inquiries_by_key: dict) -> str:
    """案内可否の問い合わせ（ポータルURL反響）に対する物確ドラフト導線（内見希望フォームと同じ様式）。
    未作成→「物確ドラフトを作る」ボタン（ワンクリック inquiry_create）。作成済み→状態表示
    （未確認なら可否記入フォーム＝inquiry_resolve、回答済なら記入済みの可否を表示）。捏造しない。"""
    from hub_core import operations as _ops
    src_key = _ops._inquiry_src_key(uid, raw_text)
    rec = inquiries_by_key.get(src_key)
    box = ('display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px;'
           'padding:8px;background:var(--warn-bg,#fef6e7);border-radius:6px')
    urls = qi.get("urls") or []
    url_html = ""
    if urls:
        items = "".join(
            f'<li style="margin:1px 0"><b>{_esc(u.get("portal") or "その他")}</b>: '
            f'<span style="word-break:break-all">{_esc(u.get("url") or "")}</span></li>' for u in urls)
        url_html = f'<ul style="margin:2px 0 0;padding-left:18px;font-size:18px;flex-basis:100%">{items}</ul>'
    if rec is None:   # 未作成: ワンクリックで案内可否確認（物確ドラフト）を作成
        return (f'<form method="post" action="/line/inquiry" style="{box}">'
                f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
                f'<input type="hidden" name="text" value="{_esc(raw_text)}">'
                '<span class="pf-l" style="margin:0">案内可否確認:</span>'
                '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
                '物確ドラフトを作る</button>'
                '<span class="gn" style="flex-basis:100%">元付へ物確する「案内可否確認」を作成します'
                '（物件名・条件は本文から推測しません＝URLと本文だけを記録）。</span>'
                f'{url_html}</form>')
    if rec.get("status") == "回答済":   # 記入済みの可否を表示（回答は送信待ちに積まれている）
        ans = rec.get("answer") or {}
        parts = [x for x in (f'案内: {ans.get("viewing","")}' if ans.get("viewing") else "",
                             f'空室: {ans.get("status","")}' if ans.get("status") else "") if x]
        return ('<div class="gn" style="margin-top:6px;padding:6px 8px;'
                'background:var(--washi,#F5F1E8);border-radius:6px">'
                f'案内可否確認: <span class="ri-badge ok">回答済</span>　{_esc(" / ".join(parts))}'
                '（顧客への回答は送信待ちに積まれています）。</div>')
    # 作成済み・未確認: 担当が可否を記入（→ inquiry_resolve・回答は line_send(queued) でドラフト化）
    return (f'<form method="post" action="/line/inquiry-resolve" style="{box}">'
            f'<input type="hidden" name="inquiry_id" value="{_esc(rec.get("inquiry_id") or "")}">'
            '<span class="pf-l" style="margin:0">案内可否確認 '
            '<span class="ri-badge warn">未確認</span>:</span>'
            '<select name="availability" required '
            'style="font-size:18px;padding:5px 8px;border:1px solid var(--line);border-radius:6px">'
            '<option value="">（可否を選ぶ）</option><option>可</option><option>要連絡</option>'
            '<option>不可</option></select>'
            '<select name="property_status" '
            'style="font-size:18px;padding:5px 8px;border:1px solid var(--line);border-radius:6px">'
            '<option value="">空室状況（任意）</option><option>取扱中</option><option>商談中</option>'
            '<option>成約済</option><option>取扱終了</option></select>'
            '<input type="text" name="note" placeholder="補足（任意）" maxlength="60" '
            'style="font-size:18px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;flex:1;min-width:120px">'
            '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
            '可否を記入＋顧客回答を作る</button>'
            '<span class="gn" style="flex-basis:100%">記入すると回答済にし、顧客への回答を送信待ちに積みます'
            '（実送信は送信ゲート経由）。</span></form>')


def _hearing_block(uid: str, raw_text: str, hi: dict, hearings_by_key: dict) -> str:
    """希望条件ヒアリング（LIFF条件フォーム着信）→希望条件レコードの台帳化導線（内見/案内可否と同じ様式）。
    未台帳→「希望条件を台帳化」ボタン（ワンクリック hearing_create）。台帳済→受付済バッジ＋条件サマリ。
    サマリは検出した既知ラベルのみ（推測・補完しない＝捏造しない）。"""
    from hub_core import operations as _ops
    order = ("種別", "物件種別", "エリア", "賃料上限", "予算上限", "間取り", "入居時期", "時期", "こだわり")
    src_key = _ops._hearing_src_key(uid, hi.get("receipt") or "", raw_text)
    rec = hearings_by_key.get(src_key)
    box = ('display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px;'
           'padding:8px;background:var(--warn-bg,#fef6e7);border-radius:6px')
    fields = hi.get("fields") or {}
    summary = " / ".join(f'{k}: {fields[k]}' for k in order if fields.get(k))
    sum_html = (f'<div class="gn" style="flex-basis:100%;word-break:break-all">{_esc(summary)}</div>'
                if summary else '')
    if rec is None:   # 未台帳: ワンクリックで希望条件を台帳化
        return (f'<form method="post" action="/line/hearing" style="{box}">'
                f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
                f'<input type="hidden" name="text" value="{_esc(raw_text)}">'
                '<span class="pf-l" style="margin:0">希望条件:</span>'
                '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
                '希望条件を台帳化</button>'
                '<span class="gn" style="flex-basis:100%">お客様の希望条件を台帳に記録します'
                '（本文にある項目だけ・推測はしません）。条件に合う物件のご提案にお使いください。</span>'
                f'{sum_html}</form>')
    # 台帳済み: 受付済バッジ＋記録したサマリ
    rfields = rec.get("fields") or {}
    rsummary = " / ".join(f'{k}: {rfields[k]}' for k in order if rfields.get(k))
    return ('<div class="gn" style="margin-top:6px;padding:6px 8px;'
            'background:var(--washi,#F5F1E8);border-radius:6px">'
            f'希望条件: <span class="ri-badge ok">台帳済</span>　{_esc(rsummary)}'
            '（担当が物件のご提案にお使いください）。</div>')


def _usage_strip(what: str, source: str, actions: list, links: list) -> str:
    """折りたたみ式「この画面の使い方」ストリップ。各画面の上部に置く。
    実装にある機能だけを書く（捏造しない）。文面は japanese-tech-writing 準拠で一文を短く。
    what   : この画面は何か（1文）
    source : 情報の出どころ（1文）
    actions: よくある次の操作（文字列を3つ程度）
    links  : 関連画面 [(href, label)]"""
    acts = "".join(f'<li style="margin:1px 0">{_esc(a)}</li>' for a in actions if a)
    lks = "　".join(
        f'<a href="{_esc(h)}" style="text-decoration:underline;color:var(--sumi,#0a2540)">{_esc(l)}</a>'
        for h, l in links if h)
    lk_html = f'<div style="margin-top:7px"><b>関連画面</b>：{lks}</div>' if lks else ""
    return ('<details class="ri-usage" style="margin:0 0 12px;border:1px solid var(--line,#e2e6ea);'
            'border-radius:8px;background:var(--panel2,#f6f8fa)">'
            '<summary style="cursor:pointer;padding:9px 13px;font-size:18px;font-weight:600;'
            'color:var(--sumi,#0a2540)">この画面の使い方</summary>'
            '<div style="padding:2px 13px 12px;font-size:18px;color:var(--ink2,#425466);line-height:1.7">'
            f'<div><b>この画面</b>：{_esc(what)}</div>'
            f'<div style="margin-top:4px"><b>情報の出どころ</b>：{_esc(source)}</div>'
            f'<div style="margin-top:7px"><b>よくある操作</b>'
            f'<ol style="margin:3px 0 0;padding-left:20px">{acts}</ol></div>'
            f'{lk_html}</div></details>')


def _line_msg_ts(row) -> str:
    """会話台帳行の時系列キー。harness createdAt → 無ければあいのての recorded_at → 空。
    どちらもISO8601（YYYY-MM-DDT…）なので文字列比較で時系列に並ぶ。"""
    return str(row.get("created_at") or row.get("recorded_at") or "")


def _line_threads(data_dir: Path) -> list:
    """会話台帳（line_inbox=受信＋pullした送信）とあいのて outbox を**友だち別スレッド**に束ねる。
    返り: [{"uid","name","entries":[...]}] を最終メッセージ時刻の降順で。
    entries は各 {"ts","side"(in/out),"text","state","row"} を時系列昇順（古い→新しい）で持つ。
    二重表示の回避: あいのてが実送信し harness 経由で台帳へ戻った送信（outbox.external_id が台帳の
    harness_msg_id に一致）は outbox 側を出さず、台帳側（実際に流れた記録）を正とする。"""
    from hub_core import operations as _ops
    inbox = _ops.load_line_inbox(data_dir)
    outbox = _ops.load_line_outbox(data_dir)
    ledger_ids = {r.get("harness_msg_id") for r in inbox if r.get("harness_msg_id")}
    threads: dict = {}

    def _th(uid):
        return threads.setdefault(uid, {"uid": uid, "name": "", "entries": []})

    for r in inbox:
        uid = r.get("line_user_id") or r.get("harness_friend_id") or "(不明)"
        t = _th(uid)
        if r.get("line_display_name"):
            t["name"] = r.get("line_display_name")
        side = "out" if (r.get("direction") == "outgoing") else "in"
        state = "recv" if side == "in" else "sent-harness"
        t["entries"].append({"ts": _line_msg_ts(r), "side": side,
                             "text": r.get("text") or "", "state": state, "row": r})
    for m in outbox:
        ext = m.get("external_id") or ""
        if ext and ext in ledger_ids:
            continue   # 実送信済みで台帳に既出＝二重表示しない
        uid = m.get("to_user") or "(不明)"
        st = m.get("status") or ""
        if st in ("queued", "gated"):
            state = "pending"
        elif st == "sent" and not m.get("sent"):
            state = "mock"
        elif st == "sent":
            state = "sent-rios"
        else:
            state = st or "pending"
        _th(uid)["entries"].append({"ts": str(m.get("created_at") or ""), "side": "out",
                                    "text": m.get("text") or "", "state": state, "row": m})
    out = list(threads.values())
    for t in out:
        t["entries"].sort(key=lambda e: e["ts"])
        t["last_ts"] = t["entries"][-1]["ts"] if t["entries"] else ""
    out.sort(key=lambda t: t["last_ts"], reverse=True)
    return out


_LINE_OUT_LABEL = {"sent-harness": "送信済み", "sent-rios": "送信済み（あいのて）",
                   "mock": "テスト動作のため未送信", "pending": "送信待ち（確認前）"}


def _line_bubble_out(e: dict, *, real_send: bool = False) -> str:
    """送信バブル（右寄せ）。pending は破線＋送信確認ボタン・実送信済みとの区別を正直に出す。"""
    state = e.get("state", "")
    label = _LINE_OUT_LABEL.get(state, "送信")
    pending = state == "pending"
    bg = "var(--warn-bg,#fef6e7)" if pending else "#e7f0fb"
    border = ("1px dashed #b45309" if pending else "1px solid #bcd3ef")
    radius = "12px 12px 3px 12px"
    send_btn = ""
    if pending:
        mid = (e.get("row") or {}).get("msg_id", "")
        approval = (
            '<label class="pf-l" style="display:block;margin:0 0 5px">'
            '<input type="checkbox" name="allow_real_send" value="1" required> '
            'この返信を今ここで1回だけ実送信する</label>'
            if real_send else ""
        )
        send_btn = ('<form method="post" action="/line/send" style="margin:5px 0 0">'
                    f'<input type="hidden" name="msg_id" value="{_esc(mid)}">'
                    + approval
                    +
                    '<button class="ri-go" type="submit" style="padding:4px 10px;font-size:18px">'
                    '送信を確認して送る</button></form>')
    return ('<div style="display:flex;justify-content:flex-end;margin:6px 0">'
            f'<div style="max-width:78%;background:{bg};border:{border};border-radius:{radius};'
            'padding:7px 11px;font-size:18px;color:var(--ink,#2a3f54)">'
            f'{_esc(e.get("text") or "（本文なし）")}'
            f'<div class="gn" style="margin-top:3px;font-size:18px">{_esc(label)}</div>'
            f'{send_btn}</div></div>')


def _line_bubble_in(data_dir: Path, uid: str, e: dict, card_choices, inquiries_by_key,
                    hearings_by_key) -> str:
    """受信バブル（左寄せ）＋この受信が引き金の操作導線（内見予約/案内可否/希望条件）。
    検出は本文から（render時）。バッジと操作は既存のブロック関数を再利用する。"""
    from hub_core import line as _line
    text = e.get("text") or ""
    badges = ""
    forms = ""
    vi = _line.viewing_intent(text)
    if vi["is_viewing"]:
        badges += '<span class="ri-badge warn" style="margin-left:6px">内見希望</span>'
        if uid and uid != "(不明)":
            cand = vi["candidate_at"]
            hint = ('（メッセージから候補日時を推定・必ず確認してください）' if cand
                    else '（日時はメッセージから確定できません・担当が入力してください）')
            forms += ('<form method="post" action="/line/viewing" '
                      'style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px;'
                      'padding:8px;background:var(--warn-bg,#fef6e7);border-radius:6px">'
                      f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
                      '<span class="pf-l" style="margin:0">内見予約:</span>'
                      + _property_select_html(data_dir, "case_id")
                      + f'<input type="datetime-local" name="event_at" value="{_esc(cand)}" required '
                      'style="font-size:18px;padding:5px 8px;border:1px solid var(--line);border-radius:6px">'
                      '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
                      '内見を予約＋確認返信を作る</button>'
                      f'<span class="gn" style="flex-basis:100%">{hint}</span></form>')
    qi = _line.inquiry_intent(text)
    if qi["is_inquiry"]:
        badges += '<span class="ri-badge warn" style="margin-left:6px">案内可否</span>'
        if uid and uid != "(不明)":
            forms += _inquiry_block(uid, text, qi, inquiries_by_key)
    hi = _line.hearing_intent(text)
    if hi["is_hearing"]:
        badges += '<span class="ri-badge warn" style="margin-left:6px">希望条件</span>'
        if uid and uid != "(不明)":
            forms += _hearing_block(uid, text, hi, hearings_by_key)
    bubble = ('<div style="display:flex;justify-content:flex-start;margin:6px 0">'
              '<div style="max-width:88%;background:var(--panel2,#f1f5f9);'
              'border:1px solid var(--line,#e2e6ea);border-radius:12px 12px 12px 3px;'
              'padding:7px 11px;font-size:18px;color:var(--ink,#2a3f54)">'
              f'{_esc(text or "（テキストなし）")}{badges}'
              '<div class="gn" style="margin-top:3px;font-size:18px">受信</div></div></div>')
    return bubble + forms


def render_line(data_dir: Path, params) -> str:
    """LINEハーネス画面: 友だち別の会話スレッド（受信＋送信を台帳から時系列表示）＋返信（送信ゲート）。既定Mock=実送信しない。"""
    from hub_core import operations as _ops, connections as _conn
    note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
            'LINEは<b>相手から届いたメッセージに返信</b>するのが基本です。相手の識別番号を覚える必要はありません。'
            '接続設定が完了するまでは<b>テスト動作となり、実送信しません</b>。'
            '公式アカウントの接続には責任者の承認が必要です。返信は必ず担当者が内容を確認して送ります。</div>')
    # harnessから着信をpull取込（公開httpsを持たないあいのてはwebhook pushを受けられないので既定はpull）。
    # 未設定なら押しても no-op（Mockのまま）。実接続は人間ゲート。
    pull_hint = ('LINE連携に接続済みです。' if _conn.harness_configured()
                 else 'LINEの接続設定が未完了です。接続するまで取り込みません。')
    pull_btn = ('<form method="post" action="/line/pull" style="margin:0 0 14px;display:flex;'
                'gap:8px;align-items:center;flex-wrap:wrap">'
                '<button class="ri-go" type="submit" style="padding:6px 13px;font-size:18px">'
                '最新の会話を取り込む</button>'
                f'<span class="gn">{_esc(pull_hint)}</span></form>')
    # LIFF内見予約アプリへの物件書出（公開opt-in済の物件だけを properties.json＋写真へ）。/op 経由で
    # liff_export を実行（認証viewer・役割ゲートはコア側で強制）。書出後 /line に戻り件数を表示する。
    def _p1(key):
        v = params.get(key) if isinstance(params, dict) else None
        return (v[0] if isinstance(v, list) and v else v) if v is not None else None
    exp_banner = ""
    if _p1("exported") is not None:
        _ex = _esc(_public_count_param(_p1("exported")))
        _sk = _esc(_public_count_param(_p1("skipped")))
        exp_banner = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #16a34a">'
                      f'お客様向けページの公開用データを準備しました: <b>{_ex}</b> 件'
                      f'（必須項目が足りず準備できなかったもの <b>{_sk}</b> 件）。'
                      '内容を確認してから公開してください。</div>')
    liff_btn = ('<form method="post" action="/op" style="margin:0 0 14px;display:flex;'
                'gap:8px;align-items:center;flex-wrap:wrap">'
                '<input type="hidden" name="op" value="liff_export">'
                '<button class="ri-go" type="submit" style="padding:6px 13px;font-size:18px">'
                '公開用データを準備</button>'
                '<span class="gn">公開許可を確認済みの物件だけを内見予約ページ用に準備します'
                '（勝手に全物件を公開しません）。</span></form>')
    pull_btn = pull_btn + exp_banner + liff_btn
    # 会話スレッド（友だち別・時系列・受信=左/送信=右）。送信は pull で台帳に入った実際の送信＋あいのて outbox。
    card_choices = _published_property_choices(data_dir)   # Flexカード送信の候補（公開物件・1回だけ読む）
    # 案内可否確認（ポータルURL反響→物確ドラフト）: 既存レコードを src_key で引く（表示時=render時に検出）。
    inquiries_by_key = {r.get("src_key"): r for r in _ops.load_inquiries(data_dir) if r.get("src_key")}
    # 希望条件ヒアリング（LIFF条件フォーム着信）: 既存レコードを src_key で引く（表示時=render時に検出）。
    hearings_by_key = {r.get("src_key"): r for r in _ops.load_hearings(data_dir) if r.get("src_key")}
    threads = _line_threads(data_dir)
    conv = ""
    for th in threads:
        uid = th["uid"]
        has_uid = bool(uid and uid != "(不明)")
        bubbles = ""
        for e in th["entries"]:
            if e["side"] == "in":
                bubbles += _line_bubble_in(data_dir, uid, e, card_choices,
                                           inquiries_by_key, hearings_by_key)
            else:
                bubbles += _line_bubble_out(e, real_send=_conn.harness_configured())
        name = th.get("name") or ""
        head = (f'<div class="cm" style="font-weight:600">{_esc(name) + "　" if name else ""}'
                f'<span style="font-weight:400">相手 {_esc(_line_uid_short(uid))}</span></div>')
        reply = ""
        if has_uid:
            reply = ('<form method="post" action="/line/new" '
                     'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                     f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
                     '<input type="hidden" name="kind" value="push">'
                     '<input type="text" name="text" placeholder="返信を書く…" required '
                     'style="font-size:18px;padding:5px 9px;flex:1;min-width:200px;'
                     'border:1px solid var(--line);border-radius:6px">'
                     '<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">'
                     '返信を送信待ちに追加</button></form>')
        card_form = _property_card_form(data_dir, uid, card_choices) if has_uid else ""
        # 会話ファーストの文脈アクション: この会話からIT重説を1クリックで開始（案件が無ければ自動作成）。
        # 案件IDを打たせない＝起点は会話。表示名を渡して自動作成案件の顧客名にする（分かる範囲・捏造しない）。
        it_start = ""
        if has_uid:
            it_start = ('<form method="post" action="/line/it-start" style="margin-top:6px">'
                        f'<input type="hidden" name="to_user" value="{_esc(uid)}">'
                        f'<input type="hidden" name="display_name" value="{_esc(name)}">'
                        '<button class="ri-go ghost" type="submit" style="padding:5px 11px;font-size:18px">'
                        'このお客様とIT重説を始める</button>'
                        '<span class="gn" style="margin-left:8px">案件が無ければ自動で作成し、IT重説の'
                        '確認画面へ進みます（案件IDの入力は不要）。</span></form>')
        conv += (f'<div class="ri-card" style="margin-bottom:12px">{head}'
                 f'<div style="margin:6px 0 2px">{bubbles}</div>{reply}{card_form}{it_start}</div>')
    # 空状態は「最初の1件を作る導線」（データ0件でも次の一歩が分かる＝白画面にしない）
    conv_html = conv or (
        '<div class="ri-card">'
        '<div class="cm">まだ会話はありません。</div>'
        '<div class="gn" style="margin:6px 0 0">お客様が公式アカウントを友だち追加してメッセージを送ると、'
        'ここに会話スレッドとして表示され、返信できます。まず接続設定を済ませ、上の'
        '<b>「最新の会話を取り込む」</b>で会話を取り込んでください。</div>'
        '<div style="margin-top:8px"><a class="ri-go ghost" href="/connections">接続設定を開く</a></div>'
        '</div>')
    usage = _usage_strip(
        "起点はお客様との会話です。この画面が仕事場になります。友だちごとのスレッドで会話を一覧し、"
        "返信や次の手続き（内見予約・案内可否・IT重説）をそのまま始めます。案件IDの入力は要りません。",
        "お客様のトークを「最新の会話を取り込む」で読み込みます。受信も、写真つきカードや返信の送信も、"
        "両方が台帳に残ります。",
        ["「最新の会話を取り込む」で会話を読み込む",
         "受信メッセージに返信を書き、内容を確認して送る",
         "「このお客様とIT重説を始める」で案件を自動作成し、IT重説へ進む"],
        [("/connections", "接続設定"), ("/it", "IT重説"), ("/viewings", "内見予約"),
         ("/customers", "顧客台帳")])
    inner = (ui.page_head("LINE", "友だちごとの会話スレッド。返信は担当者が内容を確認して送ります。")
             + usage + note + pull_btn
             + ui.section("会話（友だち別スレッド・受信左／送信右）") + conv_html)
    return _wrap_main("customers", "/customers", "LINE", inner)


def render_reins(data_dir: Path, params) -> str:
    """REINS登録の準備・法定期限管理・番号記録（REINSには一切アクセスしない・登録は会員が手入力）。"""
    import datetime as _dt
    from hub_core import property_info as _pi
    note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
            '専任・専属専任媒介は<b>REINS登録が法定義務</b>（宅建業法34条の2）。ただしREINSは会員制で'
            '<b>公式APIも一括入稿機能も無く</b>、登録は会員がREINS画面にログインして<b>手入力</b>します。'
            'この画面はあいのて側で<b>入稿シート（手入力の転記元）・登録期限・登録番号</b>を管理します。'
            'REINSへの送信・スクレイピング・自動登録はしません。祝日は未考慮のため実期限は前倒しの可能性。</div>')
    med_opts = ('<option value="">（媒介種別）</option>'
                '<option value="専任媒介">専任媒介（7営業日）</option>'
                '<option value="専属専任媒介">専属専任媒介（5営業日）</option>'
                '<option value="一般媒介">一般媒介（登録任意）</option>')
    forms = ('<fieldset class="pf-set"><legend>REINS入稿準備（期限計算＋入稿シート生成）</legend>'
             '<form method="post" action="/reins/prepare" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
             '<label class="pf-l" style="margin:0">物件</label>' + _property_select_html(data_dir, "case_id")
             + '<label class="pf-l" style="margin:0">媒介</label>'
             f'<select name="mediation" required style="font-size:18px;padding:6px 9px">{med_opts}</select>'
             '<label class="pf-l" style="margin:0">契約日</label>'
             '<input type="date" name="contract_date" required style="font-size:18px;padding:6px 9px">'
             '<button class="ri-go" type="submit">入稿シートを作成＋期限を計算</button></form>'
             '<form method="post" action="/reins/record" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
             '<label class="pf-l" style="margin:0">登録後のREINS番号を記録</label>'
             + _property_select_html(data_dir, "case_id")
             + '<input type="text" name="reins_no" placeholder="REINS物件番号" required style="font-size:18px;padding:6px 9px">'
             '<button class="ri-qbtn" type="submit">登録済にする</button>'
             '<span class="gn">会員画面で登録した後に記録します</span></form></fieldset>')
    # 状態表: reins関連フィールドが入っている物件を一覧（期限・状態・番号・入稿シート）
    today = _dt.date.today().isoformat()
    rows = ""
    for c in _property_options(data_dir):
        cid = c["case_id"]
        f = _pi.load_property_info(data_dir, cid) or {}
        status = str(f.get("reins_status") or "")
        med = str(f.get("reins_mediation") or "")
        deadline = str(f.get("reins_deadline") or "")
        no = str(f.get("reins_no") or "")
        if not (status or med or no):
            continue   # REINS未着手の物件は出さない（金メッキしない）
        if no:
            badge = '<span class="ri-badge ok">登録済</span>'
        elif deadline and deadline < today:
            badge = '<span class="ri-badge" style="background:#fee2e2;color:#991b1b">期限超過</span>'
        elif status:
            badge = '<span class="ri-badge warn">準備中</span>'
        else:
            badge = _esc(status or "—")
        sheet = (f'<a href="/doc/preview?doc=REINS-{quote(cid)}" target="_blank">入稿シート</a>'
                 if status else "—")
        rows += (f'<tr><td>{_esc(c["name"])}</td><td>{_esc(med or "—")}</td>'
                 f'<td>{_esc(deadline or "—")}</td><td>{badge}</td>'
                 f'<td>{_esc(no or "—")}</td><td>{sheet}</td></tr>')
    tbl = ('<div class="cw-tw"><table><thead><tr><th>物件</th><th>媒介</th><th>登録期限</th>'
           f'<th>状態</th><th>REINS番号</th><th>入稿</th></tr></thead><tbody>{rows}</tbody></table></div>'
           if rows else '<div class="ri-card cm">REINS準備中の物件はまだありません。上のフォームで入稿準備すると、ここに期限と状態が並びます。</div>')
    usage = _usage_strip(
        "専任・専属専任媒介のREINS登録に向けて、入稿シート・登録期限・登録番号を管理する画面です。",
        "物件と媒介種別を選ぶと、契約日から登録期限（専任7営業日／専属専任5営業日）を計算します。"
        "REINSには接続せず、登録は会員が画面に手入力します。",
        ["物件と媒介種別を選んで期限と入稿シートを作る",
         "入稿シートをREINS手入力の転記元として使う",
         "登録後にREINS登録番号を記録する"],
        [("/properties", "物件"), ("/juusetsu", "重説"), ("/line", "LINE")])
    inner = (ui.page_head("REINS（登録準備・期限管理）",
             "専任/専属専任の法定登録期限を管理し、手入力の転記元（入稿シート）を作ります。REINSには接続しません。")
             + usage + note + forms + ui.section("REINS登録状況") + tbl)
    return _wrap_main("ledger", "/ledger", "REINS", inner)


def _load_it_sessions(data_dir: Path) -> list:
    """audit から IT重説セッションの最新状態を session_id ごとに集約（新しい順）。監査リプレイで復元。"""
    _, rows = read_jsonl(data_dir / "audit_log.jsonl")
    latest, order = {}, []
    for ev in rows:
        if ev.get("action") == "it_session" and isinstance(ev.get("it_state"), dict):
            sid = ev.get("target")
            if sid not in latest:
                order.append(sid)
            latest[sid] = ev.get("it_state")
    return [latest[s] for s in reversed(order)]


def _finalized_juusetsu_docs(data_dir: Path) -> list:
    """doc/version/hash/caseが同じ記名確定イベントに束縛された重説だけを返す。"""
    from hub_core import documents as _docs
    from hub_core.operations import _finalized_version_exists
    out = []
    for d in _docs.list_documents(data_dir):
        if (d.get("kind") == "juusetsu" and d.get("case_id")
                and _finalized_version_exists(
                    data_dir, d["doc_id"], int(d.get("latest") or 0),
                    d.get("latest_sha256") or "", case_id=d.get("case_id") or "")):
            out.append(d["doc_id"])
    return out


def render_it(data_dir: Path, params) -> str:
    """IT重説（テレビ会議等）: セッション作成・法定4要件チェック・状態遷移・電磁的交付。
    映像会議は外部(BYO)＝あいのては映像を持たない。実施は運用開始ゲート(§7)＋法定4要件の fail-closed。"""
    from hub_core import it_juusetsu as _it
    from hub_core.auth import load_company as _lc
    from hub_core.operations import (OpError as _OpErr, apply_operation as _apply,
                                     _latest_juusetsu_delivery, _latest_it_conduct)
    v = current_viewer()
    can_gate = bool(v and v.role in ("責任者", "代表"))
    can_check = bool(v and v.role in ("宅建士", "責任者", "代表"))
    style_in = 'font-size:18px;padding:6px 9px;border:1px solid var(--line);border-radius:6px'

    def _p1(key):
        x = params.get(key) if isinstance(params, dict) else None
        return (x[0] if isinstance(x, list) and x else x) if x is not None else None

    note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
            'IT重説は<b>外部の映像会議（Zoom/Meet等）</b>で行い、あいのては映像を持ちません。'
            'この画面は<b>日程・映像会議URL・法定4要件の充足チェック・実施記録・電磁的交付証跡</b>を扱います。'
            '法定4要件が全て揃うまで、また<b>運用開始ゲート</b>（免許/宅建士登録/現行GL確認）が揃うまで、'
            '安全のため実施可能な状態へ進めません。</div>')

    # --- 運用開始ゲート（§7）: 練習モード / 本番モード ---
    gate = _it.operational_gate_status(_lc(data_dir))
    if gate["ready"]:
        gate_panel = ('<div class="ri-gate"><div><div class="gt">本番モード（運用開始ゲート充足）</div>'
                      '<div class="gd">宅建業免許・宅建士登録番号・現行ガイドライン確認がそろっています。'
                      'IT重説の実施が可能です。</div></div>'
                      '<span class="ri-badge ok">本番</span></div>')
    else:
        miss = "・".join(_esc(m) for m in gate["missing"])
        form = ""
        if can_gate:
            company = _lc(data_dir)
            form = ('<form method="post" action="/it/gate/save" '
                    'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">'
                    '<label class="pf-l" style="margin:0">宅建業免許番号</label>'
                    f'<input type="text" name="license_no" value="{_esc(company.get("license_no") or "")}" '
                    f'placeholder="東京都知事(1)第00000号" style="{style_in};min-width:200px">'
                    '<label class="pf-l" style="margin:0">宅建士登録番号</label>'
                    f'<input type="text" name="takkenshi_reg" value="{_esc(company.get("takkenshi_reg") or "")}" '
                    f'placeholder="（東京）第000000号" style="{style_in};min-width:180px">'
                    '<label class="pf-l" style="margin:0;display:flex;gap:5px;align-items:center">'
                    '<input type="checkbox" name="guideline_confirmed" value="on">'
                    '現行ガイドライン（令和6年12月版）を確認した</label>'
                    '<button class="ri-go" type="submit">運用開始ゲートを登録</button></form>')
        else:
            form = '<div class="gn" style="margin-top:8px">登録は責任者/代表のみ行えます。</div>'
        gate_panel = ('<div class="ri-alert" style="background:var(--warn-bg,#fef6e7);color:#92400e;'
                      'border-radius:6px;padding:12px 14px;margin:0 0 14px">'
                      f'<b>練習モード</b>（実施不可）。実施前に次を会社設定へ登録してください: <b>{miss}</b>。'
                      + form + '</div>')

    # --- セッション作成 ---
    create = ('<fieldset class="pf-set"><legend>IT重説セッションを作成</legend>'
              '<form method="post" action="/it/create" '
              'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
              '<label class="pf-l" style="margin:0">お客様・物件</label>'
              + _property_select_html(data_dir, "case_id",
                                      empty_hint="案件がまだありません。")
              + '<label class="pf-l" style="margin:0">日時</label>'
              f'<input type="datetime-local" name="scheduled_at" style="{style_in}">'
              '<label class="pf-l" style="margin:0">映像会議URL</label>'
              f'<input type="url" name="video_url" placeholder="https://zoom.us/j/..." style="{style_in};min-width:220px">'
              '<button class="ri-go" type="submit">セッションを作成</button>'
              '<span class="gn" style="flex-basis:100%">映像会議は外部（Zoom/Meet等）で開催。URLは送信ゲート経由で相手方へ送ります（実送信は人間確認）。</span>'
              '</form></fieldset>')

    # --- 電磁的交付（承諾記録＋交付証跡・新規B） ---
    fin_docs = _finalized_juusetsu_docs(data_dir)
    doc_sel = (('<select name="doc_id" required style="' + style_in + '">'
                + "".join(f'<option value="{_esc(x)}">{_esc(x)}</option>' for x in fin_docs)
                + '</select>') if fin_docs
               else f'<input type="text" name="doc_id" placeholder="JU-..." required style="{style_in}">')
    method_opts = ('<option value="email">電子メール</option>'
                   '<option value="download">ダウンロードURL</option>'
                   '<option value="usb">USBメモリ等</option>')
    fmt_opts = '<option value="pdf">PDF</option><option value="html">HTML</option>'
    delivery = ('<fieldset class="pf-set"><legend>電磁的交付（事前承諾の記録＋交付証跡）</legend>'
                '<div class="gn" style="margin-bottom:8px">'
                '相手方の<b>事前承諾</b>（方法・ファイル形式）を記録し、<b>記名確定済み</b>の重説を交付します。'
                '交付内容と結びついた改ざん検知つきの証跡になり、IT重説の「事前送付」要件を自動で満たします。'
                '交付連絡は送信待ちに追加するだけで、この操作では実送信しません。</div>'
                '<form method="post" action="/it/consent" '
                'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
                '<label class="pf-l" style="margin:0">① 承諾</label>'
                + _property_select_html(data_dir, "case_id",
                                        empty_hint="案件がまだありません。")
                + f'<input type="text" name="recipient" placeholder="相手方（LINE ID等）" required style="{style_in}">'
                + f'<select name="method" required style="{style_in}">{method_opts}</select>'
                + f'<select name="file_format" required style="{style_in}">{fmt_opts}</select>'
                + '<button class="ri-go" type="submit">承諾を記録</button></form>'
                '<form method="post" action="/it/deliver" '
                'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
                '<label class="pf-l" style="margin:0">② 交付</label>'
                + _property_select_html(data_dir, "case_id",
                                        empty_hint="案件がまだありません。")
                + f'<input type="text" name="recipient" placeholder="相手方（LINE ID等）" required style="{style_in}">'
                + '<label class="pf-l" style="margin:0">書類</label>' + doc_sel
                + f'<input type="text" name="version" placeholder="版(空=最新)" style="{style_in};width:120px">'
                + '<button class="ri-go" type="submit">確定版を交付記録へ追加</button></form>'
                '</fieldset>')

    # --- 空き枠エンジン（M4・担当カレンダーの内見/IT重説を単一 free/busy で表示） ---
    sk = _p1("slots_kind") or "IT重説"
    sd = _p1("slots_date") or ""
    kopts = "".join(f'<option value="{_esc(k)}"{" selected" if k == sk else ""}>{_esc(k)}</option>'
                    for k in ("IT重説", "内見"))
    slots_result = ""
    if sd:
        try:
            r = _apply(data_dir, "schedule_slots",
                       {"case_id": _p1("slots_case") or "", "date": sd, "kind": sk}, v.user, v.role)
            chips = ""
            for sl in r["slots"]:
                if sl["available"]:
                    chips += (f'<span class="ri-badge ok" style="margin:2px">{_esc(sl["start"][-5:])} 空き</span>')
                else:
                    chips += (f'<span class="ri-badge warn" style="margin:2px">{_esc(sl["start"][-5:])} {_esc(sl["reason"])}</span>')
            src = "担当予定と外部カレンダー" if r.get("harness_used") else "あいのての担当予定"
            slots_result = (f'<div style="margin-top:8px">{sd}・{_esc(sk)}の空き枠'
                            f'（参照元: {src}）:<div style="margin-top:5px">{chips}</div></div>')
        except _OpErr as exc:
            slots_result = ('<div class="ri-alert err" style="margin-top:8px">'
                            + _esc(_public_exception_message(
                                exc, "空き枠を取得できませんでした。入力内容を確認してください。"))
                            + '</div>')
    slots_panel = ('<fieldset class="pf-set"><legend>空き枠（内見・IT重説の重複回避）</legend>'
                   '<form method="get" action="/it" '
                   'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
                   f'<input type="date" name="slots_date" value="{_esc(sd)}" required style="{style_in}">'
                   f'<select name="slots_kind" style="{style_in}">{kopts}</select>'
                   '<button class="ri-go" type="submit">空き枠を見る</button>'
                   '<span class="gn">担当の内見とIT重説を1つのカレンダーで重複回避。'
                   '外部カレンダー接続時は実際の予定も重ねます（未接続でもあいのて単独で表示します）。</span>'
                   f'</form>{slots_result}</fieldset>')

    # --- セッション一覧 ---
    req2 = _it.IT_REQUIREMENTS[1]
    cards = ""
    for s in _load_it_sessions(data_dir):
        sid = s.get("session_id", "")
        cid = s.get("case_id", "")
        state = s.get("state", "")
        badge = _status_badge(state)
        # 要件2「事前送付」は交付証跡から自動判定（表示も証跡から直接）。他3要件は保存済みの手動チェック。
        delivered = _latest_juusetsu_delivery(data_dir, cid) is not None
        reqs = s.get("requirements") or {}
        req_rows = ""
        for r in _it.IT_REQUIREMENTS:
            is_req2 = (r == req2)
            met = delivered if is_req2 else bool(reqs.get(r, False))
            mark = '<span class="ri-badge ok">充足</span>' if met else '<span class="ri-badge warn">未充足</span>'
            auto = '<span class="gn"> — 交付証跡から自動判定</span>' if is_req2 else ""
            btn = ""
            if not is_req2 and not met and state == "要件確認" and can_check:
                btn = ('<form method="post" action="/it/check" style="display:inline-block;margin-left:8px">'
                       f'<input type="hidden" name="session_id" value="{_esc(sid)}">'
                       f'<input type="hidden" name="requirement" value="{_esc(r)}">'
                       '<input type="hidden" name="met" value="true">'
                       '<button class="ri-go" type="submit" style="padding:3px 9px;font-size:18px">確認済にする</button></form>')
            req_rows += f'<div style="margin:3px 0">{mark} {_esc(r)}{auto}{btn}</div>'
        # 状態遷移ボタン
        trans = ""
        for to in sorted(_it.SESSION_STATES.get(state, set())):
            trans += ('<form method="post" action="/it/advance" style="display:inline-block;margin:6px 6px 0 0">'
                      f'<input type="hidden" name="session_id" value="{_esc(sid)}">'
                      f'<input type="hidden" name="to_state" value="{_esc(to)}">'
                      f'<button class="ri-go" type="submit" style="padding:5px 11px;font-size:18px">{_esc(to)}へ</button></form>')
        vurl = str(s.get("video_url") or "").strip()
        if vurl and urlsplit(vurl).scheme == "https":
            vlink = (f'<div class="cm"><a href="{_esc(vurl)}" target="_blank" rel="noopener">'
                     '映像会議を開く</a></div>')
        elif vurl:
            vlink = '<div class="cm">映像会議URLの形式を確認してください。</div>'
        else:
            vlink = ""
        sched = f'<div class="cm">日時: {_esc(s.get("scheduled_at") or "未定")}</div>'
        trans_html = trans or '<span class="gn">これ以上の遷移はありません。</span>'
        # M5: 日程調整のLINE導線（予約/要件確認の段階のみ・全て送信ゲート=queued）。
        sched_forms = ""
        if state in ("予約", "要件確認"):
            sched_forms = (
                '<div style="margin-top:8px;padding:8px;background:var(--paper2,#f6f8fa);border-radius:6px">'
                '<div class="gn" style="margin-bottom:6px">日程調整（LINEの送信待ちに追加し、この操作では実送信しません）:</div>'
                '<form method="post" action="/it/propose" '
                'style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
                f'<input type="text" name="to_user" placeholder="相手方（LINE ID）" required style="{style_in}">'
                f'<input type="text" name="text" placeholder="候補日時（例: 8/1 14:00 / 8/2 10:00）" required style="{style_in};min-width:240px">'
                '<button class="ri-go" type="submit" style="padding:4px 10px;font-size:18px">候補日時をLINEの送信待ちに追加</button></form>'
                '<form method="post" action="/it/schedule" '
                'style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
                f'<input type="hidden" name="session_id" value="{_esc(sid)}">'
                f'<input type="datetime-local" name="scheduled_at" required style="{style_in}">'
                f'<input type="url" name="video_url" placeholder="映像会議URL" style="{style_in};min-width:200px">'
                f'<input type="text" name="to_user" placeholder="相手方（LINE ID・任意）" style="{style_in}">'
                '<button class="ri-go" type="submit" style="padding:4px 10px;font-size:18px">日時とURLを送信待ちに追加</button></form>'
                '</div>')
        # M6: 実施済の実施記録＋37条書面（電子契約）リンク。
        record_html = ""
        if state == "実施済":
            rec = _latest_it_conduct(data_dir, sid) or {}
            record_html = (
                '<div style="margin-top:8px;padding:8px;background:var(--ok-bg,#eef7f1);border-radius:6px">'
                '<div style="font-weight:600;margin-bottom:4px">実施記録</div>'
                f'<div class="cm">宅建士: {_esc(rec.get("takkenshi") or "—")} ／ 日時: {_esc(rec.get("scheduled_at") or "—")}</div>'
                f'<div class="cm">映像URL: {_esc(rec.get("video_url") or "—")}</div>'
                f'<div class="cm">説明した重説: {_esc(_visible_data_value("書類ID", rec.get("doc_id") or "—"))} ／ 改ざん検知つき</div>'
                '<form method="post" action="/it/keiyaku37" '
                'style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px">'
                f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
                f'<input type="text" name="signer" placeholder="契約者名（署名者）" required style="{style_in}">'
                '<button class="ri-go" type="submit" style="padding:4px 10px;font-size:18px">37条書面（電子契約）を作成</button>'
                '<span class="gn">電子契約サービス未接続のため、この画面からは実送信しません。</span></form>'
                '</div>')
        cards += (f'<div class="ri-card" style="margin-bottom:10px">'
                  f'<div class="ct">案件 {_esc(cid)} {badge}</div>'
                  f'{sched}{vlink}'
                  f'<div style="margin:8px 0 4px">法定4要件:</div>{req_rows}'
                  f'<div style="margin-top:8px">{trans_html}</div>'
                  f'{sched_forms}{record_html}'
                  '</div>')
    cards = cards or '<div class="ri-card cm">まだIT重説セッションはありません。上のフォームで作成できます。</div>'

    usage = _usage_strip(
        "起点はお客様との会話です。この画面はIT重説の日程・映像URL・法定4要件・実施記録・電磁的交付を"
        "確認し、証跡を残す場所です。案件はLINEの会話から作れます（案件IDの入力は不要）。",
        "映像会議は外部（Zoom/Meet等）で行い、あいのては映像を持ちません。会社設定の免許・宅建士登録番号などを"
        "運用開始ゲートで確認します。",
        ["LINEの会話から「このお客様とIT重説を始める」で開始する",
         "日時・映像URLを入れ、法定4要件を満たすまで実施可へ進める",
         "電磁的交付の承諾と交付証跡を記録する"],
        [("/line", "LINE"), ("/juusetsu", "重説"), ("/connections", "接続設定")])
    inner = (ui.page_head("IT重説", "テレビ会議での重要事項説明を、法定4要件と運用開始確認に基づいて管理します。")
             + usage + note + gate_panel + create + slots_panel + delivery
             + ui.section("IT重説セッション") + cards)
    return _wrap_main("it", "/it", "IT重説", inner)


def render_property_collect(data_dir: Path, params) -> str:
    """物件に書類を追加して情報を集める専用ページ（フォーム可）。完成度ボード＋アップロードフォーム。"""
    cid = (params.get("case", [""])[0] or params.get("id", [""])[0] or "").strip()
    prop_name = ""
    if cid:
        _, _cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
        for _c in _cases:
            if (_c.get("案件ID") or "").strip() == cid:
                prop_name = (_c.get("物件名") or "").strip()
                break
    board = _case_completion_section(data_dir, cid, prop_name, form_mode=True)
    back = (f'<a class="ri-qbtn" href="/case?id={quote(cid)}">← 物件に戻る</a>') if cid else ""
    visible_cid = _visible_data_value("案件", cid)
    visible_property = _visible_data_value("物件名", prop_name) or visible_cid or "対象物件"
    inner = (ui.page_head("書類から情報を集める",
             "図面・登記・調査報告書・設備表などを1件ずつ読み込むと、1つの物件レコードに合流します。"
             "揃うほどマイソク/重説が埋まり、足りない情報は取得先リンク付きタスクになります。")
             + (f'<div class="conn-guide" style="margin:0 0 14px">対象物件: <b>{_esc(visible_property)}</b> '
                f'（案件 {_esc(visible_cid or "対象")}）</div>' if cid else
                '<div class="conn-guide" style="margin:0 0 14px">物件一覧から対象を選んでください。</div>')
             + board + '<div style="margin-top:14px">' + back + '</div>')
    return _wrap_main("properties", "/properties", "書類から情報を集める", inner)


_CASE_FOUR_DOCUMENT_SLOTS = (
    ("juusetsu35", "重要事項説明書（35条）", "docx", "Word下書きを出力"),
    ("sale_condition_check", "売買条件確認書", "docx", "条件確認票を出力"),
    ("article37", "37条書面", "docx", "37条下書きを出力"),
    ("maisoku", "マイソク", "xlsx", "Excelで出力"),
)


def _case_four_documents_section(data_dir: Path, cid: str) -> str:
    """Render four distinct document slots using metadata only.

    Body bytes are intentionally absent from this path.  Every output link carries
    the exact case/customer/document/version/format tuple consumed only by
    ``/case/doc/file``.  The legacy document route cannot consume these links.
    """
    viewer = current_viewer()
    if viewer is None:
        return ""
    from hub_core.access import authorized_case_binding, list_case_four_document_metadata

    binding = authorized_case_binding(data_dir, viewer, cid)
    if binding is None:
        return ""
    customer_id = str(binding.get("customer_id") or "").strip()
    if not customer_id:
        return (
            '<div class="ri-sech" style="margin-top:22px">4帳票</div>'
            '<div class="ri-card"><div class="ct">顧客との結合を確認できません</div>'
            '<div class="cm">案件台帳に顧客IDを登録するまで、帳票本文と出力は開きません。</div></div>'
        )
    rows = list_case_four_document_metadata(
        data_dir, viewer, case_id=cid, customer_id=customer_id)
    by_kind: dict[str, list[dict]] = {slot[0]: [] for slot in _CASE_FOUR_DOCUMENT_SLOTS}
    for row in rows:
        canonical = str(row.get("canonical_kind") or "")
        if canonical in by_kind:
            by_kind[canonical].append(row)
    cards = []
    for canonical, label, fmt, action_label in _CASE_FOUR_DOCUMENT_SLOTS:
        matches = sorted(
            by_kind[canonical],
            key=lambda row: (int(row.get("version") or 0), str(row.get("doc_id") or "")),
            reverse=True,
        )
        if not matches:
            cards.append(
                '<div class="ri-card">'
                f'<div class="ct">{_esc(label)} <span class="ri-badge warn">未作成</span></div>'
                '<div class="cm">この案件・顧客に束縛された保存版はありません。</div></div>'
            )
            continue
        row = matches[0]
        doc_id = str(row.get("doc_id") or "")
        version = int(row.get("version") or 0)
        query = (
            f"doc={quote(doc_id)}&amp;v={version}&amp;case={quote(cid)}"
            f"&amp;customer={quote(customer_id)}&amp;as={quote(fmt)}"
        )
        lifecycle = {
            "juusetsu35": "35条・下書き。確定版の交付可否は出力時に別途再検査します。",
            "sale_condition_check": "売買契約書ではない条件確認票です。契約締結には使えません。",
            "article37": "37条書面の下書きです。契約成立・交付先・宅建士確認は別途必要です。",
            "maisoku": (
                "必要表示事項と広告表現の機械検査を出力時に再実行します。"
                "写真の権利と最終公開判断は別途確認が必要です。"
            ),
        }[canonical]
        duplicate_note = (
            f" / 同種{len(matches)}件のうち最新" if len(matches) > 1 else ""
        )
        cards.append(
            '<div class="ri-card">'
            f'<div class="ct">{_esc(label)} <span class="ri-badge ok">保存済み</span></div>'
            f'<div class="cm">{_esc(doc_id)} / v{version}{_esc(duplicate_note)}</div>'
            f'<div class="cm">{_esc(lifecycle)}</div>'
            f'<div class="ca"><a class="ri-go" href="/case/doc/file?{query}">{_esc(action_label)}</a></div>'
            '</div>'
        )
    return (
        '<div class="ri-sech" style="margin-top:22px">この案件の4帳票</div>'
        f'<div class="gn" style="margin:-4px 0 10px">案件 {_esc(cid)} / 顧客 {_esc(customer_id)}。'
        '本文を読む前に、この結合と担当者を照合します。</div>'
        '<div class="ri-grid2">' + "".join(cards) + '</div>'
    )


def render_case(data_dir: Path, params) -> str:
    cid = (params.get("id", [""])[0] or "").strip()
    if not cid:
        body = ('<div class="ri-main"><div class="ri-sech">案件串刺し</div>'
                '<div class="ri-card">物件一覧から対象の案件を指定してください。</div></div>')
        return _ri_shell("/case", "案件", '<div class="ri-ws">' + _ri_nav("properties") + '<main class="ri-main">' + body + '</main></div>')

    tasks = _case_request_tasks(data_dir, cid)
    ready = [t for t in tasks if (t.get("status") or "") == "ready"]
    waiting = [t for t in tasks if (t.get("status") or "") != "ready"]

    # --- 今 / 次の一手（プレーン言語・IT弱者向け） ---
    if tasks:
        now = (f'この案件で取得が必要な資料は <b>{len(tasks)}件</b>'
               f'（いま取れる {len(ready)}件 ／ 確認待ち {len(waiting)}件）。')
        nxt = ('① 下の「取得する資料」の黒いボタンを押すと、その書類を取れる公式サイトが新しいタブで開きます。'
               '→ ② 取得できたら、その資料を使って重説づくりに進みます。')
    else:
        now = 'この案件には取得タスクがまだ登録されていません。'
        nxt = '物件情報を登録すると、必要な資料（登記・都市計画・ハザード等）の取得リストが自動で出ます。'
    guide = ('<div class="ri-guide"><div class="gh">いまの状況</div>'
             f'<div class="gb">{now}</div>'
             '<div class="gh" style="margin-top:12px">次の一手</div>'
             f'<div class="gb">{nxt}</div>'
             '<div class="gn">専門用語は出していません。困ったら画面のAIに「次なにする？」と聞いてください。</div></div>')

    # --- 取得する資料（外部URLは /go 経由・本文には内部リンクのみ） ---
    cards = []
    for t in tasks:
        name = _esc(t.get("doc_name") or t.get("doc_id") or "資料")
        service = _esc(t.get("service") or "")
        badge = ('<span class="ri-badge ok">取得できる</span>' if (t.get("status") or "") == "ready"
                 else '<span class="ri-badge warn">確認待ち</span>')
        doc_id = t.get("doc_id") or ""
        url = (t.get("source_url") or "").strip()
        if url and _is_allowed_acquisition_url(url):
            action = (f'<a class="ri-go" href="/go?doc={quote(doc_id)}&amp;case={quote(cid)}" '
                      f'target="_blank" rel="noopener">{service or "公式サイト"}で取得 →</a>')
        else:
            mq = _map_query(t)
            action = (f'<a class="ri-go alt" href="/map?q={quote(mq)}" target="_blank" rel="noopener">'
                      f'{_esc(_office_label(t))}を地図で探す →</a>'
                      f'<span class="ri-where">{_esc(service) or "窓口 / 郵送で取得"}</span>')
        meta = []
        if t.get("category"): meta.append("なぜ必要: " + _esc(t.get("category")))
        if t.get("gate"): meta.append("確認: " + _esc(t.get("gate")))
        if t.get("fee_note"): meta.append(_esc(t.get("fee_note")))
        cards.append('<div class="ri-card">'
                     f'<div class="ct">{name} {badge}</div>'
                     f'<div class="cm">{" ／ ".join(meta)}</div>'
                     f'<div class="ca">{action}</div></div>')
    task_section = ('<div class="ri-sech" style="margin-top:8px">取得する資料（クリックで公式サイトへ）</div>'
                    + ("".join(cards) if cards else '<div class="ri-card cm">取得タスクはありません。</div>'))

    # --- この案件に紐づくデータ（串刺し集約・読み取り専用） ---
    agg = []

    def add_section(title, source):
        headers, rows = _load_rows_for_ui(data_dir, source)
        hit = [r for r in rows if _row_refs_id(r, cid)]
        if not hit:
            return
        cols = headers or list(hit[0].keys())
        th = "".join(f"<th>{_esc(h)}</th>" for h in cols)
        trs = ""
        for r in hit[:20]:
            trs += "<tr>" + "".join(f"<td>{render_cell(h, r.get(h, ''), '')}</td>" for h in cols) + "</tr>"
        agg.append(f'<div class="cw-sec"><div class="cw-h">{_esc(title)} ({len(hit)})</div>'
                   f'<div class="cw-tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div></div>')

    add_section("タスク", "tasks")
    add_section("案件・物件", "csv:cases.csv")
    add_section("Hold", "csv:hold_queue.csv")
    add_section("承認", "csv:approval_queue.csv")
    add_section("顧客", "csv:customers.csv")
    add_section("監査証跡", "jsonl:audit_log.jsonl")
    agg_html = ('<div class="ri-sech" style="margin-top:28px">この案件に紐づくデータ（読み取り専用）</div>'
                '<div class="cw-wrap">' + "".join(agg) + "</div>") if agg else ""

    prop_name = ((tasks[0].get("property_name") if tasks else "") or "").strip()
    if not prop_name:
        # 取得タスクが無い案件は cases 台帳から物件名を解決（Vault抽出・PRSブロックの紐付けに使う）
        _, _cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
        for _c in _cases:
            if (_c.get("案件ID") or "").strip() == cid:
                prop_name = (_c.get("物件名") or "").strip()
                break
    prop_map = (f' <a class="ri-go alt" href="/map?q={quote(prop_name)}" target="_blank" rel="noopener" '
                f'style="font-size:18px;padding:6px 12px">物件の場所を地図で見る →</a>') if prop_name else ""
    body = ('<div class="ri-ws">' + _ri_nav("properties") + '<main class="ri-main">'
            + f'<div class="ri-sech">案件串刺し: <b>{_esc(_visible_data_value("案件ID", cid))}</b>{prop_map}</div>'
            + _case_stage_section(data_dir, cid)
            + _case_prs_block(data_dir, cid, params, prop_name)
            + _case_screening_section(data_dir, cid)
            + _case_extract_section(data_dir, cid, prop_name)
            + _case_completion_section(data_dir, cid, prop_name)
            + _case_four_documents_section(data_dir, cid)
            + guide + task_section + agg_html + '</main></div>')
    return _ri_shell("/case", f"案件 {_visible_data_value('案件ID', cid)}", body)


def render_not_found(data_dir: Path, path: str) -> str:
    body = (f'<h2 class="page">404</h2>'
            '<div class="empty">指定された画面は見つかりません。</div>')
    return render_page(data_dir, "", "404", body)


# ---------------------------------------------------------------------------
# AGENT MODE — あいのて内のAI/人が行った操作を確認・承認する画面
# ---------------------------------------------------------------------------
# 操作系イベント(=人/AIが意図して行った操作)。バルクのデータ取込は別枠で件数のみ。
_AGENT_ACTIONS = {
    "case_stage_advanced": ("案件を次段階へ進めた", "case"),
    "case_lost": ("案件を失注として記録した", "case"),
    "case_finalized": ("案件を確定した", "case"),
    "task_completed": ("タスクを完了にした", "today"),
    "approval_decided": ("承認待ちを決定した", "approval"),
    "hold_released": ("保留を解除した", "hold"),
    "finalized_with_signature": ("記名確定した", "audit"),
    "finalized_juusetsu": ("重説を記名確定した", "juusetsu"),
}


def _agent_actor_kind(actor: str):
    a = (actor or "").strip().lower()
    if a in ("ri-hub-mcp", "mcp", "agent", "claude"):
        return ("AI", "AI(Claude)")
    if a in ("ri-hub", "system", "operations"):
        return ("SYS", "システム")
    return ("人", "人")


def _agent_event_link(action: str, target: str) -> str:
    dest = _AGENT_ACTIONS.get(action, (None, "audit"))[1]
    t = (target or "").strip()
    if dest == "case" and t:
        return "/case?id=" + quote(t)
    return {"today": "/today", "approval": "/approval", "hold": "/hold",
            "juusetsu": "/juusetsu", "audit": "/audit"}.get(dest, "/audit")


def _pick(row: dict, *keys):
    """表示行は query_page により英語列→日本語ラベルへ再キー化される。
    日本語・英語どちらのキーでも値を取れるようにする(空id→壊れたボタンを防ぐ)。"""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return ""


def _op_button(op: str, fields: dict, label: str, viewer, *, name: str = "", value: str = "") -> str:
    """権限のある viewer にだけ操作ボタンを出す。無ければ空。POSTは統一 /op へ。
    fail-closed: 対象id(fields の値)が空なら操作不能なのでボタンを出さない。"""
    from hub_core.operations import OP_ROLES
    if not (viewer and viewer.role in OP_ROLES.get(op, set())):
        return ""
    if any((v is None or str(v).strip() == "") for v in fields.values()):
        return ""
    hidden = f'<input type="hidden" name="op" value="{_esc(op)}">'
    for k, v in fields.items():
        hidden += f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
    # 否定的操作(却下/rejected)は中立スタイル。承認/解除など肯定操作は緑のまま。
    is_reject = str(value).lower() in ("rejected", "reject", "却下") or label.strip() in ("却下", "拒否")
    bcls = "ri-go ghost" if is_reject else "ri-go"
    btn = (f'<button class="{bcls}" type="submit" name="{_esc(name)}" value="{_esc(value)}">{_esc(label)}</button>'
           if name else f'<button class="{bcls}" type="submit">{_esc(label)}</button>')
    return f'<form method="post" action="/op" style="display:inline-block;margin:6px 6px 0 0">{hidden}{btn}</form>'


def render_agent_mode(data_dir: Path, params) -> str:
    v = current_viewer()
    role = v.role if v else "ゲスト"
    _, approvals = _load_rows_for_ui(data_dir, "csv:approval_queue.csv")
    _, holds = _load_rows_for_ui(data_dir, "csv:hold_queue.csv")
    _, audit = _load_rows_for_ui(data_dir, "jsonl:audit_log.jsonl")
    can_appr = bool(v) and v.role in {"責任者", "代表"}
    can_hold = bool(v) and v.role in {"責任者", "代表"}

    # --- 要確認キュー: 承認待ち(判断/decision=pending) ---
    pend_appr = [r for r in approvals
                 if str(_pick(r, "判断", "decision") or "pending").strip().lower() in ("", "pending")]
    appr_cards = []
    for r in pend_appr[:8]:
        aid = _pick(r, "承認ID", "approval_id")
        who = " / ".join(_visible_data_value("承認対象", x) for x in (
            _pick(r, "ポータル", "platform"), _pick(r, "顧客名", "customer_name"),
            _pick(r, "承認役割", "approval_role")) if x)
        ctrls = (_op_button("approval_decide", {"approval_id": aid}, "承認", v, name="decision", value="approved")
                 + _op_button("approval_decide", {"approval_id": aid}, "却下", v, name="decision", value="rejected")) \
            if can_appr else '<div class="gn" style="margin-top:6px">承認権限がありません（責任者/代表）。</div>'
        appr_cards.append(
            f'<div class="ri-card"><div class="ct">{_esc(who or aid)} '
            '<span class="ri-badge warn">承認待ち</span></div>'
            f'<div class="cm">{_esc(_visible_data_value("理由", _pick(r, "理由", "reason")))}　'
            f'<span style="color:var(--muted2)">{_esc(_visible_data_value("承認ID", aid))}</span></div>'
            f'{ctrls}</div>')
    if len(pend_appr) > 8:
        appr_cards.append(f'<a class="ri-card" href="/approval"><div class="ct">ほか {len(pend_appr) - 8} 件の承認待ち →</div></a>')
    if not appr_cards:
        appr_cards.append('<div class="ri-card"><div class="ct">承認待ちはありません</div></div>')

    # --- 要確認キュー: 保留(ゲート/gate != cleared) ---
    active_holds = [r for r in holds if str(_pick(r, "ゲート", "gate")).strip().lower() != "cleared"]
    hold_cards = []
    for r in active_holds[:6]:
        hid = _pick(r, "保留ID", "hold_id")
        who = " / ".join(_visible_data_value("保留対象", x) for x in (
            _pick(r, "ポータル", "platform"), _pick(r, "顧客名", "customer_name"),
            _pick(r, "物件参照", "property_ref")) if x)
        ctrl = _op_button("hold_release", {"hold_id": hid}, "保留を解除", v) if can_hold \
            else '<div class="gn" style="margin-top:6px">解除権限がありません（責任者/代表）。</div>'
        hold_cards.append(
            f'<div class="ri-card"><div class="ct">{_esc(who or hid)} '
            '<span class="ri-badge bad">保留</span></div>'
            f'<div class="cm">{_esc(_visible_data_value("理由", _pick(r, "理由", "reason")))} ／ '
            f'解除条件: {_esc(_visible_data_value("解除条件", _pick(r, "解除条件", "clear_condition")))}</div>'
            f'{ctrl}</div>')
    if len(active_holds) > 6:
        hold_cards.append(f'<a class="ri-card" href="/hold"><div class="ct">ほか {len(active_holds) - 6} 件の保留 →</div></a>')
    if not hold_cards:
        hold_cards.append('<div class="ri-card"><div class="ct">保留はありません</div></div>')

    # --- AIの操作ログ(操作系のみ・最近25件) ---
    op_events = [e for e in reversed(audit) if e.get("action") in _AGENT_ACTIONS]
    ingest_n = sum(1 for e in audit if e.get("action") not in _AGENT_ACTIONS)
    feed = []
    for e in op_events[:25]:
        icon, kind = _agent_actor_kind(e.get("actor"))
        label = _AGENT_ACTIONS[e.get("action")][0]
        link = _agent_event_link(e.get("action"), e.get("target") or e.get("case") or "")
        extra = _visible_data_value("状態", e.get("to_status") or e.get("decision") or "")
        target = _visible_data_value("対象", e.get("target") or e.get("case") or "")
        feed.append(
            f'<a class="ri-task" href="{link}"><span class="ri-tk"></span>'
            f'<span class="tt">{icon} <b>{_esc(kind)}</b> が {_esc(label)}'
            f'{("：" + _esc(extra)) if extra else ""} — {_esc(target)}</span>'
            f'<span class="tm">{_esc(_display_datetime(e.get("timestamp") or ""))}</span></a>')
    if not feed:
        feed.append('<div class="ri-task"><span class="ri-tk"></span>'
                    '<span class="tt muted">まだAI/人による操作はありません。「ことばで頼む」から案件の操作を頼むとここに出ます。</span>'
                    '<span class="tm">—</span></div>')

    body = (
        '<div class="ri-ws">'
        f'{_ri_nav("agent")}'
        '<div class="ri-main">'
        '<div class="hello">AIがした操作を、あなたが確認して承認する画面です。</div>'
        f'<div class="ri-ai-state">閲覧者: <b>{_esc(role)}</b> · 操作は改ざん検知つきで記録されます</div>'
        '<div class="ri-sech">要確認キュー（あなたの承認待ち）</div>'
        f'<div>{"".join(appr_cards)}</div>'
        '<div class="ri-sech">止まっている対象（保留の解除）</div>'
        f'<div>{"".join(hold_cards)}</div>'
        '<div class="ri-sech">AI / 人の操作ログ（最近）</div>'
        f'<div class="ri-tasks">{"".join(feed)}</div>'
        f'<div class="ri-guide" style="margin-top:18px"><div class="gh">AIに操作を頼むには</div>'
        '<div class="gb"><a href="/console">ことばで頼む</a>を開き、日本語で'
        '「この案件を内見に進めて」と頼むと、AIの提案や操作結果がこの画面に反映されます。'
        '承認・保留解除・記名確定はAIが確定せず、権限のある人が画面で確認します。</div></div>'
        '</div></div>')
    return _ri_shell("/agent", "AIの作業を確認", body)


def _inline_md(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def _md_to_html(text: str) -> str:
    """視認性重視の軽量 Markdown→HTML(見出し/箇条書き/表/段落/太字)。依存なし。"""
    out, in_ul, in_tbl = [], False, False

    def close():
        nonlocal in_ul, in_tbl
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_tbl:
            out.append("</table>"); in_tbl = False

    for ln in (text or "").split("\n"):
        s = ln.rstrip()
        if not s.strip():
            close(); continue
        st = s.strip()
        if st.startswith("|") and st.endswith("|"):
            cells = [c.strip() for c in st.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue  # 区切り行
            if not in_tbl:
                close(); out.append('<table class="md-tbl">'); in_tbl = True
            out.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in cells) + "</tr>")
            continue
        if s.startswith("### "):
            close(); out.append(f"<h3>{_inline_md(s[4:])}</h3>"); continue
        if s.startswith("## "):
            close(); out.append(f"<h2>{_inline_md(s[3:])}</h2>"); continue
        if s.startswith("# "):
            close(); out.append(f"<h1>{_inline_md(s[2:])}</h1>"); continue
        if st.startswith(("- ", "* ")):
            if not in_ul:
                close(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline_md(st[2:])}</li>"); continue
        close(); out.append(f"<p>{_inline_md(s)}</p>")
    close()
    return "".join(out)


def _doc_render(meta: dict, body: str) -> str:
    """書類本文を視認性高く表示。md/txt は整形、html はソース、maisoku(json)は様式編集導線。"""
    fmt = (meta.get("fmt") or "md").lower()
    kind = (meta.get("kind") or "").lower()
    if kind == "maisoku":
        did = quote(meta.get("doc_id", ""))
        return ('<div class="rc-cardm">マイソク（販売図面）です。様式フォームで項目を編集できます。</div>'
                f'<div style="margin:10px 0;display:flex;gap:8px;flex-wrap:wrap">'
                f'<a class="rc-btn" href="/maisoku/edit?doc={did}">様式を編集</a>'
                f'<a class="rc-btn ghost" href="/doc/preview?doc={did}" target="_blank" rel="noopener">販売図面を開く</a></div>')
    if fmt == "html":
        return ('<div class="rc-cardm">HTML書類です。整形した見た目は「整形プレビュー（別タブ）」で確認できます。'
                '下はソースです。</div>'
                f'<pre class="rc-diff" style="max-height:46vh">{_esc(body)}</pre>')
    return f'<div class="rc-doc rc-md">{_md_to_html(body)}</div>'


def _img_src(m):
    """<img src> のうち data:/外部URL/絶対URL を src 空へ（Vault相対のみ残す）。"""
    prefix, quote, src = m.group(1), m.group(2), (m.group(3) or "").strip()
    if src.startswith(("data:", "http://", "https://", "//")):
        return f'{prefix}{quote}{quote}'
    return m.group(0)


def _neutralize_external_images(html: str) -> str:
    """保存HTML中の <img> の src が data:/外部URL/絶対URL なら空にする（無断転載防止）。
    Vault相対参照(物件/…)のみ残す。CSS background:url(data:|http) も無力化。"""
    import re as _re
    html = _re.sub(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)(\2)',
                   _img_src, html, flags=_re.IGNORECASE | _re.DOTALL)
    html = _re.sub(r'url\(\s*["\']?\s*(?:data:|https?:|//)[^)]*\)', 'url()',
                   html, flags=_re.IGNORECASE)
    return html


def _juusetsu_parse(body: str) -> dict:
    """重説markdown（render_juusetsu_md出力）→ 帳票化用の構造。捏造しない＝本文の値をそのまま写す。
    返り: {title, note, meta:[(label,value)], sections:[{heading, rows:[(label,value,blank)], notes:[str]}]}."""
    import re
    title, note = "", ""
    meta: list = []
    sections: list = []
    cur = None
    for raw in (body or "").splitlines():
        s = raw.rstrip()
        st = s.strip()
        if not st:
            continue
        if st.startswith("# "):
            title = st[2:].strip()
            continue
        if st.startswith("> "):
            note = (note + " " + st[2:].strip()).strip().replace("**", "")
            continue
        if st.startswith("## "):
            cur = {"heading": st[3:].strip(), "rows": [], "notes": []}
            sections.append(cur)
            continue
        if st.startswith("|") and st.endswith("|") and cur is not None:
            # markdown表形式の重説本文（| ラベル | 値 |）にも対応。区切り行・ヘッダ行は落とす。
            cells = [c.strip().strip("*").strip() for c in st.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue                                   # |---|---| 区切り
            if cells[:2] == ["項目", "値"] or (len(cells) >= 2 and cells[0] in ("項目", "内容") and cells[1] in ("値", "内容")):
                continue                                   # ヘッダ行
            if len(cells) >= 2:
                cur["rows"].append((cells[0], cells[1], not bool(cells[1])))
            elif cells and cells[0]:
                cur["notes"].append(cells[0])
            continue
        if st.startswith("- "):
            item = st[2:].strip()
            if item.startswith("☐"):
                pair = (item[1:].strip(), "", True)          # 未記入
            else:
                # 記入済は `- {項目}：**{値}**`。項目名自体に「：」を含むため、値は末尾の **…** から取り、
                # ラベルはそれ以前の全文（＝項目名を丸ごと使う・混入させない）。
                m = re.match(r"^(.*)：\*\*(.*?)\*\*\s*$", item)
                if m:
                    pair = (m.group(1).strip(), m.group(2).strip(), False)
                else:
                    idx = min([i for i in (item.find("："), item.find(":")) if i >= 0] or [-1])
                    if idx >= 0:
                        val = item[idx + 1:].strip().strip("*").strip()
                        pair = (item[:idx].strip(), val, not bool(val))
                    else:
                        pair = (item, "", False)
            (cur["rows"] if cur else meta).append(pair if cur else (pair[0], pair[1]))
            continue
        # 見出しでも箇条書きでもない地の文（記名節の宣言文など）
        if cur is not None:
            cur["notes"].append(st.replace("**", ""))
    return {"title": title, "note": note, "meta": meta, "sections": sections}


def _juusetsu_form_html(body: str, doc_id: str, also: str = "", *,
                        status: str = "draft", evidence: dict | None = None) -> str:
    """重説を35条書面らしい罫線付き様式（帳票）で表示。印刷=Cmd/Ctrl+PでそのままA4 PDFになる。
    値は本文をそのまま写す（金メッキしない・未記入は☐で残す）。CSP: style-inline可・scriptなし。"""
    d = _juusetsu_parse(body)

    def _row(label, value, blank):
        if blank or not value:
            cell = '<span class="ju-box">☐</span><span class="ju-blank">未記入</span>'
        else:
            cell = f'<b>{_esc(value)}</b>'
        return f'<tr><th>{_esc(label)}</th><td>{cell}</td></tr>'

    secs = ""
    for s in d["sections"]:
        rows = "".join(_row(l, v, b) for (l, v, b) in s["rows"])
        notes = "".join(f'<p class="ju-note">{_esc(n)}</p>' for n in s["notes"])
        tbl = f'<table class="ju-tbl">{rows}</table>' if rows else ""
        secs += (f'<section class="ju-sec"><h2 class="ju-sech">{_esc(s["heading"])}</h2>'
                 f'{notes}{tbl}</section>')
    meta_rows = "".join(f'<tr><th>{_esc(l)}</th><td>{_esc(v)}</td></tr>' for (l, v) in d["meta"])
    meta_tbl = f'<table class="ju-tbl ju-meta">{meta_rows}</table>' if meta_rows else ""
    banner = ""
    if also:
        banner = ('<div class="no-print" style="background:#eef7f1;border:1px solid #cfe6d8;border-radius:8px;'
                  'padding:10px 14px;margin-bottom:14px;font-size:18px">同じ情報で<b>マイソクも作成しました</b>。'
                  '<a href="/doc/preview?doc=' + quote(also) + '">マイソクを見る →</a></div>')
    note_html = f'<p class="ju-caption">{_esc(d["note"])}</p>' if d["note"] else ""
    if status == "final" and evidence:
        status_html = (
            '<div class="ju-status final"><b>確定版（監査照合済み）</b><br>'
            f'{_esc(evidence.get("target") or "")} / 案件 {_esc(evidence.get("case_id") or "")} / '
            f'SHA-256 {_esc(evidence.get("content_sha256") or "")}</div>'
        )
    else:
        status_html = (
            '<div class="ju-status draft"><b>DRAFT / 下書き・交付不可</b><br>'
            '内容確認用です。顧客への交付・説明には使用できません。</div>'
        )
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_esc(d["title"] or doc_id)}</title><style>'
        '@page{size:A4;margin:14mm}'
        '*{box-sizing:border-box}'
        'body{font-family:"Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;'
        'color:#111;background:#f3f4f6;margin:0;padding:24px 12px;line-height:1.6}'
        '.ju-sheet{max-width:820px;margin:0 auto;background:#fff;border:1px solid #333;'
        'padding:30px 34px 40px;box-shadow:0 1px 6px rgba(0,0,0,.08)}'
        '.ju-sheet>h1{text-align:center;font-size:22px;font-weight:700;letter-spacing:.12em;'
        'border-bottom:2px solid #111;padding-bottom:12px;margin:0 0 6px}'
        '.ju-caption{font-size:18px;color:#555;margin:8px 0 16px;line-height:1.5}'
        '.ju-sec{margin:16px 0;break-inside:avoid;page-break-inside:avoid}'
        '.ju-sech{font-size:19px;font-weight:700;background:#1f2937;color:#fff;'
        'padding:6px 12px;margin:0 0 0;border:1px solid #1f2937}'
        '.ju-note{font-size:18px;color:#333;margin:6px 2px;line-height:1.5}'
        '.ju-tbl{border-collapse:collapse;width:100%;margin:0}'
        '.ju-tbl th,.ju-tbl td{border:1px solid #555;padding:7px 10px;font-size:18px;vertical-align:top;text-align:left}'
        '.ju-tbl th{background:#f0f2f5;font-weight:600;width:34%;white-space:normal}'
        '.ju-meta{margin-bottom:14px}.ju-meta th{width:22%}'
        '.ju-box{font-size:19px;margin-right:8px}.ju-blank{color:#999;font-size:18px}'
        '.ju-print{font-size:18px;color:#555;text-align:right;margin:0 auto 10px;max-width:820px}'
        '.ju-status{max-width:820px;margin:0 auto 12px;padding:10px 14px;border:2px solid;'
        'text-align:center;font-family:-apple-system,"Hiragino Kaku Gothic ProN",sans-serif;'
        'font-size:14px;line-height:1.45;overflow-wrap:anywhere}'
        '.ju-status.draft{color:#991b1b;border-color:#b42318;background:#fff1f0}'
        '.ju-status.final{color:#16452a;border-color:#217645;background:#eef7f1}'
        '@media print{body{background:#fff;padding:0}.ju-sheet{border:none;box-shadow:none;max-width:none;padding:0}.no-print{display:none}}'
        '</style></head><body>'
        '<div class="ju-print no-print">印刷・PDF化: Cmd+P（Windowsは Ctrl+P）で A4 の重要事項説明書として出力できます。</div>'
        + status_html
        + '<div class="ju-sheet">'
        + banner
        + f'<h1>{_esc(d["title"] or "重要事項説明書")}</h1>'
        + note_html + meta_tbl + secs
        + '</div></body></html>')


def render_doc_preview(data_dir: Path, params) -> str:
    """書類を単独ページとして整形表示(別タブ用)。html はそのまま・md は整形・txt は素。"""
    from hub_core import documents
    doc_id = (params.get("doc", [""])[0] or "").strip()
    try:
        version = int(params.get("v", [""])[0]) if params.get("v", [""])[0] else None
        cur = documents.get_version(data_dir, doc_id, version)
    except Exception:
        return "<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:40px'>書類を表示できません</body>"
    fmt = (cur["meta"].get("fmt") or "md").lower()
    kind = (cur["meta"].get("kind") or "").lower()
    body = cur["body"]
    if kind == "maisoku":
        # 様式は正本=フィールドjson(fmt=txtに格納)。表示時にテンプレ駆動でHTML化(販売図面)。
        try:
            from hub_core import branding, maisoku
            fields = json.loads(body or "{}")
            # 写真は Vault 素材のみ解決（preview=未許諾/外部URL/貼付画像は空プレースホルダ＝焼けない）
            company = branding.load_snapshot(
                data_dir, str((cur.get("meta") or {}).get("company_profile_hash") or ""))
            if (params.get("publish", [""])[0] or "").strip() == "1":
                return maisoku.render_flyer_publish(
                    fields, variant=fields.get("_variant", "dense-pro"), data_dir=data_dir,
                    prop=fields.get("property", ""), company=company,
                )[0]
            return maisoku.render_flyer(fields, variant=fields.get("_variant", "dense-pro"),
                                        data_dir=data_dir, prop=fields.get("property", ""),
                                        company=company)
        except Exception as exc:
            if getattr(exc, "code", None) in (400, 403, 409):
                raise
            return "<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:40px'>マイソクを表示できません（データ不正）</body>"
    if fmt == "html":
        # 保存済みHTMLの画像は Vault 由来のみ許可＝data:/外部URLの<img>を無力化（無断転載防止・FAIL2是正）。
        return _neutralize_external_images(body)
    if kind == "juusetsu":
        # 重説は35条書面らしい罫線付き様式（帳票）で表示＝そのままA4 PDFにできる（レイアウト是正）。
        publish = (params.get("publish", [""])[0] or "").strip() == "1"
        evidence = None
        if publish:
            evidence = documents.require_finalized_version(
                data_dir, doc_id, int((cur.get("meta") or {}).get("version") or 0),
                require_case=True,
            )
        return _juusetsu_form_html(
            body if publish else _juusetsu_draft_copy(body), doc_id,
            also=(params.get("also", [""])[0] or "").strip(),
            status="final" if publish else "draft", evidence=evidence,
        )
    inner = _md_to_html(body) if fmt not in ("txt", "ics") else f"<pre>{_esc(body)}</pre>"
    also = (params.get("also", [""])[0] or "").strip()
    banner = ""
    if also:
        banner = ('<div style="background:#eef7f1;border:1px solid #cfe6d8;border-radius:8px;padding:12px 16px;'
                  'margin-bottom:20px;font-size:19px">同じ情報で<b>マイソク（販売図面）も作成しました</b>。'
                  '<a href="/doc/preview?doc=' + quote(also) + '" style="color:#217645;font-weight:600">マイソクを見る →</a></div>')
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_esc(doc_id)}</title><style>'
            'body{font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;'
            'max-width:760px;margin:40px auto;padding:0 20px;line-height:1.85;color:#14181d}'
            'h1{font-size:24px}h2{font-size:19px}h3{font-size:20px}'
            'table{border-collapse:collapse;width:100%;margin:12px 0}td{border:1px solid #e4e7ea;padding:7px 10px}'
            'ul{padding-left:1.3em}</style></head><body>' + banner + inner + '</body></html>')


def _console_right(data_dir: Path, viewer) -> str:
    """右ペイン: 要確認キュー(承認/保留)。_op_button を使う。"""
    _, approvals = _load_rows_for_ui(data_dir, "csv:approval_queue.csv")
    _, holds = _load_rows_for_ui(data_dir, "csv:hold_queue.csv")
    can = bool(viewer) and viewer.role in {"責任者", "代表"}

    def card(who, reason, badge_cls, badge_text, ctrls):
        return (f'<div class="rc-card"><div class="rc-cardh">{_esc(who)} '
                f'<span class="badge {badge_cls}">{badge_text}</span></div>'
                f'<div class="rc-cardm">{_esc(reason)}</div>{ctrls}</div>')

    pend = [r for r in approvals if str(_pick(r, "判断", "decision") or "pending").strip().lower() in ("", "pending")]
    # 権限が無い場合の通知はカード毎に反復しない（視覚ノイズ＝GATE-PV指摘）。冒頭に1回だけ。
    perm_note = ("" if can else
                 '<div class="gn" style="margin:0 0 10px">承認・解除の実行は 責任者/代表 のみ。ここでは内容の確認ができます。</div>')
    appr = []
    for r in pend[:3]:
        aid = _pick(r, "承認ID", "approval_id")
        ctrls = (_op_button("approval_decide", {"approval_id": aid}, "承認", viewer, name="decision", value="approved")
                 + _op_button("approval_decide", {"approval_id": aid}, "却下", viewer, name="decision", value="rejected")) \
            if can else ""
        appr.append(card(_pick(r, "顧客名", "customer_name") or aid, _pick(r, "理由", "reason"), "", "承認待ち", ctrls))
    appr_more = (f'<a class="chip" href="/approval">承認待ちを全件見る（{len(pend)}件）→</a>'
                 if len(pend) > 3 else "")
    active = [r for r in holds if str(_pick(r, "ゲート", "gate")).strip().lower() != "cleared"]
    hold = []
    for r in active[:3]:
        hid = _pick(r, "保留ID", "hold_id")
        ctrl = _op_button("hold_release", {"hold_id": hid}, "保留を解除", viewer) if can else ""
        hold.append(card(_pick(r, "顧客名", "customer_name") or hid, _pick(r, "理由", "reason"), "bad", "保留", ctrl))
    hold_more = (f'<a class="chip" href="/hold">保留を全件見る（{len(active)}件）→</a>'
                 if len(active) > 3 else "")
    return (f'<div class="rc-sech" style="margin-top:0">確認・承認（{len(pend)}件待ち）</div>'
            f'{perm_note}'
            f'{"".join(appr) or "<div class=rc-empty>なし</div>"}{appr_more}'
            f'<div class="rc-sech">止まっている対象（{len(active)}）</div>'
            f'{"".join(hold) or "<div class=rc-empty>なし</div>"}{hold_more}'
            '<div style="margin-top:14px"><a class="chip" href="/agent">AIの作業を確認（全体）→</a></div>')


def _console_center(data_dir: Path, params) -> str:
    """中央ペイン(主役): 書類ワークスペース。一覧→開く→閲覧/編集/差分。"""
    from hub_core import documents
    doc_id = (params.get("doc", [""])[0] or "").strip()
    if not doc_id:
        from hub_core.access import document_summary_access_allowed
        viewer = current_viewer()
        items = [item for item in documents.list_documents(data_dir)
                 if viewer is None or document_summary_access_allowed(data_dir, viewer, item)]
        head = ('<h1 class="rc-h">書類ワークスペース</h1>'
                '<div class="rc-lead">会話で「○○のマイソク下書きを作って」「重説のドラフトを作って」と頼むと、ここに版が並びます。'
                '開いて編集→宅建士が記名確定（右で承認）。</div>')
        if not items:
            return head + ('<div class="rc-card"><div class="rc-cardh">まだ書類はありません</div>'
                           '<div class="rc-cardm">左の会話からドラフトを作成してください。</div></div>')
        cards = "".join(
            f'<a class="rc-doccard" href="/console?doc={quote(d["doc_id"])}">'
            f'<div class="rc-doctitle">{_esc(_document_display_title(d["doc_id"]))}</div>'
            f'<div class="rc-cardm">{_esc(_doc_kind_label(d.get("kind", "")))} · '
            f'第{d["latest"]}版 · {_esc(_display_datetime(d.get("updated", "")))}</div></a>'
            for d in items)
        return head + f'<div class="rc-docgrid">{cards}</div>'
    try:
        v_param = params.get("v", [""])[0]
        version = int(v_param) if v_param else None
        from hub_core.access import document_access_allowed
        viewer = current_viewer()
        if viewer is not None and not document_access_allowed(
                data_dir, viewer, doc_id, version):
            raise documents.DocError(404, "書類が見つかりません。")
        cur = documents.get_version(data_dir, doc_id, version)
    except Exception:
        return (f'<h1 class="rc-h">{_esc(_document_display_title(doc_id))}</h1>'
                '<div class="rc-card rc-cardm">表示できません</div>')
    meta = cur["meta"]
    latest = documents.latest_version(data_dir, doc_id)
    edit = (params.get("edit", [""])[0] == "1")
    saved = (params.get("saved", [""])[0] == "1")
    finalized = (params.get("finalized", [""])[0] == "1")
    fin_err_raw = (params.get("fin_err", [""])[0] or "").strip()
    fin_err = _public_notice_param(fin_err_raw, "記名確定できませんでした。入力内容と権限を確認してください。") if fin_err_raw else ""
    vsel = " ".join(
        (f'<b class="chip on">v{n}</b>' if n == meta["version"] else f'<a class="chip" href="/console?doc={quote(doc_id)}&v={n}">v{n}</a>')
        for n in range(1, latest + 1))
    head = (f'<h1 class="rc-h">{_esc(_document_display_title(doc_id))}</h1>'
            f'<div class="rc-lead">{_esc(_doc_kind_label(meta.get("kind", "")))} · '
            f'第{meta["version"]}版（全{latest}版） · 改ざん検知つき</div>'
            f'<div style="margin:10px 0">{vsel}</div>')
    toast = ""
    if saved:
        toast = '<div class="rc-toast">✓ 新しい版を保存しました</div>'
    elif finalized:
        toast = '<div class="rc-toast">記名して確定しました（改ざん検知つきで記録）</div>'
    elif fin_err:
        toast = f'<div class="rc-toast" style="background:var(--bad-bg);color:var(--bad)">記名確定できません: {_esc(fin_err)}</div>'
    completion_html = ""
    if (meta.get("kind") or "").lower() == "juusetsu":
        try:
            from hub_core import deal_taxonomy as _tax
            completion = _tax.schema_completion(cur["body"])
            missing = list(completion.get("missing") or [])
            if missing:
                first = " / ".join(str(item)[:180] for item in missing[:3])
                completion_html = (
                    '<div class="rc-card" style="border-left:4px solid var(--bad)">'
                    f'<div class="rc-cardh">記名確定前: {len(missing)}件未充足</div>'
                    f'<div class="rc-cardm">先頭の未充足項目: {_esc(first)}</div>'
                    '<div class="rc-cardm"><b>編集して、各項目へ確認済みの値、または明示的な非該当理由を記入してください。</b>'
                    '裸の「非該当」「なし」だけでは確定できません。</div>'
                    f'<a class="rc-btn" href="/console?doc={quote(doc_id)}&amp;v={meta["version"]}&amp;edit=1">未充足項目を編集</a>'
                    '</div>')
            else:
                completion_html = (
                    '<div class="rc-card" style="border-left:4px solid var(--ok)">'
                    '<div class="rc-cardh">法定schemaの全適用項目に値または非該当理由があります</div>'
                    '<div class="rc-cardm">記名確定時に未解決チェックと署名を再検査します。</div></div>')
        except Exception:
            completion_html = (
                '<div class="rc-card" style="border-left:4px solid var(--bad)">'
                '<div class="rc-cardh">法定schemaを確認できません</div>'
                '<div class="rc-cardm">この版は記名確定できません。最新の正式様式で作り直してください。</div></div>')
    if edit:
        body = ('<form method="post" action="/doc/save">'
                f'<input type="hidden" name="doc_id" value="{_esc(doc_id)}">'
                f'<input type="hidden" name="kind" value="{_esc(meta.get("kind",""))}">'
                f'<input type="hidden" name="fmt" value="{_esc(meta.get("fmt","md"))}">'
                f'<textarea class="rc-edit" name="body">{_esc(cur["body"])}</textarea>'
                '<div class="rc-actions">'
                '<button class="rc-btn" type="submit">保存（新しい版）</button>'
                f'<a class="rc-btn ghost" href="/console?doc={quote(doc_id)}">キャンセル</a></div></form>')
        return head + completion_html + body
    pv = f'/doc/preview?doc={quote(doc_id)}&v={meta["version"]}'
    actions = ('<div class="rc-actions">'
               f'<a class="rc-btn" href="/console?doc={quote(doc_id)}&edit=1">✎ 編集</a>'
               f'<a class="rc-btn ghost" href="{pv}" target="_blank" rel="noopener">整形プレビュー（別タブ）</a>'
               '<a class="rc-btn ghost" href="/console">← 一覧</a></div>')
    diff_html = ""
    if meta["version"] > 1:
        try:
            d = documents.diff(data_dir, doc_id, meta["version"] - 1, meta["version"])
            if d.strip():
                diff_html = f'<div class="rc-sech">前版との差分</div><pre class="rc-diff">{_esc(d)}</pre>'
        except Exception:
            pass
    # 記名確定はログイン中の宅建士本人だけ。この版の本文hashを署名する。
    v = current_viewer()
    if v and v.role == "宅建士":
        signer_name, signer_reg = _signer_form_defaults(data_dir, v)
        fin = ('<div class="rc-sech">記名確定（この版に署名）</div>'
               '<form method="post" action="/doc/finalize" class="rc-finform">'
               f'<input type="hidden" name="doc_id" value="{_esc(doc_id)}">'
               f'<input type="hidden" name="version" value="{meta["version"]}">'
               f'<input type="text" name="takkenshi_name" value="{_esc(signer_name)}" placeholder="宅建士名" required>'
               f'<input type="text" name="license_no" value="{_esc(signer_reg)}" placeholder="宅建士の登録番号" required>'
               '<button class="rc-btn" type="submit">記名確定</button></form>')
    else:
        fin = '<div class="rc-sech">記名確定</div><div class="rc-cardm">記名確定は本人確認済みの宅地建物取引士だけが実行できます。</div>'
    return head + toast + completion_html + actions + _doc_render(meta, cur["body"]) + diff_html + fin



_CONSOLE_JS = r"""
const HIST=(window.BOOTHIST||[]).slice();
let THREAD=window.THREAD||'';
function add(who,txt){var l=document.getElementById('cv');var d=document.createElement('div');d.style.margin='10px 0';
 var h=document.createElement('b');h.textContent=who;d.appendChild(h);d.appendChild(document.createElement('br'));
 var p=document.createElement('div');p.style.whiteSpace='pre-wrap';p.textContent=txt;d.appendChild(p);
 l.appendChild(d);l.scrollTop=l.scrollHeight;return p;}
function setbudget(e){var cb=document.getElementById('costbar');if(!cb||typeof e.cost_jpy==='undefined')return;var b=e.budget||{};
 cb.textContent='このターン ¥'+e.cost_jpy+(b.daily_jpy?(' / 本日 ¥'+Math.round(b.spent_jpy)+' / 上限 ¥'+b.daily_jpy):' (ローカル=無料)');}
async function send(ev){if(ev)ev.preventDefault();var i=document.getElementById('ci');var m=i.value.trim();if(!m)return false;i.value='';
 add('あなた',m);var bubble=add('あいのて','考え中…');
 try{
  var resp=await fetch('/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,history:HIST,thread:THREAD})});
  if(!resp.ok){bubble.textContent='エラー HTTP '+resp.status;return false;}
  var reader=resp.body.getReader();var dec=new TextDecoder();var buf='';
  while(true){var rd=await reader.read();if(rd.done)break;buf+=dec.decode(rd.value,{stream:true});var idx;
   while((idx=buf.indexOf('\n\n'))>=0){var chunk=buf.slice(0,idx);buf=buf.slice(idx+2);if(chunk.indexOf('data:')!==0)continue;
    var e;try{e=JSON.parse(chunk.slice(5).trim());}catch(_){continue;}
    if(e.type==='tool'){bubble.textContent='実行中: '+e.summary+' …';}
    else if(e.type==='final'){bubble.textContent=e.reply||'(応答なし)';
     HIST.push({role:'user',content:m});HIST.push({role:'assistant',content:e.reply||''});setbudget(e);
     if(e.thread_id){var fresh=!THREAD;THREAD=e.thread_id;
      if(history.replaceState)history.replaceState(null,'','/console?thread='+encodeURIComponent(THREAD));}
     if(e.pending_confirmations&&e.pending_confirmations.length){add('確認待ち','右の承認パネルで人間が確定してください（AIは確定しません）。');}
     if(e.tool_events&&e.tool_events.some(function(t){return t.tool==='operate'||t.tool==='save_document';})){refreshPanels();}
    }}}
 }catch(err){bubble.textContent='通信エラー: '+err;}
 return false;}
async function refreshPanels(){try{var r=await fetch(location.href);var t=await r.text();
 var doc=new DOMParser().parseFromString(t,'text/html');
 var c=doc.querySelector('.rc-center');var rt=doc.querySelector('.rc-right');
 if(c)document.querySelector('.rc-center').innerHTML=c.innerHTML;
 if(rt)document.querySelector('.rc-right').innerHTML=rt.innerHTML;}catch(e){}}
document.addEventListener('submit',async function(ev){var f=ev.target;
 if(!f||!f.getAttribute||f.getAttribute('action')!=='/op')return;
 ev.preventDefault();var fd=new FormData(f);var body={};fd.forEach(function(v,k){body[k]=v;});
 if(ev.submitter&&ev.submitter.name)body[ev.submitter.name]=ev.submitter.value;
 try{await fetch('/api/op',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){}
 refreshPanels();});
"""


def _log_chat(data_dir: Path, viewer, message: str, res: dict) -> None:
    """会話の使用ログ(append-only・PII redact)。法的監査(HMAC)とは別の利用記録。
    本文はredactし、ツールは名前と結果statusのみ(引数=PIIを残さない)。"""
    from hub_core.pii import redact_text
    try:
        rec = {
            "timestamp": now_jst_iso(),
            "actor": getattr(viewer, "user", "?"),
            "role": getattr(viewer, "role", None),
            "provider": res.get("provider"),
            "message_redacted": redact_text(str(message))[:500],
            "tools": [{"tool": e.get("tool"), "status": (e.get("result") or {}).get("status")}
                      for e in (res.get("tool_events") or [])],
            "pending": len(res.get("pending_confirmations") or []),
        }
        with open(Path(data_dir) / "chat_sessions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # ログ失敗で会話を妨げない(best-effort)


def _console_ai_note(data_dir: Path) -> str:
    """現在のAIモードと、外部送信/予算の正直表示。"""
    from hub_core import chat_llm
    cfg = chat_llm.load_mode_config(data_dir)
    configured = str(cfg.get("provider") or "").strip().lower()
    configured_base = str(cfg.get("base_url") or "").strip()
    if (configured not in ("", "openai", "anthropic")
            or (configured == "openai" and configured_base
                and not chat_llm._is_local_host(configured_base))):
        return '旧AI設定は一般配布版では未対応のため停止中 · AI設定で対応モードを選び直してください'
    try:
        prov = chat_llm.build_provider(data_dir)
    except Exception:
        prov = None
    if prov is None:
        return 'AIは使わない設定 · AI設定からローカルOllamaまたはAnthropicを選べます'
    if getattr(prov, "is_external", True):
        bs = chat_llm.budget_status(data_dir)
        return (f'外部AI({_esc(prov.name)}/{_esc(prov.model)})に送信 · '
                f'電話/メールは伏字・<b>氏名/住所は送信されます</b> · '
                f'本日残 ¥{int(bs["remaining_jpy"])}/¥{int(bs["daily_jpy"])}')
    return f'ローカルAI({_esc(prov.model)}) · データは外に出ません · ¥0'


_CONSOLE_NAV = [("/home", "", "ホーム"), ("/console", "", "ことばで頼む"), ("/agent", "", "AIの作業を確認"),
                ("/properties", "", "物件"), ("/juusetsu", "", "重説"), ("/ads", "", "マイソク"),
                ("/leads", "", "顧客"), ("/audit", "", "台帳")]


def _js_json(obj) -> str:
    """<script> 内に安全に埋め込めるJSON。`</script>`/`<!--`/U+2028/9 によるスクリプト脱出を封じる。"""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029"))


def _console_threads_html(threads, active: str) -> str:
    items = "".join(
        f'<a class="rc-thread {"on" if t["thread_id"] == active else ""}" '
        f'href="/console?thread={quote(t["thread_id"])}">'
        f'<div class="rc-tt">{_esc(t["title"])}</div>'
        f'<div class="rc-tm">{_esc((t.get("updated") or "")[5:16])} · {_esc(t["turns"])}往復</div></a>'
        for t in threads[:25])
    body = items or '<div class="rc-empty">まだ会話履歴はありません。下で話しかけると保存されます。</div>'
    return ('<div class="rc-threads"><div class="rc-threads-h">会話履歴'
            '<a class="rc-new" href="/console">＋ 新しい会話</a></div>'
            f'<div class="rc-thread-list">{body}</div></div>')


def _console_cv_initial(turns) -> str:
    """読み込んだスレッドの会話を #cv の初期表示として描画(リロードしても会話が残る)。"""
    if not turns:
        return ('<div class="ph">例:「今日の承認待ちを見せて」「中野の物件のマイソク下書きを作って」<br><br>'
                'AIが台帳を読み・下書きを作り、状態を進めます。承認・記名確定など重要操作は'
                '<b>人間が右で確定</b>します。この会話は自動保存され、左の履歴から開けます。</div>')
    out = []
    for t in turns:
        if t.get("role") not in ("user", "assistant"):
            continue
        who = "あなた" if t["role"] == "user" else "あいのて"
        out.append(f'<div class="turn"><b>{_esc(who)}</b>'
                   f'<div class="tx">{_esc(t.get("content", ""))}</div></div>')
    return "".join(out)


def render_console(data_dir: Path, params) -> str:
    """会話ファースト3ペイン(会話 | 書類 | 承認)を統一シェル内に配置。会話は /chat/stream(SSE)で実動し、
    スレッド単位でサーバ保存(リロードで復元・左の履歴から過去スレを開ける)。"""
    from hub_core import chat_history
    v = current_viewer()
    vu = v.user if v else None  # 所有者スコープ(IDOR防止): 自分のスレッドだけ
    threads = chat_history.list_threads(data_dir, owner=vu)
    active = (params.get("thread", [""])[0] or "").strip()
    if active in ("", "new"):
        active = ""  # 新規(初回送信時にスレッド採番)
    turns = chat_history.load_thread(data_dir, active, owner=vu) if active else []
    boot_hist = [{"role": t["role"], "content": t.get("content", "")}
                 for t in turns if t.get("role") in ("user", "assistant")]

    chat = (
        '<aside class="rc-left">'
        f'{_console_threads_html(threads, active)}'
        '<div class="rc-chat"><div class="rc-chat-h">会話 — AIに頼む</div>'
        f'<div class="rc-mode">{_console_ai_note(data_dir)}</div>'
        f'<div id="cv" class="rc-cv">{_console_cv_initial(turns)}</div>'
        '<form onsubmit="return send(event)" class="rc-form">'
        '<input id="ci" type="text" placeholder="AIに頼む…"><button class="rc-btn" type="submit">送信</button></form>'
        '<div id="costbar" class="rc-cost"></div></div></aside>'
    )
    center = f'<section class="rc-center">{_console_center(data_dir, params)}</section>'
    right = (f'<aside class="rc-right">'
             f'{_console_right(data_dir, v)}</aside>')
    body = ('<div class="ri-ws">'
            f'{_ri_nav("console")}'
            f'<main class="ri-main rc-main">{chat}{center}{right}</main></div>')
    boot = (f'window.THREAD={_js_json(active)};'
            f'window.BOOTHIST={_js_json(boot_hist)};')
    return _ri_shell("/console", "ことばで頼む", body, scripts=boot + _CONSOLE_JS)


# ---------------------------------------------------------------------------
# 主役ナビの実ページ (物件/マイソク/顧客/台帳) — 全て統一シェルを通る
# ---------------------------------------------------------------------------
def _kpis_html(items) -> str:
    """items: [(cls, n, label, href)] → 統一KPI行。"""
    cells = "".join(
        f'<a class="kpi {cls}" href="{href}"><div class="n">{_esc(n)}</div>'
        f'<div class="l">{_esc(label)}</div></a>'
        for cls, n, label, href in items)
    return f'<div class="ri-kpis">{cells}</div>'


def _wrap_main(active: str, route: str, title: str, inner: str) -> str:
    body = (f'<div class="ri-ws">{_ri_nav(active)}'
            f'<main class="ri-main">{inner}</main></div>')
    return _ri_shell(route, title, body)


def _docs_of_kind(data_dir: Path, kind: str):
    try:
        from hub_core import documents
        from hub_core.access import document_summary_access_allowed
        viewer = current_viewer()
        return [d for d in documents.list_documents(data_dir)
                if (d.get("kind") or "") == kind
                and (viewer is None
                     or document_summary_access_allowed(data_dir, viewer, d))]
    except Exception:
        return []


def _doc_library_html(data_dir: Path, kind: str, *, open_label: str = "全文を開く",
                      empty_msg: str = "保存済みの書類はまだありません。") -> str:
    """指定 kind の保存済み書類をカード一覧化。各カードに全文表示(/doc/preview)とConsole編集の導線。"""
    docs = _docs_of_kind(data_dir, kind)
    if not docs:
        return ui.empty(empty_msg)
    cards = []
    for d in docs:
        q = quote(d["doc_id"])
        if kind == "maisoku":
            output_links = _maisoku_export_links(
                d["doc_id"], data_dir=data_dir, version=int(d.get("latest") or 0))
        else:
            output_links = _juusetsu_export_links(
                d["doc_id"], data_dir=data_dir, version=int(d.get("latest") or 0))
        cards.append(
            f'<div class="ri-card"><div class="ct">{_esc(d["doc_id"])} '
            f'<span class="ri-badge warn">v{_esc(d.get("latest", ""))}</span></div>'
            f'<div class="cm">更新 {_esc(_display_datetime(d.get("updated") or ""))}</div>'
            '<div style="margin-top:11px;display:flex;gap:8px;flex-wrap:wrap">'
            f'<a class="ri-go" href="/doc/preview?doc={q}" target="_blank" rel="noopener">{_esc(open_label)}</a>'
            f'<a class="ri-go ghost" href="/console?doc={q}">ことばで編集</a></div>'
            '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
            f'{output_links}</div></div>')
    return f'<div class="ri-grid2">{"".join(cards)}</div>'


def _case_bound_export_href(data_dir: Path, doc_id: str, version: int,
                            output_format: str) -> str:
    """Return the only authorized export URL for a case-workspace document."""
    from hub_core import documents
    from hub_core.access import case_bound_document_metadata

    viewer = current_viewer()
    if viewer is None or not version:
        return ""
    try:
        meta = documents.get_version_metadata(data_dir, doc_id, version)
    except Exception:
        return ""
    case_id = str(meta.get("case_id") or "").strip()
    customer_id = str(meta.get("customer_id") or "").strip()
    if not case_id or not customer_id:
        return ""
    authorized = case_bound_document_metadata(
        data_dir, viewer, case_id=case_id, customer_id=customer_id,
        doc_id=doc_id, version=version, requested_format=output_format,
        require_four_kind=True,
    )
    if authorized is None:
        return ""
    return (f"/case/doc/file?doc={quote(doc_id)}&amp;v={version}"
            f"&amp;case={quote(case_id)}&amp;customer={quote(customer_id)}"
            f"&amp;as={quote(output_format)}")


def _maisoku_export_links(doc_id: str, *, data_dir: Path | None = None,
                          version: int | None = None) -> str:
    """Return only links that can work on this machine, plus browser-print fallback."""
    import importlib.util
    from hub_core import docgen

    q = quote(doc_id)
    xlsx_href = (_case_bound_export_href(data_dir, doc_id, int(version or 0), "xlsx")
                 if data_dir is not None else "")
    pdf_href = (_case_bound_export_href(data_dir, doc_id, int(version or 0), "pdf")
                if data_dir is not None else "")
    excel_ready = bool(docgen.MAISOKU_GENSHI.is_file()
                       and importlib.util.find_spec("openpyxl") is not None)
    links = []
    if excel_ready and xlsx_href:
        links.append(
            f'<a class="ri-go ghost" href="{xlsx_href}">Excelで出力</a>')
    if excel_ready and docgen._find_soffice() and pdf_href:
        links.append(f'<a class="ri-go" href="{pdf_href}">PDFで出力</a>')
    links.append(
        f'<a class="ri-go ghost" href="/doc/preview?doc={q}&amp;publish=1" target="_blank" rel="noopener" '
        'title="必要表示事項・広告表現・写真権利を確認してから開きます">'
        '印刷用に開く（PDF保存）</a>')
    return "".join(links)


def _juusetsu_export_links(doc_id: str, *, data_dir: Path | None = None,
                            version: int | None = None) -> str:
    """Expose visibly marked draft output and exact audit-bound final output separately."""
    import importlib.util
    from hub_core import docgen

    q = quote(doc_id)
    vq = f"&amp;v={int(version)}" if version else ""
    docx_href = (_case_bound_export_href(data_dir, doc_id, int(version or 0), "docx")
                 if data_dir is not None else "")
    pdf_href = (_case_bound_export_href(data_dir, doc_id, int(version or 0), "pdf")
                if data_dir is not None else "")
    word_ready = importlib.util.find_spec("docx") is not None
    links = []
    if word_ready and docx_href:
        links.append(
            f'<a class="ri-go ghost" href="{docx_href}">'
            'Word（様式）で出力（下書き・交付不可）</a>')
    if word_ready and docgen._find_soffice() and pdf_href:
        links.append(f'<a class="ri-go ghost" href="{pdf_href}">'
                     'PDF下書き（交付不可）</a>')
    links.append(
        f'<a class="ri-go ghost" href="/doc/preview?doc={q}{vq}" target="_blank" rel="noopener">'
        '印刷用に開く（PDF保存） / 下書き・交付不可</a>')
    evidence = None
    if data_dir is not None and version:
        try:
            from hub_core import documents
            evidence = documents.require_finalized_version(
                data_dir, doc_id, version, require_case=True)
        except Exception:
            evidence = None
    if evidence:
        final_q = f"{vq}&amp;publish=1"
        if word_ready:
            links.append(f'<a class="ri-go" href="/doc/file?doc={q}&amp;as=docx{final_q}">'
                         '確定版Word（監査照合済み）</a>')
        if word_ready and docgen._find_soffice():
            links.append(f'<a class="ri-go" href="/doc/file?doc={q}&amp;as=pdf{final_q}">'
                         '確定版PDF（監査照合済み）</a>')
        links.append(f'<a class="ri-go" href="/doc/preview?doc={q}{final_q}" '
                     'target="_blank" rel="noopener">確定版を開く（監査照合済み）</a>')
    return "".join(links)


def _properties_kanban(cases) -> str:
    """カンバン表示（CAN-04型・読み取り専用GET切替）。列=集約5ステージ。"""
    from hub_core.operations import CASE_STAGES, aggregate_status
    cols = {s: [] for s in CASE_STAGES}
    other = []
    for r in cases:
        stage = aggregate_status((r.get("状態") or "").strip())
        (cols[stage] if stage in cols else other).append(r)
    done_like = [r for r in other]
    board = []
    for stage in CASE_STAGES:
        cards = []
        for r in cols[stage][:12]:
            cid = r.get("案件ID") or ""
            cards.append(
                f'<a class="kb-card" href="/case?id={quote(cid)}">'
                f'<div class="kb-t">{_esc(r.get("物件名") or "(物件名未設定)")}</div>'
                f'<div class="kb-m">{_esc(r.get("顧客名") or "")}'
                f'<span class="lref">{_esc(cid)}</span></div></a>')
        more = (f'<div class="kb-more">ほか{len(cols[stage]) - 12}件</div>'
                if len(cols[stage]) > 12 else "")
        board.append(
            f'<div class="kb-col"><div class="kb-h">{_esc(stage)}'
            f'<span class="nb">{len(cols[stage])}</span></div>{"".join(cards) or "<div class=kb-empty>—</div>"}{more}</div>')
    tail = (f'<div class="gn" style="margin-top:8px">集約外（完了・その他）: {len(done_like)}件は台帳表示で。</div>'
            if done_like else "")
    return f'<div class="kb-board">{"".join(board)}</div>{tail}'




def _property_add_form(viewer) -> str:
    """物件をその場で登録する欄。

    これが無いと、物件0件の店主が **UIから1件も物件を入れられない**（案内は
    「ことばで頼む」だけで、AI未接続だとそこは行き止まり）。住所さえあれば登録できる。
    """
    from hub_core.operations import OP_ROLES
    if not (viewer and viewer.role in OP_ROLES.get("property_register", set())):
        return ""
    return ('<form class="pa-form" method="post" action="/op">'
            '<input type="hidden" name="op" value="property_register">'
            '<input type="hidden" name="deal_type" value="sale">'
            '<div class="ms-row"><label class="ms-l" for="pa-name">物件の名前</label>'
            '<input class="ms-i" type="text" id="pa-name" name="property_name" '
            'placeholder="芝浦リバーサイドレジデンス 503"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-addr">所在地（必須）</label>'
            '<input class="ms-i" type="text" id="pa-addr" name="address" '
            'placeholder="東京都港区芝浦3丁目12番8号" required></div>'
            '<div class="pf-grid">'
            '<div class="ms-row"><label class="ms-l" for="pa-price">販売価格</label>'
            '<input class="ms-i" type="text" id="pa-price" name="rent_or_price" placeholder="6,980万円"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-layout">間取り</label>'
            '<input class="ms-i" type="text" id="pa-layout" name="layout" placeholder="2LDK"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-area">専有面積</label>'
            '<input class="ms-i" type="text" id="pa-area" name="area" placeholder="58.42㎡"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-built">築年月</label>'
            '<input class="ms-i" type="text" id="pa-built" name="built_year" placeholder="2016年3月"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-structure">構造</label>'
            '<input class="ms-i" type="text" id="pa-structure" name="structure" placeholder="鉄筋コンクリート造"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-station">最寄駅</label>'
            '<input class="ms-i" type="text" id="pa-station" name="station" placeholder="田町駅"></div>'
            '<div class="ms-row"><label class="ms-l" for="pa-walk">徒歩</label>'
            '<input class="ms-i" type="text" id="pa-walk" name="walk_min" placeholder="8分"></div>'
            '</div>'
            '<div class="ms-actions">'
            '<button class="ms-go" type="submit">この物件を登録する</button></div></form>')


def _property_cards(cases: list) -> str:
    """物件を「行」でなく**仕事が生えるカード**で見せる。

    表に並べるだけだと、見えてはいても次に何をするかが分からない（高齢の店主には特に）。
    その物件でやることをカードの中に置き、案件IDのような符牒は控えめにする。
    """
    if not cases:
        return (ui.empty("まだ物件がありません。下の欄から登録できます。")
                + _property_add_form(current_viewer()))
    out = []
    for r in cases:
        cid = (r.get("案件ID") or "").strip()
        q = ("?case=" + quote(cid)) if cid else ""
        name = r.get("物件名") or "(物件名未設定)"
        deal = (r.get("取引種別") or "").strip()
        cust = (r.get("顧客名") or "").strip()
        meta = "／".join(x for x in (deal, (f"お客様 {cust}" if cust else "")) if x)
        acts = (
            (f'<a class="pc-go" href="/case?id={quote(cid)}">この物件の全部を見る</a>' if cid else "")
            + f'<a class="pc-go ghost" href="/maisoku/new-form{q}">マイソクを作る</a>'
            + f'<a class="pc-go ghost" href="/juusetsu/new{q}">重要事項説明書を作る</a>'
            + '<a class="pc-go ghost" href="/keisan">お金を計算する</a>')
        out.append(
            f'<div class="pc"><div class="pc-head">'
            f'<span class="pc-name">{_esc(name)}</span>'
            f'{_status_badge(r.get("状態") or "")}</div>'
            + (f'<div class="pc-meta">{_esc(meta)}</div>' if meta else "")
            + f'<div class="pc-acts">{acts}</div>'
            + (f'<div class="pc-id">案件番号 {_esc(cid)}</div>' if cid else "")
            + '</div>')
    return (f'<div class="pc-list">{"".join(out)}</div>'
            + ui.section("物件を足す") + _property_add_form(current_viewer()))


def render_properties(data_dir: Path, params) -> str:
    """物件 — cases.csv を正本にした案件・物件の一覧。各行は案件串刺し(/case)へ。
    表示切替（CAN-04型）: ?view=kanban でカンバン・既定は台帳テーブル。"""
    _, cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
    view = (params.get("view", [""])[0] or "").strip()
    done = {"完了", "成約", "クローズ", "解約"}
    active_n = sum(1 for r in cases if (r.get("状態") or "").strip() not in done | {""})
    held = sum(1 for r in cases if (r.get("保留種別") or "").strip())
    custn = len({r.get("顧客ID") for r in cases if r.get("顧客ID")})
    kpis = _kpis_html([
        ("blue", len(cases), "案件・物件", "/properties"),
        ("green", active_n, "進行中", "/properties"),
        ("org", held, "保留あり", "/hold"),
        ("blue", custn, "関係顧客", "/customers"),
    ])
    table = _property_cards(cases)
    detail = (
        '<div class="ri-grid2" style="margin-top:8px">'
        '<a class="ri-card" href="/research"><div class="ct">物件を調べる →</div>'
        '<div class="cm">役所調査・原典・ハザード等の進行中タスク。</div></a>'
        '<a class="ri-card" href="/documents"><div class="ct">作った書類を見る →</div>'
        '<div class="cm">重説・契約書類・OCR・専門確認の対象。</div></a>'
        '<a class="ri-card" href="/ads"><div class="ct">広告を出す前の確認 →</div>'
        '<div class="cm">公開前審査・掲載ゲート。記名・確認が必要。</div></a>'
        '<a class="ri-card" href="/console"><div class="ct">ことばで頼む →</div>'
        '<div class="cm">「ことばで頼む」から「この案件を内見に進めて」などと話しかけられます。</div></a>'
        '<a class="ri-card" href="/keisan"><div class="ct">お金を計算する →</div>'
        '<div class="cm">仲介手数料の上限・印紙・諸費用概算・ローン月々の目安。</div></a>'
        '<a class="ri-card" href="/analytics"><div class="ct">これまでの成果を見る →</div>'
        '<div class="cm">媒体別の反響・成約・成約率と直近の接触サマリ。</div></a>'
        '</div>')
    toggle = ('<div class="facets"><span class="flabel">表示:</span>'
              + (f'<a class="facet" href="/properties">台帳</a>'
                 f'<span class="facet on">カンバン</span>' if view == "kanban" else
                 f'<span class="facet on">台帳</span>'
                 f'<a class="facet" href="/properties?view=kanban">カンバン</a>')
              + '</div>')
    main_view = _properties_kanban(cases) if view == "kanban" else table
    inner = (ui.page_head("物件", "案件・物件の一覧。各物件から案件の全体ビュー（串刺し）へ移動できます。")
             + kpis + ui.section("物件・案件一覧") + toggle + main_view
             + ui.section("関連台帳・操作") + detail)
    return _wrap_main("properties", "/properties", "物件", inner)




def _parse_dt(s):
    from datetime import datetime
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt) if "%H" in fmt else datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            continue
    return None


def _dwell_class(due_at):
    """期限から滞留色を返す: ok(墨)/warn(朱薄・残2日)/hot(朱濃・期限切れ)。"""
    from datetime import datetime, timedelta, timezone
    d = _parse_dt(due_at)
    if d is None:
        return "ok"
    now = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)
    if d.date() < now.date():
        return "hot"
    if d.date() <= (now + timedelta(days=2)).date():
        return "warn"
    return "ok"


def _ics_esc(s):
    return str(s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _ics_for_case(data_dir, case_id):
    """案件の予定(events: 内見/契約日等)を .ics(iCalendar)で書き出す。GoogleカレンダーやApple/Outlookで開ける。
    OAuth・API・外部ネットワーク不要(設計の『外部ネットワーク0』を維持)。時刻のある予定のみ。bytes or None。"""
    from datetime import timedelta
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    if not db.exists():
        return None
    st = SqliteStore(db)
    cs = st.query("cases", "case_id = ?", (case_id,))
    if not cs:
        return None
    name = cs[0].get("customer_name") or case_id
    prop = cs[0].get("property_name") or ""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ainote//case//JP", "CALSCALE:GREGORIAN"]
    _co = load_company(data_dir, strict=True) or {}
    _org = str(_co.get("name") or "").strip()
    if _org:   # お客様の予定表に出る主催者は取扱会社（未設定なら出さない=別名を騙らない）
        out.append(f"X-WR-CALNAME:{_ics_esc(_org)}")
    has = False
    for e in (st.query("events", "case_id = ?", (case_id,)) or []):
        d = _parse_dt(e.get("event_at"))
        if d is None:
            continue
        start = d.strftime("%Y%m%dT%H%M%S")
        end = (d + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
        summary = (e.get("event_type") or "予定") + f"（{name}様）"
        uid = (e.get("event_id") or "ev") + "@ainote"
        out += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}",
                f"SUMMARY:{_ics_esc(summary)}", f"LOCATION:{_ics_esc(prop)}",
                f"DESCRIPTION:{_ics_esc('案件 ' + case_id)}", "END:VEVENT"]
        has = True
    out.append("END:VCALENDAR")
    return ("\r\n".join(out) + "\r\n").encode("utf-8") if has else None


def _ondo_rank(s):
    """温度感ランクを優先度数値に(A/今すぐ=3, B/おなやみ=2, C/そのうち=1)。"""
    s = str(s or "")
    if "A" in s or "今すぐ" in s:
        return 3
    if "B" in s or "おなやみ" in s:
        return 2
    if "C" in s or "そのうち" in s or "まだ" in s:
        return 1
    return 0


def _dwell_rank(cls):
    return {"hot": 2, "warn": 1}.get(cls, 0)


def _pipeline_rows(data_dir):
    """全案件に journey(ステージ/期限/最終接触)・温度感・滞留色を結合し、要対応優先(滞留→温度感)でソート。"""
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    if not db.exists():
        return []
    st = SqliteStore(db)
    try:
        cases = st.query("cases", None) or []
        journeys = {j.get("case_id"): j for j in (st.query("customer_journey", None) or [])}
        ondo = {a.get("customer_id"): a.get("field_value")
                for a in (st.query("customer_attributes", "field_key = ?", ("温度感",)) or [])}
    except Exception:
        return []
    rows = []
    for c in cases:
        cid = c.get("case_id")
        if not cid:
            continue
        j = journeys.get(cid, {})
        stage = j.get("stage") or c.get("status") or "—"
        due = j.get("due_at") or ""
        last = j.get("last_contact_at") or ""
        rows.append({"case_id": cid, "customer_id": c.get("customer_id") or "",
                     "name": c.get("customer_name") or cid, "deal_type": c.get("deal_type") or "",
                     "stage": stage, "due": due, "last": last,
                     "ondo": ondo.get(c.get("customer_id") or "", ""), "dwell": _dwell_class(due)})
    rows.sort(key=lambda r: (_dwell_rank(r["dwell"]), _ondo_rank(r["ondo"])), reverse=True)
    return rows




def _pipeline_board(rows, limit=40):
    """要対応サマリ＋温度感/滞留順の顧客行(タイムラインへリンク)。『今日どの客に動くか』の司令塔。"""
    hot = sum(1 for r in rows if r["dwell"] == "hot")
    warn = sum(1 for r in rows if r["dwell"] == "warn")
    calm = len(rows) - hot - warn
    alert = ('<div class="pl-alert">'
             f'<div class="pl-a hot"><b>{hot}</b> 滞留・期限切れ</div>'
             f'<div class="pl-a warn"><b>{warn}</b> 期限接近</div>'
             f'<div class="pl-a calm"><b>{calm}</b> 順調</div></div>')
    body = ""
    for r in rows[:limit]:
        dw = r["dwell"]
        ond = _ondo_rank(r["ondo"])
        ocls = "a" if ond == 3 else ("b" if ond == 2 else "c")
        obadge = f'<span class="pl-ondo {ocls}">温 {_esc(r["ondo"])}</span>' if r["ondo"] else ""
        meta = ""
        if r["due"]:
            dlabel = ("期限切れ" if dw == "hot" else ("期限接近" if dw == "warn" else "期限"))
            meta = f'<span class="pl-due {dw}">{dlabel} {_esc(r["due"][:10])}</span>'
        if r["last"]:
            meta += f'<br>最終接触 {_esc(r["last"][:10])}'
        body += (f'<a class="pl-row {dw}" href="/timeline?id={quote(r["case_id"])}">'
                 f'<span class="pl-nm">{_esc(r["name"])}</span>'
                 f'<span class="pl-deal">{_esc(_deal_label(r["deal_type"]))}</span>'
                 f'<span class="pl-stage">{_esc(r["stage"])}</span>{obadge}'
                 f'<span class="pl-meta">{meta}</span></a>')
    if not rows:
        body = '<div class="ri-card cm">案件がありません。反響を顧客化すると、ここに進捗が並びます。</div>'
    return alert + body


def _home_attn(data_dir):
    """ホーム上部の要対応バナー(滞留・期限接近の自動浮上→/customers)。無ければ空。"""
    rows = _pipeline_rows(data_dir)
    hot = sum(1 for r in rows if r["dwell"] == "hot")
    warn = sum(1 for r in rows if r["dwell"] == "warn")
    if not (hot or warn):
        return ""
    parts = ""
    if hot:
        parts += f'<a class="pl-a hot" href="/customers"><b>{hot}</b> 滞留・期限切れ → 要対応</a>'
    if warn:
        parts += f'<a class="pl-a warn" href="/customers"><b>{warn}</b> 期限接近</a>'
    return f'<div class="pl-alert" style="margin:0 0 22px">{parts}</div>'


def render_timeline(data_dir: Path, params) -> str:
    """顧客タイムライン: 顧客が今どこにいるかを3カラムで(中央=進捗タイムライン＋ステージバー＋次アクション/
    右=属性パネル＋書類チェック＋おすすめ物件)。設計 §7。既存shell(右ペイン対応)に載る。"""
    from hub_core import operations as _ops
    from hub_core.store import SqliteStore
    cid = (params.get("id", [""])[0] or "").strip()
    db = Path(data_dir) / "hub.db"
    st = SqliteStore(db) if db.exists() else None

    def q(table, where=None, args=()):
        try:
            return st.query(table, where, args) if st else []
        except Exception:
            return []

    cases = q("cases", "case_id = ?", (cid,))
    if not cid or not cases:
        body = ('<div class="ri-sech">顧客タイムライン</div>'
                '<div class="ri-card">顧客一覧から対象のお客様を選択してください。</div>')
        return ui.shell("properties", "タイムライン", f'<div class="tl-wrap">{body}</div>',
                        viewer_role=_vr(), viewer_user=_vu())
    case = cases[0]
    deal_type = case.get("deal_type") or "lease_tenant"
    stages = _ops.stages_for(deal_type)
    jr = q("customer_journey", "case_id = ?", (cid,))
    cur_stage = jr[0].get("stage") if jr else (case.get("status") or stages[0])
    due_at = jr[0].get("due_at") if jr else ""
    last_contact = jr[0].get("last_contact_at") if jr else ""
    cust_id = case.get("customer_id") or ""

    # --- ステージバー ---
    try:
        ci = stages.index(cur_stage)
    except ValueError:
        ci = 0
    sts = ""
    for i, s in enumerate(stages):
        cls = "now" if i == ci else ("done" if i < ci else "")
        sts += f'<div class="tl-st {cls}"><span class="d">{i+1}</span>{_esc(s)}</div>'
    stage_bar = f'<div class="tl-stages">{sts}</div>'

    # --- 次アクションカード(滞留色) ---
    dw = _dwell_class(due_at)
    NEXT = {"反響": "即レス→ヒアリングアポ", "追客": "条件合致物件を配信・架電", "ヒアリング": "物件提案へ",
            "物件提案": "内見アポ調整", "内見": "入居申込書の記入へ", "申込": "保証会社へ審査送信",
            "審査": "契約日確定→重説準備", "重説": "賃貸借契約の締結へ", "契約": "初期費用請求へ",
            "初期費用": "鍵渡しの段取り", "鍵渡し": "管理引継・台帳更新", "管理": "更新6ヶ月前リマインド",
            "反響ヒアリング": "物件提案・事前審査提案", "買付事前審査": "売主へ条件取次",
            "条件交渉": "重説日程確定", "売買契約": "ローン本審査支援へ", "ローン本審査": "決済段取り",
            "決済引渡": "仲介手数料受領・アフター"}
    due_label = ("期限なし" if not due_at else
                 ("期限切れ " + _esc(due_at[:10]) if dw == "hot" else
                  ("期限接近 " + _esc(due_at[:10]) if dw == "warn" else "期限 " + _esc(due_at[:10]))))
    next_card = (f'<div class="tl-next {dw}"><div><div class="na">次の一手: {_esc(NEXT.get(cur_stage, "次アクションを設定"))}</div>'
                 f'<div class="nd">現在地「{_esc(cur_stage)}」'
                 + (f' ・ 最終接触 {_esc(last_contact[:10])}' if last_contact else "")
                 + f'</div></div><span class="tl-due {dw}">{due_label}</span></div>')

    # --- 縦タイムライン(events + contact_log を時系列降順) ---
    rows = []
    for e in q("events", "case_id = ?", (cid,)):
        rows.append((e.get("event_at") or "", "ev", e.get("event_type") or "イベント",
                     e.get("source_tool") or "", e.get("event_id") or ""))
    for c in q("contact_log", "customer_id = ?", (cust_id,)):
        rows.append((c.get("occurred_at") or "", "ct", (c.get("channel") or "接触") + "：" + (c.get("summary") or ""),
                     c.get("actor") or "", c.get("reaction") or ""))
    rows.sort(key=lambda r: r[0], reverse=True)
    LAWG = {"重説", "契約", "売買契約", "賃貸借契約", "決済", "記名"}
    MONEYG = {"初期費用", "手付", "請求", "決済", "入金"}
    evs = ""
    for at, kind, title, actor, extra in rows[:30]:
        title = _visible_data_value("出来事", title)
        actor = _visible_data_value("担当者", actor)
        extra = _visible_data_value("記録", extra)
        gcls = "law" if any(g in title for g in LAWG) else ("money" if any(g in title for g in MONEYG) else "")
        gate = (' <span class="tl-gate">law</span>' if gcls == "law"
                else ' <span class="tl-gate">money</span>' if gcls == "money" else "")
        meta = (_esc(at[:16].replace("T", " ")) if at else "") + (f' ・ {_esc(actor)}' if actor else "")
        why = f'<div class="ew">{_esc(extra)}</div>' if extra else ""
        evs += (f'<div class="tl-ev {gcls}"><div class="et">{_esc(title)}{gate}</div>'
                f'<div class="em">{meta}</div>{why}</div>')
    if not evs:
        evs = '<div class="ri-card cm">まだ接触・イベントの記録がありません。</div>'
    timeline = f'<div class="ri-sech" style="margin-top:4px">経緯（時系列）</div><div class="tl-line">{evs}</div>'

    # --- 操作(ステージ前進・失注記録・接触記録・権限ある viewer のみ) ---
    role = _vr()
    is_lost = (case.get("status") or "") == "失注"
    next_stage = stages[ci + 1] if ci + 1 < len(stages) else ""
    acts = ""
    if not is_lost and next_stage and role in _ops.OP_ROLES.get("stage_advance", set()):
        acts += ('<form method="post" action="/op" style="margin:0;display:inline-flex">'
                 '<input type="hidden" name="op" value="stage_advance">'
                 f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
                 f'<input type="hidden" name="to_stage" value="{_esc(next_stage)}">'
                 f'<button class="ri-go" type="submit">「{_esc(next_stage)}」へ進める →</button></form>')
    if not is_lost and role in _ops.OP_ROLES.get("case_lose", set()):
        # 失注は理由必須(選択式)。理由の蓄積が仕入れ・掲載改善の源泉(CRMギャップp1)
        lopts = "".join(f'<option>{_esc(r)}</option>' for r in _ops.LOST_REASONS)
        acts += ('<form method="post" action="/op" style="margin:0;display:inline-flex;gap:6px">'
                 '<input type="hidden" name="op" value="case_lose">'
                 f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
                 f'<select name="reason">{lopts}</select>'
                 '<button class="ri-go ghost" type="submit">失注として記録</button></form>')
    if cust_id and role in _ops.OP_ROLES.get("contact_log_add", set()):
        acts += ('<form method="post" action="/op" class="tl-clog">'
                 '<input type="hidden" name="op" value="contact_log_add">'
                 f'<input type="hidden" name="customer_id" value="{_esc(cust_id)}">'
                 f'<input type="hidden" name="case_id" value="{_esc(cid)}">'
                 '<select name="channel"><option>架電</option><option>メール</option><option>LINE</option>'
                 '<option>来店</option><option>内見</option><option>SMS</option></select>'
                 '<input type="text" name="summary" placeholder="接触の要約">'
                 '<button class="ri-go ghost" type="submit">接触を記録</button></form>')
    if q("events", "case_id = ?", (cid,)):  # 予定があればカレンダー書き出し(Googleカレンダー等で開ける)
        acts += f'<a class="ri-go ghost" href="/cal/ics?case={quote(cid)}">📅 予定を書き出す（.ics→Googleカレンダー）</a>'
    acts_html = f'<div class="tl-actions">{acts}</div>' if acts else ""

    files_html = _files_panel(data_dir, "case", cid, "/timeline?id=" + quote(cid))
    lost_badge = '<span class="ri-badge bad">失注</span>' if is_lost else ""
    center = (f'<div class="tl-wrap">'
              f'<div class="tl-head"><span class="nm">{_esc(_visible_data_value("顧客名", case.get("customer_name") or cid))}</span>'
              f'<span class="ri-badge warn">{_esc(_deal_label(deal_type))}</span>{lost_badge}'
              f'<span class="lb">顧客タイムライン</span></div>'
              f'{stage_bar}{next_card}{acts_html}{timeline}{files_html}</div>')

    right = _timeline_right(data_dir, q, cid, cust_id, deal_type, cur_stage)
    return ui.shell("properties", "タイムライン", center, right=right, viewer_role=_vr(), viewer_user=_vu())


def _deal_label(dt):
    return {"lease_tenant": "賃貸・客付", "lease_landlord": "賃貸・元付",
            "sale_buyer": "売買・買主", "sale_seller": "売買・売主",
            "sale": "売買", "lease": "賃貸"}.get(dt, dt)


def _vr():
    v = current_viewer()
    return getattr(v, "role", None)


def _vu():
    v = current_viewer()
    return getattr(v, "user", None)


def _timeline_right(data_dir, q, cid, cust_id, deal_type="", cur_stage=""):
    """右ペイン: 属性(計算フィールド込み)＋書類チェック＋おすすめ物件(マッチング)。"""
    attrs = q("customer_attributes", "customer_id = ?", (cust_id,))
    by_cat = {}
    amap = {}
    for a in attrs:
        by_cat.setdefault(a.get("category") or "その他", []).append(a)
        amap[a.get("field_key")] = a.get("field_value")
    # 計算フィールド: 年収→許容家賃(月収比30%)
    calc = ""
    income = _num(amap.get("年収") or amap.get("月収"))
    if income:
        rent = int(income * 10000 / 12 * 0.30) if "年収" in (amap.get("年収") and "年収" or "") or amap.get("年収") else int(income * 0.30)
        if amap.get("年収"):
            rent = int(_num(amap.get("年収")) * 10000 / 12 * 0.30)
            calc = f'<div class="tl-calc">支払能力(家賃月収比30%)<br><b>〜{rent:,}円/月</b> が提案上限</div>'
    order = ["希望条件", "与信", "個人情報", "動機", "温度感", "その他"]
    ah = ""
    for cat in order:
        if cat not in by_cat:
            continue
        ah += f'<div class="ag">{_esc(cat)}</div>'
        for a in by_cat[cat]:
            ah += f'<div class="ar"><span class="ak">{_esc(a.get("field_key"))}</span><span class="av">{_esc(a.get("field_value") or "—")}</span></div>'
    attr_panel = (f'<div class="ri-right-h">顧客属性</div><div class="tl-attr">{calc}{ah or "<div class=cm>属性未登録</div>"}</div>')

    # 書類チェック
    reqs = q("document_requirements", "case_id = ?", (cid,))
    # ファイル自動充足: caseフォルダに doc_kind と一致する書類種別のファイルがあれば充足扱い
    from hub_core import files as _f
    file_hints = {i["doc_hint"] for i in _f.list_files(data_dir, "case", cid) if i["doc_hint"]}
    dh = ""
    for r in reqs:
        kind = r.get("doc_kind") or ""
        present = str(r.get("present") or "").strip() in ("1", "true", "済", "yes")
        by_file = any(h and (h in kind or kind in h) for h in file_hints)
        ok = present or by_file
        req = str(r.get("required") or "").strip() in ("1", "true", "必須", "yes")
        cls = "ok" if ok else ("miss" if req else "")
        tag = '<span style="font-size:18px;color:var(--ok);margin-left:4px">ファイル添付</span>' if (by_file and not present) else ""
        dh += (f'<div class="tl-doc {cls}"><span class="dc">{"✓" if ok else ""}</span>'
               f'{_esc(kind)}{"（要）" if (req and not ok) else ""}{tag}</div>')
    doc_panel = (f'<div class="ri-right-h" style="margin-top:22px">必要書類</div>{dh or "<div class=cm>—</div>"}') if reqs else ""

    # おすすめ物件: 顧客の希望条件でスコアリング(設計§5・P4マッチング)
    matched = _match_properties(data_dir, cust_id, deal_type)
    ph = ""
    for p, reasons in matched:
        rs = p.get("risk_scores") or ""
        risk = f'<span class="pr">🌊 {_esc(rs.split(",")[0] if "," in rs else rs)}</span>' if rs else ""
        why = f'<span class="pr" style="background:var(--accent-bg);color:var(--accent)">{_esc("・".join(reasons))} 一致</span>' if reasons else ""
        ph += (f'<div class="tl-prop"><div class="pn">{_esc(p.get("address") or p.get("property_id"))}</div>'
               f'<div class="pm">{_esc(p.get("rent_or_price") or "")} ／ {_esc(p.get("layout") or "")} ／ '
               f'{_esc(p.get("station") or "")}{("徒歩"+_esc(p.get("walk_min"))+"分" if p.get("walk_min") else "")}</div>'
               f'{why}{risk}</div>')
    prop_panel = (f'<div class="ri-right-h" style="margin-top:22px">おすすめ物件</div>{ph}') if matched else ""

    return attr_panel + doc_panel + prop_panel


def _num(s):
    import re as _re
    m = _re.search(r"[\d,\.]+", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else 0


def _safe_back(back, default="/timeline"):
    """オープンリダイレクト防止: 同一オリジンの相対パスのみ許可。
    外部URL(http://…)・プロトコル相対(//evil)・バックスラッシュ誘導(/\\evil)は default に倒す。"""
    b = str(back or "")
    if b.startswith("/") and not b.startswith("//") and not b.startswith("/\\"):
        return b
    return default


def _file_access_allowed(data_dir: Path, viewer: Viewer | None, scope: str,
                         entity_id: str, filename: str) -> bool:
    """Authorize both the file entity and its sensitivity class."""
    from hub_core import files as _files

    if viewer is None or scope not in _files.SCOPES or not str(entity_id or "").strip():
        return False
    if viewer.sees_all_rows():
        return True
    # 担当 role はPII列を見られない。顧客ファイルとOffice/PDF書類も同じ境界に置く。
    if scope == "customer" or Path(str(filename or "")).suffix.lower() in _files.DOC_EXT:
        return False
    from hub_core.access import case_access_allowed, related_entity_access_allowed
    if scope == "case":
        return case_access_allowed(data_dir, viewer, entity_id)
    if scope == "property":
        return related_entity_access_allowed(
            data_dir, viewer, "property_id", entity_id)
    return False


def _match_properties(data_dir, customer_id, deal_type="", top=3):
    """顧客の希望条件(customer_attributes)に対し物件をスコアリングし上位を返す。
    実装正本は hub_core/matching.py(両方向マッチングの単一正本・NG条件は除外)。"""
    from hub_core import matching as _m
    return _m.match_properties(data_dir, customer_id, deal_type, top)


def _files_panel(data_dir, scope, eid, back):
    """Obsidian式ファイル管理: 写真グリッド＋書類一覧＋ローカルフォルダ案内＋アップロード。"""
    from hub_core import files as _files
    items = _files.list_files(data_dir, scope, eid)
    viewer = current_viewer()
    if viewer is not None:
        items = [item for item in items
                 if _file_access_allowed(data_dir, viewer, scope, eid, item["name"])]
    photos = [i for i in items if i["kind"] == "photo"]
    docs = [i for i in items if i["kind"] != "photo"]
    def raw(i):
        return (f'/file/raw?scope={quote(scope)}&amp;id={quote(eid)}&amp;name={quote(i["name"])}')

    up = ('<form class="tl-up" method="post" action="/file/upload" enctype="multipart/form-data">'
          f'<input type="hidden" name="scope" value="{_esc(scope)}">'
          f'<input type="hidden" name="id" value="{_esc(eid)}">'
          f'<input type="hidden" name="back" value="{_esc(back)}">'
          '<input type="file" name="f" multiple accept="image/*,.pdf,.docx,.xlsx,.doc,.xls">'
          '<button class="ri-go" type="submit">アップロード</button></form>')
    ph = ""
    for i in photos:
        if i["web_viewable"]:
            ph += (f'<a class="tl-ph" href="{raw(i)}" target="_blank" rel="noopener">'
                   f'<img src="{raw(i)}" alt="{_esc(i["name"])}" loading="lazy">'
                   f'<span class="cap">{_esc(i["doc_hint"] or i["name"])}</span></a>')
        else:
            ph += (f'<a class="tl-ph" href="{raw(i)}" style="display:flex;align-items:center;justify-content:center;text-align:center">'
                   f'<span style="font-size:18px;color:var(--muted);padding:6px">{_esc(i["ext"][1:].upper())}<br>{_esc(i["name"])}</span></a>')
    photos_html = f'<div class="tl-photos">{ph}</div>' if photos else ""
    dh = ""
    for i in docs:
        hint = f'<span class="fk">{_esc(i["doc_hint"])}</span>' if i["doc_hint"] else ""
        dh += (f'<div class="tl-fr">📄 {_esc(i["name"])} {hint}'
               f'<span class="fs">{_esc(i["size_h"])} ・ {_esc(i["mtime"])}</span>'
               f'<a href="{raw(i)}" target="_blank" rel="noopener">開く</a></div>')
    folder = ('<div class="tl-folder">ファイルはこの案件の中に保存されます。'
              '<b>複数端末への移動には暗号化バックアップを使ってください。</b>'
              f' 写真 {len(photos)}・書類 {len(docs)}</div>')
    empty = "" if items else '<div class="ri-card cm">まだファイルがありません。上のボタンから追加してください。</div>'
    return ('<div class="ri-sech" style="margin-top:24px">ファイル（写真・収入証明 等）</div>'
            f'{folder}{up}{photos_html}{dh}{empty}')


_DOC_CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


def _juusetsu_draft_copy(body: str) -> str:
    """Make any non-publish rendering unambiguously unusable for delivery."""
    replacement = (
        "> 【下書き・交付不可】内部確認用コピーです。確定済みの内容を含む場合も、"
        "顧客への交付には監査照合済みの確定版出力を使用してください。"
    )
    text, count = re.subn(
        r"^>[^\n]*(?:【確定版】|記名確定した重要事項説明書)[^\n]*$",
        replacement, str(body), count=1, flags=re.MULTILINE,
    )
    if count:
        return text
    if "下書き・交付不可" in text:
        return text
    lines = text.splitlines()
    insert_at = 1
    if len(lines) > 1 and lines[1].startswith("<!-- ainote-juusetsu-schema:"):
        insert_at = 2
    lines.insert(insert_at, replacement)
    return "\n".join(lines)


def _validated_office_payload(path: Path) -> tuple[bytes, str]:
    """Read one generated artifact and bind suffix, MIME and file magic exactly."""
    import io
    import zipfile

    suffix = path.suffix.lower()
    content_type = _DOC_CONTENT_TYPES.get(suffix)
    if content_type is None or not path.is_file() or path.is_symlink():
        raise ValueError("出力ファイルの形式を確認できません。")
    payload = path.read_bytes()
    if suffix == ".pdf":
        if not payload.startswith(b"%PDF-"):
            raise ValueError("PDFの内容とContent-Typeが一致しません。")
    elif suffix in {".docx", ".xlsx"}:
        if not payload.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(io.BytesIO(payload)):
            raise ValueError("Officeファイルの内容とContent-Typeが一致しません。")
        required_member = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if required_member not in archive.namelist():
                raise ValueError("Officeファイルの内部形式を確認できません。")
    return payload, content_type


def _article37_fields_for_draft(data: dict, items: list[str]) -> dict[str, str]:
    """Make every unresolved statutory field visibly unresolved in the artifact."""
    import unicodedata

    unknown = {
        "", "-", "?", "??", "unknown", "n/a", "na", "none", "null", "tbd", "tba",
        "不明", "未入力", "未確認", "未定", "要確認",
    }
    result = {}
    for item in items:
        value = data.get(item)
        if isinstance(value, (dict, list, tuple, set)):
            result[item] = "要確認"
            continue
        text = str(value or "").strip()
        normalized = unicodedata.normalize("NFKC", text).casefold()
        result[item] = "要確認" if normalized in unknown else text
    return result


def _generate_doc_file(data_dir: Path, doc_id: str, as_fmt: str, prs_address: str = "",
                       *, version: int | None = None, publish: bool = False):
    """保存済み書類を実物Office様式で生成して (bytes, filename, content_type) を返す。
    マイソク=fill_maisoku_xlsx、35条/売買条件確認票=build_md_docx、
    37条=build_keiyaku37_docx。4種のlifecycleと真実ラベルは混ぜない。
    重説は物件住所があればPRS災害リスク(洪水・参考スクリーニング)を自動充填(法定ハザードマップの代替でない旨明記)。
    as_fmt='pdf' は LibreOffice で変換。HTML近似でなく実物のExcel/Word/PDFを出す。"""
    import importlib.util
    import tempfile
    from hub_core import documents, docgen
    cur = documents.get_version(data_dir, doc_id, version)
    meta = cur.get("meta", {}) or {}
    kind = (meta.get("kind") or "").lower()
    canonical_kind = documents.canonical_four_document_kind(kind)
    body = cur.get("body") or ""
    base = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", doc_id)[:80] or "document"
    allowed = documents.four_document_output_formats(canonical_kind or "")
    if not allowed:
        raise ValueError("この書類は4帳票の様式出力に対応していません。")
    if as_fmt not in allowed:
        raise ValueError(f"{canonical_kind} の出力形式 {as_fmt!r} には対応していません。")
    if publish and canonical_kind != "juusetsu35":
        raise ValueError("確定版出力は監査照合済みの35条書面だけに対応しています。")
    with tempfile.TemporaryDirectory(prefix="ainote_doc_export_") as td:
        tmp = Path(td)
        if canonical_kind == "maisoku":
            if importlib.util.find_spec("openpyxl") is None:
                raise ValueError(
                    "Excel出力には openpyxl が必要です。DEMO.md の手順で追加してください。")
            try:
                fields = json.loads(body or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("マイソクの保存データが壊れているため出力できません。") from exc
            from hub_core import branding, maisoku
            profile = branding.load_snapshot(
                data_dir, str(meta.get("company_profile_hash") or ""))
            fields, _manifest = maisoku.validate_for_publish(
                fields, data_dir, str(fields.get("property") or ""), company=profile)
            src = Path(docgen.fill_maisoku_xlsx(
                fields, tmp / (base + ".xlsx"), company=profile))
        elif canonical_kind == "juusetsu35":
            if importlib.util.find_spec("docx") is None:
                raise ValueError(
                    "Word出力には python-docx が必要です。DEMO.md の手順で追加してください。")
            evidence = None
            if publish:
                evidence = documents.require_finalized_version(
                    data_dir, doc_id, int(meta.get("version") or 0), require_case=True)
            export_body = body if evidence else _juusetsu_draft_copy(body)
            deal = meta.get("deal_type") or ("賃貸" if ("賃貸" in body or "貸借" in body) else "売買")
            addr = (prs_address or meta.get("property_address") or "").strip()
            prs_note = ""
            if addr:
                try:
                    from hub_core import prs as _prs
                    if _prs.configured():
                        prs_note = _prs.juusetsu_note(address=addr).get("text", "")
                except Exception:
                    prs_note = ""
            # 保存済み重説の本文(md)からWordを生成＝プレビューと同一内容を客に渡せる形で出力
            # （旧: build_juusetsu_docx({})は空の国交省様式を出す偽収束バグだった）。
            suffix = (f"_FINAL_v{int(meta.get('version') or 0)}" if evidence
                      else "_DRAFT_交付不可")
            audit_label = ""
            if evidence:
                audit_label = (f"{evidence['target']} / 案件 {evidence['case_id']} / "
                               f"SHA-256 {evidence['content_sha256']}")
            src = Path(docgen.build_md_docx(
                export_body, tmp / (base + suffix + ".docx"), prs_note=prs_note,
                document_status="final" if evidence else "draft", audit_label=audit_label,
            ))
        elif canonical_kind == "sale_condition_check":
            if importlib.util.find_spec("docx") is None:
                raise ValueError(
                    "Word出力には python-docx が必要です。DEMO.md の手順で追加してください。")
            # 売買契約書へ昇格させない。保存本文の条件確認票をDRAFTとしてだけ出力する。
            src = Path(docgen.build_md_docx(
                body, tmp / (base + "_売買条件確認票_契約不可_DRAFT.docx"),
                document_status="draft",
            ))
        else:  # article37
            if importlib.util.find_spec("docx") is None:
                raise ValueError(
                    "Word出力には python-docx が必要です。DEMO.md の手順で追加してください。")
            try:
                article_data = json.loads(body or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("37条書面の保存データが壊れているため出力できません。") from exc
            if not isinstance(article_data, dict):
                raise ValueError("37条書面の保存データを確認できません。")
            deal = str(meta.get("deal_type") or "売買")
            is_sale = "賃貸" not in deal and "貸" not in deal
            items = [item for item in docgen.KEIYAKU37_ITEMS_COMMON
                     if not (item.startswith("移転登記") and not is_sale)]
            article_data = _article37_fields_for_draft(article_data, items)
            src = Path(docgen.build_keiyaku37_docx(
                article_data, tmp / (base + "_37条書面_交付不可_DRAFT.docx"),
                deal_type=deal,
            ))
        if as_fmt == "pdf":
            pdf = docgen.office_to_pdf(str(src), str(tmp))
            if not pdf:
                raise ValueError("PDF変換には LibreOffice(soffice) が必要です。印刷用画面からPDF保存してください。")
            src = Path(pdf)
        payload, content_type = _validated_office_payload(src)
        return payload, src.name, content_type


def _doc_output_buttons(doc_id: str, kind: str) -> str:
    """書類のOffice出力と、その端末で利用できるPDF導線。"""
    if kind == "maisoku":
        return ('<div class="ri-examples" style="margin-top:10px">'
                + _maisoku_export_links(doc_id) + '</div>')
    elif kind == "juusetsu":
        return ('<div class="ri-examples" style="margin-top:10px">'
                + _juusetsu_export_links(doc_id) + '</div>')
    else:
        return ""


# マイソク単独の新規作成で使う要点フィールド（67項目の全部でなく、まず必須級）。
MAISOKU_NEW_FIELDS = [
    ("property_type", "物件種目", "物件"), ("property_name", "物件名", "物件"),
    ("title_copy", "キャッチコピー", "物件"), ("price", "価格／賃料", "物件"),
    ("address", "所在地", "立地"), ("nearest_station", "最寄駅", "立地"), ("access", "交通（徒歩分）", "立地"),
    ("walk_distance_m", "徒歩経路の道路距離（m）", "立地"),
    ("land_area", "土地／敷地面積", "面積・仕様"), ("building_area", "建物／専有面積", "面積・仕様"),
    ("floor_plan", "間取り", "面積・仕様"), ("built", "築年月", "面積・仕様"),
    ("structure", "構造", "面積・仕様"), ("floors_total", "階建／所在階", "面積・仕様"),
    ("torihiki_taiyo", "取引態様", "取引"), ("bikou", "備考", "取引"),
]


def _ocr_to_maisoku(st: dict) -> dict:
    """OCR構造化フィールド → マイソク新規フォームのキーへ写像（読取れた項目のみ・捏造なし）。"""
    import re as _re
    m = {}
    direct = {
        "property_type": "property_type", "property_name": "property_name",
        "price": "price", "address": "address", "land_area": "land_area",
        "area": "building_area", "layout": "floor_plan", "built": "built",
        "structure": "structure", "floor": "floors_total",
    }
    for src_k, dst_k in direct.items():
        v = str(st.get(src_k) or "").strip()
        if v:
            m[dst_k] = v
    transit = str(st.get("transit") or "").strip()
    if transit:
        m["access"] = transit
        # 駅名: まず括弧内（『品川』/『品川」等の不揃いも許容）、無ければ駅直前の語（・や括弧・路線名を除く）。
        sm = _re.search(r"[『「]([^『「』」]{1,10})[』」]\s*駅", transit)
        if not sm:
            sm = _re.search(r"([一-龥ぁ-んァ-ヶA-Za-z0-9]{2,10})\s*駅\s*徒歩", transit)
        station = sm.group(1).strip() if sm else ""
        if station:
            m["nearest_station"] = station + "駅"
    # 表の外の補足（管理費/修繕/用途地域/総戸数/駐車場）は備考へ（下書き・要確認）
    extras = []
    for k, lab in [("management_fee", "管理費"), ("repair_fund", "修繕積立金"),
                   ("youto", "用途地域"), ("total_units", "総戸数"), ("parking", "駐車場")]:
        v = str(st.get(k) or "").strip()
        if v:
            extras.append(f"{lab}：{v}")
    if extras:
        m["bikou"] = "OCR読取（要確認）｜" + " ／ ".join(extras)
    return m


def _ocr_to_juusetsu(st: dict) -> dict:
    """OCR構造化フィールド → 重説フォーム(juusetsu_draft.FIELDS)のキーへ写像（読取れた項目のみ・捏造なし）。
    販売価格は売買様式の代金欄へ写す。値はOCR下書きなので宅建士が原本と照合する。"""
    m = {}
    direct = {
        "property_name": "property_name", "address": "address", "structure": "structure",
        "area": "area", "layout": "layout", "built": "built", "floor": "floors",
        "youto": "youto", "management_fee": "kanri_fee", "price": "price",
    }
    for src_k, dst_k in direct.items():
        v = str(st.get(src_k) or "").strip()
        if v:
            m[dst_k] = v
    # 販売図面（価格あり）は売買取引＝取引態様を売買に寄せる（賃貸/売買で様式が切り替わる）。
    if str(st.get("price") or "").strip():
        m["deal_type"] = "売買"
    return m



# マイソク新規作成の「窓口型」段階。1画面で聞くことを1つに絞る（高齢者対応の芯）。
# 既存の POST /maisoku/new-create は変更しない＝各段は前の入力を hidden で持ち回る。
MAISOKU_STEPS = [
    ("basic", "まず、どんな物件ですか", ("property_type", "property_name", "price"),
     "物件名と価格だけあれば作れます。あとは後からでも足せます。"),
    ("place", "つぎに、場所を教えてください",
     ("address", "nearest_station", "access", "walk_distance_m"),
     "徒歩分数を書く場合は、地図で確認した道路距離も入れてください。"),
    ("spec", "広さと造りを入れます", ("land_area", "building_area", "floor_plan",
                                      "built", "structure", "floors_total"),
     "登記や元の図面から写せる項目です。分からない欄は飛ばしてください。"),
    ("deal", "最後に、取引のことを", ("torihiki_taiyo", "title_copy", "bikou"),
     "取引態様は広告に必ず要る項目です。"),
]
_MAISOKU_LABEL = {k: l for k, l, _g in MAISOKU_NEW_FIELDS}


def _maisoku_step_index(name: str) -> int:
    for i, (key, _t, _f, _h) in enumerate(MAISOKU_STEPS):
        if key == name:
            return i
    return 0


def _copy_suggestion_html(answered: dict) -> str:
    """キャッチコピー欄の下に、いま入っている事実から作れる候補を出す。

    考えて言葉をひねり出すのではなく、前の画面で答えた事実（駅・徒歩分数・間取り・
    築年・設備）を並べ替えているだけ。だから根拠のない褒め言葉が混ざらない。
    押すと欄に入る。入れたあとで自由に直せる。
    """
    from hub_core import ad_copy
    drafts = ad_copy.suggest(answered, today=datetime.date.today().isoformat())
    if not drafts:
        lack = ad_copy.missing_for_copy(answered)
        if not lack:
            return ""
        return ('<div class="cs-wrap"><div class="cs-h">売り文句の候補</div>'
                '<div class="cs-empty">まだ候補を作れません。'
                f'{_esc("、".join(lack[:3]))}が入ると作れるようになります。'
                '空欄のままでも先に進めます。</div></div>')
    chips = "".join(
        f'<button type="button" class="cs-chip" data-copy="{_esc(d.text)}">'
        f'<span class="cs-kind">{_esc(d.kind)}</span>'
        f'<span class="cs-text">{_esc(d.text)}</span>'
        f'<span class="cs-basis">根拠：{_esc("・".join(v for _f, v in d.basis if v))}</span>'
        '</button>' for d in drafts)
    return ('<div class="cs-wrap"><div class="cs-h">売り文句の候補'
            '<span class="cs-sub">押すと上の欄に入ります</span></div>'
            f'<div class="cs-chips">{chips}</div>'
            '<div class="cs-note">入力した事実だけで組み立てています。'
            '「抜群」「厳選」「掘出し物件」のように広告で使えない言葉は入りません。</div>'
            '<script>document.querySelectorAll(".cs-chip").forEach(function(b){'
            'b.addEventListener("click",function(){'
            'var i=document.getElementById("w-title_copy");'
            'if(i){i.value=b.getAttribute("data-copy");i.focus();}'
            'document.querySelectorAll(".cs-chip").forEach(function(o){o.classList.remove("on");});'
            'b.classList.add("on");});});</script></div>')


def render_maisoku_step(data_dir: Path, params) -> str:
    """窓口型の1段。聞くのはこの段の項目だけ。前の段の答えは hidden で持ち回る。"""
    from hub_core.auth import load_company
    company = load_company(data_dir, strict=True)
    ocr_prefill, ocr_note = _take_ocr_prefill(
        data_dir, params, ".maisoku_ocr_prefill.json", _ocr_to_maisoku)
    params = _params_with_prefill(params, ocr_prefill)
    idx = _maisoku_step_index((params.get("step", ["basic"])[0] or "basic").strip())
    key, title, fields, help_text = MAISOKU_STEPS[idx]

    # 既に答えた値（クエリで持ち回る）。この段の項目は入力欄に、他は hidden に。
    answered = {k: (params.get(k, [""])[0] or "") for k, _l, _g in MAISOKU_NEW_FIELDS}
    case_id = (params.get("case", [""])[0] or "").strip()

    dots = "".join(
        f'<span class="ms-dot{" on" if i <= idx else ""}"></span>' for i in range(len(MAISOKU_STEPS)))
    bar = (f'<div class="ms-steps"><div class="ms-dots">{dots}</div>'
           f'<div class="ms-count">{idx + 1} / {len(MAISOKU_STEPS)}</div></div>')

    rows = ""
    for k in fields:
        rows += (f'<div class="ms-row"><label class="ms-l" for="w-{k}">{_esc(_MAISOKU_LABEL.get(k, k))}</label>'
                 f'<input class="ms-i" type="text" id="w-{k}" name="{k}" '
                 f'value="{_esc(answered.get(k, ""))}"></div>')
        if k == "title_copy":
            rows += _copy_suggestion_html(answered)
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_esc(v)}">'
        for k, v in answered.items() if k not in fields and v)
    if case_id:
        hidden += f'<input type="hidden" name="case" value="{_esc(case_id)}">'

    last = idx == len(MAISOKU_STEPS) - 1
    action = "/maisoku/new-create" if last else "/maisoku/new-form"
    method = "post" if last else "get"
    nxt = ("" if last else
           f'<input type="hidden" name="step" value="{MAISOKU_STEPS[idx + 1][0]}">')
    go = ("マイソクを作る" if last else "つぎへ")
    back = ('<a class="ms-back" href="/maisoku">やめる</a>' if idx == 0 else
            f'<a class="ms-back" href="/maisoku/new-form?step={MAISOKU_STEPS[idx - 1][0]}'
            + "".join(f"&{k}={quote(v)}" for k, v in answered.items() if v)
            + '">ひとつ前へ</a>')

    obi = (f'<div class="ms-note">帯（取扱業者欄）は業者情報から自動で入ります：'
           f'<b>{_esc(company.get("name") or "（業者情報を登録してください）")}</b> '
           f'{_esc(company.get("license_no") or "")}</div>')

    body = (f'{bar}<h1 class="ms-h">{_esc(title)}</h1>'
            f'<p class="ms-help">{_esc(help_text)}</p>{ocr_note}'
            f'<form class="ms-form" method="{method}" action="{action}">{rows}{hidden}{nxt}'
            f'<div class="ms-actions"><button class="ms-go" type="submit">{go}</button>{back}</div>'
            f'</form>{obi if last else ""}')
    return _wrap_main("maisoku", "/maisoku", "マイソクを作る", f'<div class="ms-wrap">{body}</div>')


def render_maisoku_new(data_dir: Path, params) -> str:
    """マイソク（販売図面）を物件がゼロから作る新規フォーム。帯は業者情報から自動。→survey様式で生成。
    写真から読み取った下書き(.maisoku_ocr_prefill.json)があれば事前入力する（無料ローカルOCR）。"""
    from hub_core.auth import load_company
    from hub_core import maisoku as _ms
    company = load_company(data_dir, strict=True)   # マイソクは公開物。空で進めない
    # 台帳の物件を選んでいれば、登記等から重説フィールド→マイソクフィールドへ写して事前入力
    sel_case = (params.get("case", [""])[0] or "").strip()
    mprefill = {}
    if sel_case:
        mprefill = _ms.from_property_fields(_property_prefill(data_dir, sel_case))
    # 写真OCRの読取結果があれば取り込み（1回きり・読んだら消す）
    ocr_note = ""
    if (params.get("from", [""])[0] or "") == "ocr":
        stash = Path(data_dir) / ".maisoku_ocr_prefill.json"
        if stash.is_file():
            try:
                import json as _j
                st = _j.loads(stash.read_text(encoding="utf-8"))
                mprefill.update({k: v for k, v in _ocr_to_maisoku(st).items() if v})
                n_read = len([v for v in st.values() if v])
                if n_read:
                    ocr_note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #2e7d32">'
                                f'写真から <b>{n_read}項目</b>を読み取って下に入れました'
                                '（無料・ローカル・クラウド送信なし）。<b>値は必ず現物と照合してください</b>（OCRは下書き補助）。</div>')
                else:
                    ocr_note = ('<div class="conn-guide" style="margin:0 0 14px;border-left:3px solid #b45309">'
                                'この画像からは物件情報を読み取れませんでした（<b>推測で埋めません</b>）。'
                                '販売図面の写真を使うか、下に手で入力してください。</div>')
            except (OSError, ValueError):
                pass
            try:
                stash.unlink()
            except OSError:
                pass
    choices = _property_choices(data_dir)
    picker = ""
    if choices:
        opts = '<option value="">（新規に入力）</option>' + "".join(
            f'<option value="{_esc(c["case_id"])}"{" selected" if c["case_id"] == sel_case else ""}>'
            f'{_esc(c["name"])}（{_esc(c["deal"])}）</option>' for c in choices)
        picker = ('<div class="pf-set" style="padding:12px 16px"><label class="pf-l">台帳の物件から読み込む</label>'
                  '<select onchange="location.href=\'/maisoku/new-form?case=\'+encodeURIComponent(this.value)" '
                  'style="padding:8px 11px;border:1px solid var(--line);border-radius:6px;font-size:19px;max-width:420px">'
                  + opts + '</select></div>')
    groups = {}
    for key, label, grp in MAISOKU_NEW_FIELDS:
        groups.setdefault(grp, []).append((key, label))
    blocks = ""
    for grp, items in groups.items():
        fs = "".join(f'<div class="pf-f"><label class="pf-l" for="m-{k}">{_esc(l)}</label>'
                     f'<input type="text" id="m-{k}" name="{k}" value="{_esc(mprefill.get(k, ""))}"></div>' for k, l in items)
        blocks += f'<fieldset class="pf-set"><legend>{_esc(grp)}</legend><div class="pf-grid">{fs}</div></fieldset>'
    obi = ('<div class="pf-set" style="padding:12px 16px"><div class="conn-guide">'
           f'帯（取扱業者欄）は業者情報から自動で入ります：<b>{_esc(company.get("name") or "（/profile で登録）")}</b>'
           f' {_esc(company.get("license_no") or "")}。ブランド色も反映されます。</div></div>')
    # 写真・PDFから自動入力（無料ローカルOCR＝macOS Vision / Windows.Media.Ocr）。クラウドに送らない。
    from hub_core import local_ocr as _loc
    if _loc.available():
        photo = ('<div class="pf-set" style="padding:12px 16px">'
                 '<form method="post" action="/maisoku/from-photo" enctype="multipart/form-data" '
                 'style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
                 '<label class="pf-l" style="margin:0">販売図面の写真・PDFから自動入力</label>'
                 '<input type="file" name="photo" accept="image/*,application/pdf" required '
                 'style="font-size:18px">'
                 '<button class="ri-go ghost" type="submit">読み取ってフォームに入れる</button>'
                 f'<span class="gn">無料・端末内で処理（{_esc(_loc.engine())}）・クラウド送信なし</span>'
                 '</form></div>')
    else:
        photo = ('<div class="pf-set" style="padding:12px 16px"><div class="gn">'
                 '写真からの自動入力は macOS / Windows で使えます（無料ローカルOCR）。'
                 'この環境では手動で入力してください。</div></div>')
    case_hidden = (f'<input type="hidden" name="case" value="{_esc(sel_case)}">'
                   if sel_case else "")
    form = ('<form method="post" action="/maisoku/new-create" class="pf-wrap">'
            + case_hidden + picker + blocks + obi
            + '<div class="pf-actions"><button class="ri-go" type="submit">マイソクの下書きを作成</button>'
            '<a class="ri-qbtn" href="/maisoku" style="margin-left:8px">戻る</a></div></form>')
    inner = (ui.page_head("マイソクの新規作成",
             "物件の情報を入力すると、survey様式の販売図面を作成します。帯は業者情報から自動で入ります。")
             + '<div class="conn-guide" style="margin:0 0 14px">最低限、<b>物件名</b>と<b>価格</b>を入れれば作れます。'
             '台帳の物件を選ぶか、<b>販売図面の写真</b>から自動で入れられます。</div>'
             + ocr_note + photo + form)
    return _wrap_main("maisoku", "/maisoku", "マイソクの新規作成", inner)


_AD_LEVEL_LABEL = {"block": "直さないと出せません", "confirm": "根拠が要ります",
                   "note": "確認してください"}


def _ad_review_panel(data_dir: Path, doc_id: str) -> str:
    """販売図面1枚ぶんの広告表示チェックを、印刷の手前に出す。

    店主が打ったキャッチコピーがそのままA4に焼かれるので、**刷る前に**見せる。
    自動では書き換えない（広告表現の責任は業者にあるため）。言い換え案だけ出す。
    """
    from hub_core import ad_rules, documents
    try:
        cur = documents.get_version(data_dir, doc_id)
        fields = json.loads(cur["body"] or "{}")
    except Exception:
        return ""
    if not isinstance(fields, dict):
        return ""
    from hub_core.auth import load_company
    company = load_company(data_dir)
    from hub_core import maisoku as _msc
    merged = {**fields}
    for k, v in (_msc.company_to_obi(company) if company else {}).items():
        merged.setdefault(k, v)
    issues = ad_rules.review(merged, today=datetime.date.today().isoformat())
    missing = [_msc.MAISOKU_LABELS.get(k, k) for k in _msc.check_required(merged)]

    rows = ""
    for i in issues:
        label = _AD_LEVEL_LABEL.get(i.level, i.level)
        sug = (f'<div class="adc-sug">こう直せます：{_esc(i.suggestion)}</div>'
               if i.suggestion else "")
        rows += (f'<li class="adc-i adc-{_esc(i.level)}">'
                 f'<div class="adc-head"><span class="adc-badge">{_esc(label)}</span>'
                 f'<span class="adc-where">{_esc(ad_rules.FIELD_LABEL.get(i.field, i.field))}</span>'
                 f'<b class="adc-term">{_esc(i.term)}</b></div>'
                 f'<div class="adc-why">{_esc(i.why)}</div>{sug}'
                 f'<div class="adc-src">{_esc(i.source_text)}</div></li>')
    if missing:
        rows += ('<li class="adc-i adc-block"><div class="adc-head">'
                 '<span class="adc-badge">直さないと出せません</span>'
                 '<span class="adc-where">必要表示事項</span></div>'
                 f'<div class="adc-why">広告に必ず載せる項目が空です：{_esc("、".join(missing))}</div>'
                 '<div class="adc-src">不動産の表示に関する公正競争規約施行規則（必要表示事項）</div></li>')

    if not rows:
        head = ('<div class="adc-ok">刷る前の確認：この検査で見つかった問題はありません。</div>')
        body = ""
    else:
        n_block = len(ad_rules.blocking(issues)) + (1 if missing else 0)
        head = (f'<div class="adc-ng">刷る前の確認：{_esc(ad_rules.summary(issues))}'
                + ("" if not missing else " 必要表示事項の欠落もあります。") + "</div>")
        body = f'<ul class="adc-list">{rows}</ul>'
        if n_block:
            body += ('<div class="adc-foot">「直さないと出せません」が残っているうちは、'
                     'この販売図面を配らないでください。'
                     '<a class="ri-go" href="/maisoku/edit?doc=' + quote(doc_id)
                     + '">直しに行く</a></div>')
    return (ui.section("広告としての表示チェック") + '<div class="adc-wrap">'
            + head + body
            + '<div class="adc-note">この検査が見るのは、使えない言葉・徒歩分数や面積の'
            '書き方・必ず載せる項目の3つです。これを通ったことは、広告全体が'
            '規約に適合していることの保証ではありません。</div></div>')


def _maisoku_mobile_summary(data_dir: Path, doc_id: str) -> str:
    """390pxではA4を縮小表示せず、確認すべき業務要点と出力操作を返す。"""
    try:
        import importlib.util
        from hub_core import docgen
        from hub_core import documents, maisoku as _ms
        current = documents.get_version(data_dir, doc_id)
        fields = json.loads(current.get("body") or "{}")
        if not isinstance(fields, dict):
            return ""
    except Exception:
        return ""
    variant = str(fields.get("_xlsx_variant") or "A").upper()
    variant_label = _ms.XLSX_VARIANT_LABELS.get(variant, _ms.XLSX_VARIANT_LABELS["A"])
    q = quote(doc_id)
    facts = (
        ("価格", fields.get("price")),
        ("交通", fields.get("access") or fields.get("nearest_station")),
        ("所在地", fields.get("address")),
        ("間取り", fields.get("floor_plan")),
        ("面積", fields.get("building_area") or fields.get("land_area")),
        ("築年月", fields.get("built")),
    )
    rows = "".join(
        f'<div class="msm-row"><dt>{_esc(label)}</dt><dd>{_esc(value or "未入力")}</dd></div>'
        for label, value in facts
    )
    excel_ready = bool(docgen.MAISOKU_GENSHI.is_file()
                       and importlib.util.find_spec("openpyxl") is not None)
    version = int(current.get("version") or 0)
    xlsx_href = _case_bound_export_href(data_dir, doc_id, version, "xlsx")
    excel_action = (
        f'<a class="ri-go ghost" href="{xlsx_href}">Excelで保存</a>'
        if excel_ready and xlsx_href else ""
    )
    return (
        '<section class="ms-mobile-summary" aria-label="スマートフォン用マイソク要点">'
        '<div class="msm-kicker">このPCに保存・外部送信なし</div>'
        f'<h3 class="msm-name">{_esc(fields.get("property_name") or doc_id)}</h3>'
        f'<div class="msm-variant">{_esc(variant_label)}</div>'
        f'<dl class="msm-facts">{rows}</dl>'
        '<div class="msm-actions">'
        f'<a class="ri-go" href="/doc/preview?doc={q}" target="_blank" rel="noopener">全体を確認・拡大</a>'
        f'{excel_action}'
        f'<a class="ri-go ghost" href="/doc/preview?doc={q}&amp;publish=1" target="_blank" rel="noopener">PDFで保存（印刷）</a>'
        '</div><p class="msm-note">A4紙面はこの画面では縮小表示しません。全体確認を開くと、拡大とブラウザのPDF保存ができます。</p>'
        '</section>'
    )


def render_maisoku(data_dir: Path, params) -> str:
    """マイソク — 保存済み販売図面の実物プレビュー＋一覧＋物件からの新規作成＋会話作成導線。"""
    mai = _docs_of_kind(data_dir, "maisoku")
    _, cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
    from hub_core import maisoku as _msv
    kpis = _kpis_html([
        ("", len(mai), "作成したマイソク", "/maisoku"),
        ("", len(cases), "台帳の物件", "/properties"),
        ("red" if max(0, len(cases) - len(mai)) else "", max(0, len(cases) - len(mai)), "マイソク未作成", "/properties"),
    ])
    # 実物プレビュー: 選択 or 先頭の販売図面を iframe で inline 表示(実際のマイソクが見える)
    sel = (params.get("doc", [""])[0] or "").strip()
    sel_doc = next((d for d in mai if d["doc_id"] == sel), (mai[0] if mai else None))
    preview = ""
    if sel_doc:
        tabs = "".join(
            f'<a class="chip {"on" if d["doc_id"] == sel_doc["doc_id"] else ""}" '
            f'href="/maisoku?doc={quote(d["doc_id"])}">{_esc(d["doc_id"])}</a>' for d in mai)
        q = quote(sel_doc["doc_id"])
        preview = (
            ui.section("実物プレビュー（販売図面）")
            + f'<div class="ms-tabs">{tabs}</div>'
            + '<div class="ms-preview-tools">'
            + f'<a class="ri-go ms-preview-open" href="/doc/preview?doc={q}" '
            + 'target="_blank" rel="noopener">販売図面を大きく開く</a>'
            + '<span class="ms-preview-note">別画面で拡大して、文字と写真を確認できます。</span></div>'
            + _maisoku_mobile_summary(data_dir, sel_doc["doc_id"])
            + f'<iframe class="ms-frame" src="/doc/preview?doc={q}" title="マイソク プレビュー"></iframe>'
            + f'<div class="ms-pa"><a class="ri-go" href="/maisoku/edit?doc={q}">様式を編集</a>'
            + f'<a class="ri-go ghost" href="/doc/preview?doc={q}" target="_blank" rel="noopener">プレビュー（別タブ）</a></div>'
            + _doc_output_buttons(sel_doc["doc_id"], "maisoku")
            + _ad_review_panel(data_dir, sel_doc["doc_id"]))
    else:
        preview = ui.empty("保存済みのマイソクがありません。下のいずれかで作成してください。")

    lib = ui.section("保存済みマイソク一覧") + _doc_library_html(
        data_dir, "maisoku", open_label="販売図面を開く",
        empty_msg="まだありません。")
    # 物件から新規作成(POST=書込はPOST経由・その物件の販売図面ドラフトを生成して保存)
    make_rows = "".join(
        '<form class="ri-card" method="post" action="/maisoku/new">'
        f'<input type="hidden" name="case" value="{_esc(r.get("案件ID") or "")}">'
        f'<div class="ct">{_esc(r.get("物件名") or "(物件名未設定)")}</div>'
        f'<div class="cm">{_esc(r.get("取引種別") or "")} ／ {_esc(r.get("案件ID") or "")}</div>'
        '<button class="ri-go" type="submit" style="margin-top:11px">マイソクを生成</button></form>'
        for r in cases if (r.get("案件ID") or "").strip())
    make_block = (f'<div class="ri-grid2">{make_rows}</div>' if make_rows
                  else ui.empty("物件(cases)がありません。"))
    blank_start = (
        '<div class="ri-quick" style="margin:2px 0 16px"><span class="ri-quick-l">物件がまだ無い場合</span>'
        '<a class="ri-go" href="/maisoku/new-form">ゼロから作成</a></div>')
    guide = (
        '<div class="ri-guide"><div class="gh">マイソクの作り方</div>'
        '<div class="gb">上の「物件からマイソク生成」で台帳の物件から販売図面の下書きを作成・保存できます。'
        'または「ことばで頼む」から「<b>中野の物件のマイソク下書きを作って</b>」と話しかけてください。'
        '生成物は<b>整形プレビュー → 編集 → 版管理</b>でき、公開は '
        '<a href="/ads">公開ゲート</a> で人間が確認・記名してから。HTML→PDF はブラウザ印刷でローカル完結。</div></div>')
    usage = _usage_strip(
        "販売図面（マイソク）の実物プレビュー・一覧・作成を行う画面です。印刷でPDF化できます。",
        "台帳の物件情報や、写真から読み取った下書き（無料ローカルOCR）をもとに作成します。"
        "帯（業者情報）は会社設定から自動で入ります。",
        ["「物件からマイソク生成」で下書きを作る",
         "実物プレビューを開いて内容を確認する",
         "印刷（PDF）やExcel原紙で出力する"],
        [("/maisoku/new-form", "マイソクを新規作成"), ("/juusetsu", "重説"), ("/properties", "物件")])
    inner = (ui.page_head("マイソク", "販売図面（マイソク）の実物プレビュー・一覧・作成。印刷でPDF化。")
             + usage + kpis + preview + lib
             + ui.section("物件からマイソク生成") + blank_start + make_block
             + ui.section("会話で作成") + guide)
    return _wrap_main("maisoku", "/maisoku", "マイソク", inner)


def make_maisoku_from_case(data_dir: Path, case_id: str):
    """cases.csv の1物件から販売図面ドラフト(様式フィールドjson)を生成・保存し doc_id を返す。"""
    from hub_core import documents, maisoku as _ms
    _, cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
    row = next((r for r in cases if (r.get("案件ID") or "") == case_id), None)
    if row is None:
        return None
    name = row.get("物件名") or case_id
    fields = _ms.default_fields()
    fields.update({
        "property_type": (row.get("取引種別") or "") + "物件" if row.get("取引種別") else "物件",
        "property_name": name,
        "property": name,
        "torihiki_taiyo": "媒介",
        "price": "（要入力）", "address": "（要入力）", "access": "（要入力）",
        "bikou": "物件台帳から生成した販売図面の下書きです。各項目を入力し、間取り図・地図・写真を差し込み、公開前に宅建士が確認してください。",
    })
    # 帯（取扱業者欄）は業者プロフィール(company.json)から自動fill＝設定画面で一度入れれば全書類に反映。
    fields = _ms.fields_with_company(fields, load_company(data_dir, strict=True))
    if not str(fields.get("company_name") or "").strip():
        fields["company_name"] = "（/setup で会社情報を登録）"
    if not str(fields.get("license") or "").strip():
        fields["license"] = "（免許番号を登録）"
    fields["_variant"] = "standard"
    fields["_xlsx_variant"] = "A"
    did = "MS-" + (case_id or name)
    documents.save_version(data_dir, did, json.dumps(fields, ensure_ascii=False, indent=2),
                           kind="maisoku", fmt="txt", author="あいのて(物件から生成)",
                           case_id=case_id)
    return did


_MAISOKU_PHOTO_LABELS = {
    "photo_main": "メイン写真",
    "photo_sub1": "写真1",
    "photo_sub2": "写真2",
    "photo_sub3": "写真3",
    "photo_floorplan": "間取り図",
    "photo_map": "案内図・地図",
}


def _eligible_maisoku_assets(data_dir: Path, prop: str) -> list[dict]:
    """同一物件かつ広告利用権を検証できた画像だけを編集候補に返す。"""
    if not prop:
        return []
    from hub_core import provenance, vault

    eligible = []
    for asset in vault.scan(data_dir):
        if str(asset.get("property") or "") != prop:
            continue
        ref = str(asset.get("asset_key") or "")
        if Path(ref).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        resolved = provenance._resolve_vault_file(data_dir, ref)
        if resolved is None:
            continue
        try:
            provenance.check_asset(data_dir, resolved, prop, "advertise")
        except provenance.ProvenanceError:
            continue
        eligible.append(asset)
    return eligible


def render_maisoku_edit(data_dir: Path, params) -> str:
    """マイソク 雛形フォーム編集: 様式の各項目をフォームで埋め、保存すると販売図面に反映(右でライブプレビュー)。"""
    from hub_core import documents, maisoku as _ms
    doc_id = (params.get("doc", [""])[0] or "").strip()
    if not doc_id:
        inner = (ui.page_head("マイソク編集", "編集するマイソクを選んでください。")
                 + ui.empty("マイソクが選択されていません。マイソク一覧から編集対象を開いてください。")
                 + '<p><a class="ri-go" href="/maisoku">マイソク一覧へ</a></p>')
        return _wrap_main("maisoku", "/maisoku/edit", "マイソク編集", inner)

    viewer = current_viewer()
    if viewer is not None:
        from hub_core.access import document_access_allowed
        if not document_access_allowed(data_dir, viewer, doc_id):
            inner = (ui.page_head("マイソクを編集できません", "保存データを読み取れませんでした。")
                     + '<div class="ri-alert err">このマイソクは見つからないか、担当範囲外です。</div>'
                     + '<p><a class="ri-go" href="/maisoku">マイソク一覧へ</a></p>')
            return _wrap_main("maisoku", "/maisoku/edit", "マイソク編集", inner)

    fields = _ms.default_fields()
    data = {}
    try:
        cur = documents.get_version(data_dir, doc_id)
        data = json.loads(cur["body"] or "{}")
        if not isinstance(data, dict):
            raise ValueError("maisoku body is not an object")
    except Exception:
        inner = (ui.page_head("マイソクを編集できません", "保存データを読み取れませんでした。")
                 + '<div class="ri-alert err">このマイソクの保存データを確認できません。元の版を変更せず、管理責任者へ連絡してください。</div>'
                 + '<p><a class="ri-go" href="/maisoku">マイソク一覧へ</a></p>')
        return _wrap_main("maisoku", "/maisoku/edit", "マイソク編集", inner)

    variant = data.get("_variant", _ms.DEFAULT_VARIANT)
    if variant not in _ms.VARIANTS:
        variant = _ms.DEFAULT_VARIANT
    for k in fields:
        fields[k] = data.get(k, "")
    prop = str(data.get("property") or data.get("property_name") or "").strip()
    photo_assets = _eligible_maisoku_assets(data_dir, prop)
    sections = []
    for grp, items in _ms.field_groups():
        rows = []
        for key, label, ml in items:
            val = fields.get(key, "")
            if ml:
                inp = f'<textarea name="{key}" rows="2">{_esc(val)}</textarea>'
            else:
                inp = f'<input type="text" name="{key}" value="{_esc(val)}">'
            rows.append(f'<label class="mf-row"><span class="mf-l">{_esc(label)}</span>{inp}</label>')
        sections.append(f'<div class="mf-sec"><div class="ri-sech">{_esc(grp)}</div>{"".join(rows)}</div>')
    vopts = "".join(f'<option value="{_esc(v)}"{" selected" if v == variant else ""}>{_esc(_ms.VARIANT_LABELS[v])}</option>'
                    for v in _ms.VARIANTS)
    xlsx_variant = str(data.get("_xlsx_variant") or "A").upper()
    if xlsx_variant not in _ms.XLSX_VARIANTS:
        xlsx_variant = "A"
    xopts = "".join(
        f'<option value="{v}"{" selected" if v == xlsx_variant else ""}>'
        f'{_esc(_ms.XLSX_VARIANT_LABELS[v])}</option>'
        for v in _ms.XLSX_VARIANTS
    )
    photo_rows = []
    for slot in _ms.PHOTO_SLOTS:
        selected = str(data.get(slot) or "").strip()
        options = ['<option value="">使用しない</option>']
        known = {str(a.get("asset_key") or "") for a in photo_assets}
        if selected and selected not in known:
            options.append(f'<option value="{_esc(selected)}" selected>{_esc(Path(selected).name)}（利用確認待ち）</option>')
        for asset in photo_assets:
            ref = str(asset.get("asset_key") or "")
            options.append(
                f'<option value="{_esc(ref)}"{" selected" if ref == selected else ""}>'
                f'{_esc(asset.get("filename") or Path(ref).name)}</option>')
        photo_rows.append(
            '<label class="mf-row"><span class="mf-l">'
            + _esc(_MAISOKU_PHOTO_LABELS.get(slot, slot))
            + f'</span><select name="{_esc(slot)}">{"".join(options)}</select></label>')
    photo_block = (
        '<div class="mf-sec"><div class="ri-sech">掲載する写真・図面</div>'
        + ("".join(photo_rows) if photo_assets or any(data.get(s) for s in _ms.PHOTO_SLOTS)
           else '<div class="gn">この物件で広告利用を確認済みの画像はありません。物件素材で自社撮影・作成または利用許諾を登録してください。</div>')
        + '<div style="margin-top:8px"><a class="ri-go ghost" href="/materials?property='
        + quote(prop) + '">物件素材を確認</a></div></div>')
    accent = str(data.get("_accent") or (load_company(data_dir, strict=True) or {}).get("brand_color") or _ms.DEFAULT_ACCENT)
    if not _ms._valid_hex(accent):
        accent = _ms.DEFAULT_ACCENT
    font_labels = dict(FONT_LABELS)
    form = (
        '<form class="mf-form" method="post" action="/maisoku/edit">'
        f'<input type="hidden" name="doc_id" value="{_esc(doc_id)}">'
        f'<input type="hidden" name="base_version" value="{_esc(cur["meta"].get("version", ""))}">'
        '<div class="mf-top"><label class="mf-row" style="grid-template-columns:60px 200px">'
        f'<span class="mf-l">様式</span><select name="_variant">{vopts}</select></label>'
        + '<label class="mf-row" style="grid-template-columns:72px 230px">'
        + f'<span class="mf-l">Excel</span><select name="_xlsx_variant">{xopts}</select></label>'
        + '<label class="mf-inline"><span class="mf-l">ブランド色</span>'
        + f'<input type="color" name="_accent" value="{_esc(accent)}" style="width:64px;height:40px"></label>'
        + '<label class="mf-inline"><span class="mf-l">書体</span><select name="_font">'
        + "".join(f'<option value="{fk}"{" selected" if (data.get("_font") or "gothic")==fk else ""}>{_esc(font_labels[fk])}</option>' for fk in _ms.DISPLAY_FONTS)
        + '</select></label>'
        '<button class="ri-btn" type="submit">保存して様式に反映</button></div>'
        + photo_block + "".join(sections)
        + '<div class="mf-actions"><button class="ri-btn" type="submit">保存</button>'
        f'<a class="ri-go ghost" href="/maisoku?doc={quote(doc_id)}">マイソク一覧へ戻る</a></div></form>')
    if doc_id:
        prev = (ui.section("ライブプレビュー（保存で更新）")
                + '<div class="ms-preview-tools">'
                + f'<a class="ri-go ms-preview-open" href="/doc/preview?doc={quote(doc_id)}" '
                + 'target="_blank" rel="noopener">販売図面を大きく開く</a>'
                + '<span class="ms-preview-note">別画面で拡大して、文字と写真を確認できます。</span></div>'
                + _maisoku_mobile_summary(data_dir, doc_id)
                + f'<iframe class="ms-frame" src="/doc/preview?doc={quote(doc_id)}" title="プレビュー"></iframe>'
                + f'<div class="ms-pa"><a class="ri-go" href="/doc/preview?doc={quote(doc_id)}" '
                'target="_blank" rel="noopener">別タブで全画面・印刷(PDF)</a></div>')
    else:
        prev = ui.empty("doc が指定されていません。マイソク一覧から編集対象を選んでください。")
    caution = ('<div class="ri-card" style="border-color:var(--warn-bg);background:var(--warn-bg);'
               'color:var(--warn);margin-bottom:14px;font-size:18px;line-height:1.7">'
               '<b>帯（取扱業者欄）＝必要表示事項</b>。広告可否（広告転載区分）・帯替え可否は'
               '<b>元付の承諾事項</b>で、物確で必ず確認を。広告不可物件の無断掲載・成約済みの掲載継続は'
               'レインズ利用規程／宅建業法・表示規約違反（おとり広告）になります。</div>')
    saved_notice = ('<div class="ri-alert ok" role="status">新しい版として保存しました。</div>'
                    if (params.get("saved", [""])[0] or "") == "1" else "")
    inner = (ui.page_head("マイソク編集（雛形フォーム）",
                          f"{_esc(doc_id or '(未選択)')} — 各項目を入力し、保存すると標準様式（販売図面）に反映されます。")
             + saved_notice + caution
             + '<div class="mf-grid"><div class="mf-left">' + form + '</div>'
             '<div class="mf-right">' + prev + '</div></div>')
    return _wrap_main("maisoku", "/maisoku/edit", "マイソク編集", inner)




# ---- いまの名簿を取り込む（移行）-------------------------------------------------
# 「使い始めるには全部入力し直し」では乗り換えられない。手元のExcel/CSVをそのまま読む。
# いきなり台帳に書かず、**先に何がどう入るかを見せてから**押してもらう。


# 下見したファイルを、押されるまで一時的に持っておく（同じファイルを二度上げさせない）。
# プロセス内のみ・上限つき。再起動すれば消える（消えても「選び直してください」と案内する）。
_MIGRATION_STASH: dict[str, tuple[bytes, str]] = {}
_MIGRATION_MAX = 4


def _stash_migration(raw: bytes, filename: str) -> str:
    import hashlib
    token = hashlib.sha256(raw[:65536] + filename.encode("utf-8")).hexdigest()[:16]
    if len(_MIGRATION_STASH) >= _MIGRATION_MAX:
        _MIGRATION_STASH.pop(next(iter(_MIGRATION_STASH)), None)
    _MIGRATION_STASH[token] = (raw, filename)
    return token


def _take_migration(token: str):
    return _MIGRATION_STASH.pop(token, None)


def render_migrate(data_dir: Path, params) -> str:
    viewer = current_viewer()
    can_edit = bool(viewer and viewer.role in ("責任者", "代表"))
    msg_raw = (params.get("msg", [""])[0] or "").strip()
    msg = _public_notice_param(msg_raw, "取り込み処理を完了できませんでした。") if msg_raw else ""
    head = ui.page_head(
        "いまのお客様名簿を取り込む",
        "いま使っている Excel や CSV を、そのまま読み込めます。"
        "入力し直す必要はありません。")
    note = ('<div class="ri-guide"><div class="gh">読み込めるもの</div>'
            '<div class="gb">Excel（.xlsx）と CSV。1行目に「氏名」「電話」などの見出しがある形。'
            '「氏名」「お客様名」「名前」はどれもお名前として読みます。'
            '当てはまらない列も捨てずに備考として残します。<br>'
            '<b>いきなり台帳には入りません。</b>先に「何件がどう入るか」をお見せします。</div></div>')
    alert = f'<div class="ri-alert ok">{_esc(msg)}</div>' if msg else ""
    if not can_edit:
        return _wrap_main("customers", "/customers", "名簿の取り込み",
                          head + note + ui.empty("取り込みは責任者・代表のみが行えます。"))
    form = (
        '<form class="mig-form" method="post" action="/migrate/preview" '
        'enctype="multipart/form-data">'
        '<div class="ms-row"><span class="ms-l">名簿のファイル</span>'
        '<div class="ms-file-row"><label class="ms-file-pick" for="mgFile">ファイルを選ぶ'
        '<input class="sr-only" type="file" id="mgFile" name="file" '
        'accept=".csv,.xlsx,.xlsm,text/csv" required '
        'onchange="document.getElementById(\'mgFileName\').textContent=this.files.length?this.files[0].name:\'まだ選んでいません\'">'
        '</label><span class="ms-file-name" id="mgFileName">まだ選んでいません</span></div></div>'
        '<div class="ms-row"><label class="ms-l" for="mgTool">いままで何で管理していましたか</label>'
        '<input class="ms-i" type="text" id="mgTool" name="source_tool" '
        'placeholder="Excel／別の管理ソフトの名前など"></div>'
        '<div class="ms-actions"><button class="ms-go" type="submit">中身を見てみる</button>'
        '<a class="ms-back" href="/customers">やめる</a></div></form>')
    return _wrap_main("customers", "/customers", "名簿の取り込み",
                      f'{head}{alert}{note}<div class="ms-wrap">{form}</div>')


def render_migrate_preview(data_dir: Path, plan: dict, *, filename: str,
                           source_tool: str) -> str:
    """取り込む前の下見。ここでは台帳に何も書いていない。"""
    ready, skipped = plan["ready"], plan["skipped"]
    display_filename = _visible_data_value("名称", Path(str(filename or "")).name) or "選択したファイル"
    rows = "".join(
        f'<div class="mig-row"><span class="mig-name">{_esc(r["顧客名"])}</span>'
        f'<span class="mig-c">{_esc(r["連絡先"] or "連絡先なし")}</span></div>'
        for r in ready[:20])
    more = (f'<div class="cu-src">ほか {len(ready) - 20} 名</div>' if len(ready) > 20 else "")
    skip_html = ""
    if skipped:
        items = "".join(f'<li>{n}行目: {_esc(why)}</li>' for n, why in skipped[:20])
        skip_html = ('<div class="ri-sech" style="margin-top:18px">入らない行（理由つき）</div>'
                     f'<ul class="mig-skip">{items}</ul>')
    unmapped = plan.get("unmapped") or []
    un = ("" if not unmapped else
          '<div class="cu-src">見出しが分からなかった列（備考として残します）: '
          + _esc("・".join(unmapped[:8])) + "</div>")
    body = (ui.page_head("この内容で取り込みます",
                         f"{display_filename} を読みました。まだ台帳には入れていません。")
            + f'<div class="ri-sech">入る方 {len(ready)} 名</div>'
            + (f'<div class="mig-list">{rows}</div>{more}' if ready else
               ui.empty("入れられる行がありませんでした。"))
            + un + skip_html
            + '<form method="post" action="/migrate/apply" class="ms-form" '
              'style="margin-top:22px">'
            + f'<input type="hidden" name="token" value="{_esc(plan["_token"])}">'
            + f'<input type="hidden" name="source_tool" value="{_esc(source_tool)}">'
            + '<div class="ms-actions">'
            + (f'<button class="ms-go" type="submit">この {len(ready)} 名を取り込む</button>'
               if ready else "")
            + '<a class="ms-back" href="/migrate">ファイルを選び直す</a></div></form>')
    return _wrap_main("customers", "/customers", "取り込みの確認", body)


def _customer_cards(custs: list, cases_rows: list,
                    property_choices: list[tuple[str, str]]) -> str:
    """お客様を、**そのお客様とのお取引の履歴つき**で見せる。

    同じ方が賃貸で借りたあと購入することは実務で普通にある。1件だけ見て「新規の方」と
    扱ってしまうと、いちばん大事なお客様を取りこぼす。何回目か・どんな取引だったかを出す。
    """
    if not custs:
        return ui.empty("まだお客様がいません。「いまの名簿を取り込む」から持ってこられます。")
    from hub_core.operations import OP_ROLES
    viewer = current_viewer()
    can_create_case = bool(
        viewer and viewer.role in OP_ROLES.get("customer_case_create", set()))
    by_cust: dict[str, list] = {}
    for c in cases_rows:
        cid = str(c.get("顧客ID") or "").strip()
        if cid:
            by_cust.setdefault(cid, []).append(c)
    out = []
    for r in custs:
        cid = str(r.get("顧客ID") or "").strip()
        name = r.get("顧客名") or "(お名前未設定)"
        deals = by_cust.get(cid, [])
        kinds = [_deal_label(str(d.get("取引種別") or "").strip()) for d in deals]
        n = len(deals)
        repeat = ""
        if n >= 2:
            uniq = sorted({k for k in kinds if k})
            what = "と".join(uniq) if uniq else ""
            repeat = (f'<span class="cu-repeat">{n}回目のお取引'
                      + (f"（{_esc(what)}）" if what else "") + "</span>")
        elif n == 1:
            repeat = f'<span class="cu-once">{_esc(kinds[0] or "取引")} 1件</span>'
        hist = ""
        if deals:
            items = "".join(
                f'<a class="cu-deal" href="/case?id={quote(str(d.get("案件ID") or ""))}">'
                f'<span class="cu-kind">{_esc(_deal_label(str(d.get("取引種別") or "取引")))}</span>'
                f'{_esc(str(d.get("物件名") or "(物件名未設定)"))}'
                f'{_status_badge(str(d.get("状態") or ""))}</a>' for d in deals)
            hist = f'<div class="cu-deals">{items}</div>'
        origin = str(r.get("元ツール") or "").strip()
        public_origin = _visible_data_value("元ツール", origin)
        src = f'<div class="cu-src">{_esc(public_origin)}から取り込み</div>' if public_origin else ""
        create_case = ""
        if can_create_case and cid:
            options = "".join(
                f'<option value="{_esc(property_id)}">{_esc(label)}</option>'
                for property_id, label in property_choices)
            property_field = (
                '<label>登録済み物件<select name="property_id">'
                '<option value="">まだ物件を決めない</option>' + options
                + '</select></label>'
                if options else
                '<label>物件名（未定なら空欄）<input name="property_name" '
                'placeholder="例：駅前レジデンス 101"></label>'
            )
            create_case = (
                '<details style="margin-top:10px"><summary>この方の新しいお取引を始める</summary>'
                '<form method="post" action="/op" class="ri-actform" style="margin-top:8px">'
                '<input type="hidden" name="op" value="customer_case_create">'
                f'<input type="hidden" name="customer_id" value="{_esc(cid)}">'
                '<label>取引<select name="deal_type">'
                '<option value="lease_tenant">賃貸を借りる</option>'
                '<option value="sale_buyer">購入する</option>'
                '<option value="lease_landlord">賃貸を募集する</option>'
                '<option value="sale_seller">売却する</option>'
                '</select></label>'
                + property_field +
                '<button class="ri-go" type="submit">案件を作る</button></form></details>')
        out.append(
            f'<div class="cu"><div class="cu-head"><span class="cu-name">{_esc(name)}</span>'
            f'{repeat}{_status_badge(r.get("状態") or "")}</div>{hist}{src}{create_case}</div>')
    return f'<div class="cu-list">{"".join(out)}</div>'


def render_customers(data_dir: Path, params) -> str:
    """顧客 — customers.csv（顧客台帳）＋ portal_leads.csv（反響）。連絡先(PII)は表示しない。"""
    _, custs = _load_rows_for_ui(data_dir, "csv:customers.csv")
    _, leads = _load_rows_for_ui(data_dir, "csv:portal_leads.csv")
    _, cases_rows = _load_rows_for_ui(data_dir, "csv:cases.csv")
    property_choices: list[tuple[str, str]] = []
    try:
        from hub_core.store import SqliteStore
        db = Path(data_dir) / "hub.db"
        props = SqliteStore(db).query("properties") if db.is_file() else []
        name_by_property = {
            str(row.get("物件ID") or row.get("property_id") or "").strip():
            str(row.get("物件名") or row.get("property_name") or "").strip()
            for row in cases_rows
            if str(row.get("物件ID") or row.get("property_id") or "").strip()
        }
        property_choices = [
            (str(row.get("property_id") or "").strip(),
             name_by_property.get(str(row.get("property_id") or "").strip())
             or str(row.get("address") or "").strip())
            for row in props if str(row.get("property_id") or "").strip()
        ]
    except Exception:
        property_choices = []
    new_leads = sum(1 for r in leads if str(_pick(r, "返信ゲート", "reply_gate") or "").lower()
                    in ("hold", "pending", ""))
    kpis = _kpis_html([
        ("blue", len(custs), "顧客", "/customers"),
        ("green", len(leads), "反響(累計)", "/leads"),
        ("org", new_leads, "未返信の反響", "/inbox"),
        ("blue", len(cases_rows), "案件", "/properties"),
    ])
    ctable = _customer_cards(custs, cases_rows, property_choices)
    lcards = []
    for r in leads[:8]:
        who = _pick(r, "顧客名", "customer_name") or "(匿名)"
        plat = _pick(r, "ポータル", "platform") or ""
        kind = _pick(r, "問い合わせ種別", "inquiry_type") or ""
        gate = _pick(r, "返信ゲート", "reply_gate") or ""
        badge = _status_badge("hold" if str(gate).lower() in ("hold", "pending") else "ok")
        lead_id = _pick(r, "反響ID", "portal_lead_id") or ""
        convert = _op_button("lead_convert", {"portal_lead_id": lead_id}, "顧客化（案件を作成）", current_viewer())
        lcards.append(
            f'<div class="ri-card"><div class="ct">{_esc(who)} {badge}</div>'
            f'<div class="cm">{_esc(_jp(plat))} ／ {_esc(_jp(kind))} ／ 返信ゲート: {_esc(_jp(gate) or "—")}</div>'
            f'{convert}</div>')
    leads_block = f'<div class="ri-grid2">{"".join(lcards)}</div>' if lcards else ui.empty("反響はありません。")
    detail = ('<div class="ri-examples" style="margin-top:4px">'
              '<a class="ri-chip" href="/leads">反響一覧 →</a>'
              '<a class="ri-chip" href="/inbox">新着の分類 →</a>'
              '<a class="ri-chip" href="/viewings">内見 →</a>'
              '<a class="ri-chip" href="/applications">申込・資料請求 →</a></div>')
    pipe = _pipeline_board(_pipeline_rows(data_dir))
    inner = (ui.page_head("顧客", "顧客台帳と反響の一覧。連絡先（電話・メール）は画面に表示しません。")
             + '<div style="margin:2px 0 14px"><a class="ri-go" href="/migrate">'
               'いまのお客様名簿を取り込む</a></div>'
             + kpis
             + ui.section("パイプライン（要対応・温度感順）") + pipe
             + ui.section("顧客一覧") + ctable
             + ui.section("反響（最近）") + leads_block + detail)
    return _wrap_main("customers", "/customers", "顧客", inner)


def _audit_visible_value(value, field: str = "") -> str:
    """監査画面へ内部の絶対パスを出さず、人が確認できる値だけ返す。"""
    raw = str(value or "").strip()
    key = str(field or "").strip().lower()
    if key in {"target", "source_ref", "source", "参照元"}:
        low = raw.lower()
        if low.startswith(("ri-chousa", "chousa")):
            return "物件調査データ"
        if low.startswith("mail:") or low.startswith("portal:"):
            return "問い合わせデータ"
    return _visible_data_value(field, value)


_AUDIT_INGEST_LABELS = {
    "chousa": "物件調査", "claims": "クレーム", "crm": "顧客", "docmgmt": "書類管理",
    "governance": "権限・資格", "gyosei": "行政調査", "kaikei": "会計", "kanri": "管理",
    "keiyaku": "契約", "maisoku": "マイソク", "media": "広告", "ocr": "OCR",
    "report": "報告書", "satei": "査定", "setup": "初期設定", "shinsa": "審査", "zaiko": "在庫",
}
_AUDIT_ACTION_LABELS = {
    "case_created": "案件を作成", "task_done": "タスクを完了",
    "finalized_with_signature": "書類を記名確定", "save_document": "書類の版を保存",
    "portal_lead_ingested": "問い合わせを取り込み", "hub_ingest_run": "一括取り込みを実行",
    "property_registered": "物件を登録", "customers_imported": "顧客名簿を取り込み",
    "customer_case_created": "顧客と物件を接続", "connection_tested": "LINE接続を確認",
}


def _audit_action_label(action: str) -> str:
    raw = str(action or "").strip()
    if raw in _AUDIT_ACTION_LABELS:
        return _AUDIT_ACTION_LABELS[raw]
    if raw in _AGENT_ACTIONS:
        return _AGENT_ACTIONS[raw][0]
    if raw.endswith("_outputs_ingested"):
        source = raw.removesuffix("_outputs_ingested")
        return f'{_AUDIT_INGEST_LABELS.get(source, "データ")}を取り込み'
    if raw and re.fullmatch(r"[a-z0-9_.:/-]+", raw.lower()):
        return "システム操作"
    return raw or "—"


def _audit_time_label(value: str) -> str:
    return _display_datetime(value)


def _audit_actor_label(value: str) -> str:
    raw = str(value or "").strip()
    mapped = {"ri-hub": "あいのて", "setup": "初回設定", "system": "システム"}.get(raw)
    if mapped:
        return mapped
    if raw and re.fullmatch(r"[a-z0-9_.:/-]+", raw.lower()) and any(
            token in raw.lower() for token in ("webhook", "agent", "mcp", "system", "operations")):
        return "システム"
    return raw or "—"


def _audit_status_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapped = {"pass": "正常", "hold": "保留", "pending": "確認待ち",
              "failed": "失敗", "error": "失敗"}.get(raw)
    if mapped:
        return mapped
    if raw and re.fullmatch(r"[a-z0-9_.:/-]+", raw):
        return "未確認"
    return raw or "—"


def _verified_audit_rows(data_dir: Path) -> list[dict]:
    """監査モジュールで検証したJSONLだけを、壊れた行を捨てずに読み返す。"""
    from hub_core.audit import AuditChainError, verify_audit_chain

    log = Path(data_dir) / "audit_log.jsonl"
    broken = verify_audit_chain(log)
    if broken:
        raise AuditChainError("監査ログの整合性を確認できません。")
    if not log.is_file():
        return []

    rows = []
    try:
        text = log.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuditChainError("監査ログを読み取れません。") from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditChainError(
                f"監査ログのJSON行を読み取れません: line={line_no}") from exc
        if not isinstance(row, dict):
            raise AuditChainError(
                f"監査ログの行形式を確認できません: line={line_no}")
        rows.append(row)
    return rows


def render_audit_status(data_dir: Path, params) -> tuple[int, str]:
    """監査チェーンをfail-closedで検証し、安全な状態表示だけを返す。"""
    from hub_core.audit import AuditChainError

    try:
        rows = _verified_audit_rows(data_dir)
    except (AuditChainError, OSError, UnicodeError, TypeError, ValueError):
        inner = (
            ui.page_head("監査ログ", "記録の改ざん・欠損を検知する台帳です。")
            + '<section class="ri-alert err" role="alert" aria-labelledby="audit-stop-title">'
              '<h2 id="audit-stop-title">監査ログの整合性を確認できません</h2>'
              '<p>改ざん、欠損、または読み取り障害の可能性があるため、'
              '<b>業務操作を停止しています</b>。</p>'
              '<p>監査ログと同時刻のバックアップを変更せず保全し、管理責任者へ連絡してください。'
              '自動修復は行いません。復旧判断まではファイルの編集・削除・再生成をしないでください。</p>'
              '</section>'
        )
        return 409, _wrap_main("audit", "/audit", "監査ログ", inner)

    if not rows:
        inner = (
            ui.page_head("監査ログ", "記録の改ざん・欠損を検知する台帳です。")
            + '<section class="ri-alert" role="status">'
              '<h2>監査記録はまだありません</h2>'
              '<p>最初の確定操作を行うと、ここに検証可能な記録が追加されます。</p>'
              '</section>'
        )
        return 200, _wrap_main("audit", "/audit", "監査ログ", inner)

    q = _public_display_param(params.get("q", [""])[0]).casefold()
    visible = []
    for row in rows:
        target = row.get("target") or row.get("source_ref") or ""
        keyed_values = (
            ("seq", row.get("seq")), ("timestamp", row.get("timestamp")),
            ("actor", row.get("actor")), ("action", row.get("action")),
            ("target" if row.get("target") else "source_ref", target),
            ("gate_status", row.get("gate_status")),
        )
        search_values = [str(value or "") for _key, value in keyed_values]
        raw_values = [_audit_visible_value(value, key) for key, value in keyed_values]
        values = [raw_values[0] or "—", _audit_time_label(raw_values[1]),
                  _audit_actor_label(raw_values[2]), _audit_action_label(raw_values[3]),
                  raw_values[4] or "—", _audit_status_label(raw_values[5])]
        if q and q not in " ".join(search_values + raw_values + values).casefold():
            continue
        visible.append((raw_values, values))

    event_rows = "".join(
        '<tr>'
        + f'<td class="audit-seq" data-label="連番">{_esc(values[0])}</td>'
        + f'<td class="audit-time" data-label="日時">{_esc(values[1])}</td>'
        + f'<td class="audit-actor" data-label="実行者">{_esc(values[2])}</td>'
        + f'<td class="audit-action" data-label="操作">{_esc(values[3])}</td>'
        + f'<td data-label="対象">{_esc(values[4])}</td>'
        + f'<td class="audit-status" data-label="状態">{_esc(values[5])}</td>'
        + '</tr>'
        for raw_values, values in reversed(visible[-100:])
    )
    if event_rows:
        events = (
            '<div class="ai-specwrap"><table class="ai-spec audit-events">'
            '<thead><tr><th>連番</th><th>日時</th><th>実行者</th>'
            '<th>操作</th><th>対象</th><th>状態</th></tr></thead>'
            f'<tbody>{event_rows}</tbody></table></div>'
        )
    else:
        events = ui.empty("検索条件に合う監査記録はありません。")

    inner = (
        ui.page_head("監査ログ", "記録の改ざん・欠損を検知する台帳です。")
        + '<section class="ri-alert ok" role="status" aria-labelledby="audit-ok-title">'
          '<h2 id="audit-ok-title">監査ログは正常です</h2>'
          '<p>ハッシュチェーンと末尾アンカーが一致しています。業務操作を継続できます。</p>'
          f'<p>検証済みの記録: {_esc(len(rows))}件</p></section>'
        + ui.section("監査イベント") + events
    )
    return 200, _wrap_main("audit", "/audit", "監査ログ", inner)


def render_ledger(data_dir: Path, params) -> str:
    """台帳 — 監査ログ＋統合台帳のハブ。各台帳へ件数つきで導線。"""
    _, audit = _load_rows_for_ui(data_dir, "jsonl:audit_log.jsonl")
    finalized = sum(1 for e in audit if e.get("action") == "finalized_with_signature")
    kpis = _kpis_html([
        ("blue", len(audit), "監査イベント", "/audit"),
        ("green", finalized, "記名確定", "/audit"),
        ("org", len(LEDGERS), "統合台帳", "/ledger"),
        ("blue", "HMAC", "改ざん検知", "/audit"),
    ])
    index_pages = [PAGE_BY_ROUTE.get("/audit")] + LEDGERS
    cards = []
    for p in index_pages:
        if not p:
            continue
        try:
            n = count_page(data_dir, p)
        except Exception:
            n = "—"
        label = _deemoji(p["label"])
        cards.append(
            f'<a class="ri-card" href="{p["route"]}"><div class="ct">{_esc(label)} '
            f'<span class="ri-badge warn">{_esc(n)}</span></div>'
            f'<div class="cm">{_esc(_deemoji(p.get("desc") or ""))}</div></a>')
    grid = f'<div class="ri-grid2">{"".join(cards)}</div>'
    recent = []
    for e in list(reversed(audit))[:6]:
        target = _audit_visible_value(e.get("target") or e.get("case") or "", "target")
        recent.append(
            f'<div class="ri-task"><span class="ri-tk"></span>'
            f'<span class="tt"><b>{_esc(_audit_actor_label(e.get("actor") or ""))}</b> · '
            f'{_esc(_audit_action_label(e.get("action") or ""))}'
            f' — {_esc(target)}</span>'
            f'<span class="tm">{_esc(_display_datetime(e.get("timestamp") or ""))}</span></div>')
    recent_block = (f'<div class="ri-card ri-tasks">{"".join(recent)}</div>'
                    if recent else ui.empty("監査イベントはまだありません。"))
    inner = (ui.page_head("台帳", "監査ログと統合台帳のハブ。各台帳へ件数つきで移動できます（改ざん検知つき）。")
             + kpis + ui.section("最近の監査イベント") + recent_block
             + ui.section("統合台帳") + grid)
    return _wrap_main("ledger", "/ledger", "台帳", inner)


# ---------------------------------------------------------------------------
# ルータ (GETのみ)
# ---------------------------------------------------------------------------
def _yen(n) -> str:
    try:
        return "¥" + f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"




def _portal_brand_css(data_dir) -> str:
    """お客様ページの色を**取扱会社のブランド色**へ差し替える追加CSS。

    お客様に届く面に製品の色（cobalt）を出さない。
    管理画面と同じ APP_CSS を土台に使うが、色トークンだけ会社の値で上書きする。
    会社色が未設定なら中立の graphite に落とす（製品色のままにしない）。
    """
    from hub_core import branding as _br, maisoku as _maisoku
    from hub_core.auth import CompanyProfileError, load_company
    try:
        company = load_company(data_dir, strict=True) or {}
    except CompanyProfileError:
        company = {}
    _name, brand = _br.brand_of_company(company)
    theme = _maisoku.theme_fields(accent=brand)
    accent = theme["accent"]
    accent_ink = theme["accent_ink"]
    accent_text = theme["accent_text"]
    accent_soft = theme["accent_soft"]
    def _rgb(hexcolor: str) -> tuple[int, int, int]:
        h = (hexcolor or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            return (42, 46, 55)

    def _lin(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    def _lum(hexcolor: str) -> float:
        r, g, b = _rgb(hexcolor)
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    def _contrast(a: str, b: str) -> float:
        lo, hi = sorted((_lum(a), _lum(b)))
        return (hi + 0.05) / (lo + 0.05)

    def _hex(parts: tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*parts)

    action_bg, action_ink = accent, accent_ink
    if _contrast(action_bg, action_ink) < 7.0:
        action_ink = "#fff" if _contrast(accent, "#fff") >= _contrast(accent, "#16191c") else "#16191c"
        r, g, b = _rgb(accent)
        for _ in range(24):
            action_bg = _hex((r, g, b))
            if _contrast(action_bg, action_ink) >= 7.0:
                break
            if action_ink == "#fff":
                r, g, b = (max(0, round(c * 0.88)) for c in (r, g, b))
            else:
                r, g, b = (min(255, round(c + (255 - c) * 0.12)) for c in (r, g, b))
    return (":root{"
            f"--ai-cobalt:{accent};--ai-cobalt-deep:{accent_text};"
            f"--ai-cobalt-press:{accent_text};--ai-soft:{accent_soft};"
            f"--accent:{accent_text};--accent-ink:{accent_ink};"
            f"--accent-bg:{accent_soft};--ink-deep:{accent_text};"
            "}"
            f".portal-shell .ri-go,.portal-shell .ri-go:hover{{background:{action_bg};"
            f"border-color:{action_bg};color:{action_ink}}}"
            ".portal-shell{max-width:720px;margin:0 auto;padding:52px 24px 72px}"
            ".portal-head{display:flex;align-items:flex-end;justify-content:space-between;"
            "gap:18px;border-bottom:4px solid var(--ai-cobalt);padding-bottom:14px;margin-bottom:28px}"
            ".portal-company{font-family:var(--head);font-size:21px;font-weight:700;color:var(--sumi)}"
            ".portal-contact{font-size:18px;color:var(--muted);margin-top:3px}"
            ".portal-kicker{font-size:18px;color:var(--muted);white-space:nowrap}"
            ".portal-shell h1{font-size:28px!important;line-height:1.35;margin:0 0 6px}"
            "@media(max-width:600px){.portal-shell{padding:28px 18px 48px}"
            ".portal-head{display:block}.portal-kicker{margin-top:4px}"
            ".portal-shell .ri-actform{display:grid}.portal-shell .ri-actform input,"
            ".portal-shell .ri-actform button{width:100%;min-width:0}}")


def _portal_company(data_dir) -> str:
    """お客様ページに出す会社名＝取扱会社。未設定なら一般名詞で伏せる（別会社名を出さない）。"""
    raw = str((load_company(data_dir, strict=True) or {}).get("name") or "").strip()
    return _visible_data_value("会社名", raw) or "担当会社"


def _portal_contact(data_dir) -> str:
    """Show who can answer before a public visitor submits personal data."""
    company = load_company(data_dir, strict=True) or {}
    email = _visible_data_value("メールアドレス", company.get("email") or "")
    tel = _visible_data_value("電話番号", company.get("tel") or "")
    return email or tel or "連絡先未設定"


def _tenant_label(tenant: str) -> str:
    """内部テナントslugを人間可読名へ（self=自社・その他は顧客名解決を試みる）。"""
    if tenant in ("self", "", "代表", "岩谷"):
        return "自社"
    return _visible_data_value("契約先", tenant) or "対象"


# 一般配布版で実際に保守できるLLMモードだけを画面へ出す。
# 任意のOpenAI互換URLと外部CLI/MCPは、接続先の固定・鍵の永続化・配布物内の接続部が
# 揃っていないため一般配布版の選択肢にしない。
_LLM_MODES = [
    {"mode": "none", "provider": "", "label": "① AIを使わない",
     "model": "", "base": "",
     "privacy": "外部送信なし", "cost": "¥0", "note": "初期設定。AI接続なしでも台帳・書類・承認は使える。"},
    {"mode": "local", "provider": "openai", "label": "② ローカル（Ollama）",
     "model": "qwen3:8b", "base": "http://localhost:11434/v1",
     "privacy": "AI処理は端末内", "cost": "¥0", "note": "推奨・完全無料・オフライン可。軽量モデルはツール呼び出しの信頼性が外部LLMより低い。"},
    {"mode": "anthropic", "provider": "anthropic", "label": "③ 自前API（Anthropic）",
     "model": "claude-haiku-4-5", "base": "",
     "privacy": "Anthropicへ送信", "cost": "従量（安）", "note": "難工程は上位モデルへ昇格可。キーは同期フォルダ外に保存。"},
]

_LLM_TASK_RECO = [
    ("重説ドラフト・契約文言", "Anthropic（上位モデル）", "正確性が要る工程。最終は必ず宅建士が確認・記名。"),
    ("反響一次返信・要約", "ローカル or Anthropic", "定型・大量は安く速く。個人情報はローカルが安全。"),
    ("マイソク文案・広告", "ローカル or Anthropic", "広告表現は必ず人が確認する。"),
    ("機微な個人情報を含む処理", "ローカル（推奨）", "データを外に出さない。"),
]


def _save_public_llm_mode(data_dir: Path, form: dict) -> bool:
    """一般配布版で提供する3モードだけを保存する。

    未知の値は旧設定も含めてAIなしへ倒す。任意URLや外部CLIをPOSTで復活させない。
    """
    from hub_core import chat_llm

    mode = (form.get("llm_mode", [""])[0] or "").strip()
    model = (form.get("model", [""])[0] or "").strip()[:160]
    api_key = (form.get("api_key", [""])[0] or "").strip()
    if mode in ("", "none"):
        chat_llm.save_mode_config(data_dir, "")
        return True
    if mode == "local":
        chat_llm.save_mode_config(
            data_dir, "openai", "http://localhost:11434/v1", model or "qwen3:8b")
        return True
    if mode == "anthropic":
        chat_llm.save_mode_config(data_dir, "anthropic", "", model or "claude-haiku-4-5")
        if api_key:
            chat_llm.save_api_key(api_key)
        return True
    chat_llm.save_mode_config(data_dir, "")
    return False


def _portal_export_rows(data_dir: Path) -> list[dict]:
    """properties台帳＋cases(物件名)から掲載書式用の行を組み立てる。"""
    _, props = _load_rows_for_ui(data_dir, "csv:properties.csv")
    if not props:
        try:
            from hub_core.store import SqliteStore
            db = Path(data_dir) / "hub.db"
            props = SqliteStore(db).query("properties") if db.exists() else []
        except Exception:
            props = []
    _, cases = _load_rows_for_ui(data_dir, "csv:cases.csv")
    name_by_pid = {}
    for c in cases:
        pid = c.get("物件ID") or c.get("property_id")
        nm = c.get("物件名") or c.get("property_name")
        if pid and nm:
            name_by_pid[pid] = nm
    rows = []
    for r in props:
        pid = r.get("物件ID") or r.get("property_id") or ""
        rows.append({
            "property_name": name_by_pid.get(pid, "") or r.get("所在地") or r.get("address") or "",
            "address": r.get("所在地") or r.get("address") or "",
            "rent_or_price": r.get("賃料/価格") or r.get("rent_or_price") or "",
            "layout": r.get("間取り") or r.get("layout") or "",
            "area": r.get("面積") or r.get("area") or "",
            "built_year": r.get("築年") or r.get("built_year") or "",
            "structure": r.get("構造") or r.get("structure") or "",
            "station": r.get("最寄駅") or r.get("station") or "",
            "walk_min": r.get("徒歩分") or r.get("walk_min") or "",
            "deal_type": r.get("取引種別") or r.get("deal_type") or "",
            "pet": r.get("ペット") or r.get("pet") or "",
        })
    return rows


# 業者プロフィールの項目定義: (key, ラベル, placeholder, グループ, 入力型)。帯(取扱業者欄)＋重説に使い回す。
PROFILE_FIELDS = [
    ("name", "会社名", "株式会社みなと不動産", "会社", "text"),
    ("license_no", "宅地建物取引業 免許番号", "東京都知事 (1) 第00000号", "会社", "text"),
    ("address", "会社所在地", "東京都〇〇区〇〇1-2-3", "会社", "text"),
    ("tel", "電話番号", "03-0000-0000", "連絡先", "text"),
    ("fax", "FAX", "03-0000-0001", "連絡先", "text"),
    ("email", "メールアドレス", "info@example.com", "連絡先", "text"),
    ("staff", "既定の担当者", "山田", "連絡先", "text"),
    ("association", "所属する保証協会", "（公社）全国宅地建物取引業保証協会 等", "加盟団体", "text"),
    ("fair_trade", "公正取引協議会", "首都圏不動産公正取引協議会 等", "加盟団体", "text"),
    ("holiday", "定休日", "水曜日", "加盟団体", "text"),
    ("brand_color", "ブランド色", "#b3261e", "マイソクの体裁", "color"),
    ("display_font", "見出しの書体", "gothic", "マイソクの体裁", "font"),
]
PROFILE_GROUPS = ["会社", "連絡先", "加盟団体", "マイソクの体裁"]
FONT_LABELS = [("gothic", "ゴシック（標準）"), ("condensed", "コンデンス（引き締まった見出し）"),
               ("rounded", "丸ゴシック（やわらかい）")]



def render_profile_error(data_dir: Path, message: str) -> str:
    """業者情報の保存に失敗した理由を画面に出す（黙って保存済みに見せない）。"""
    body = (f'<div class="ri-h1">業者情報を保存できませんでした</div>'
            f'<div class="ri-note" style="color:var(--ai-seal-deep)">{_esc(message)}</div>'
            f'<div style="margin-top:16px"><a class="ri-go" href="/profile">入力に戻る</a></div>')
    return _ri_shell("/profile", "業者情報", body)



# ---- ブランドの履歴と「元に戻す」画面 -------------------------------------------
# 版管理は hub_core/branding.py に入っていたが画面が無く、実運用では戻せなかった。
# 直したくなったら戻せる操作を利用者に提供する。

_BRAND_SOURCE_LABEL = {
    "setup": "はじめての設定", "profile": "業者情報の画面", "manual": "手入力",
    "logo": "ロゴ画像から取り込み", "it_gate_set": "IT重説の設定",
}


def _brand_source_label(src: str) -> str:
    src = str(src or "")
    if src.startswith("restore:"):
        return f"版 {src.split(':', 1)[1]} に戻した"
    if src.startswith("website:"):
        return "Webサイトから取り込み"
    return _BRAND_SOURCE_LABEL.get(src, _visible_data_value("参照元", src) or "不明")


def render_brand_history(data_dir: Path, params) -> str:
    """会社の見た目・表示情報の変更履歴。ここから過去の版へ戻せる。"""
    from hub_core import branding as _br
    viewer = current_viewer()
    can_edit = bool(viewer and viewer.role in ("責任者", "代表"))
    rows = list(reversed(_br.read_history(data_dir)))
    msg_raw = (params.get("msg", [""])[0] or "").strip()
    msg = _public_notice_param(msg_raw, "会社情報の変更を完了できませんでした。") if msg_raw else ""
    cur_hash = ""
    try:
        from hub_core.auth import load_company
        cur_hash = _br.profile_hash(load_company(data_dir, strict=True) or {})
    except Exception:      # noqa: BLE001
        cur_hash = ""

    head = ui.page_head("会社情報の変更履歴",
                        "社名・免許番号・住所・ブランド色などを変えた記録です。"
                        "いつでも前の状態に戻せます。")
    if msg:
        head += f'<div class="ri-alert ok">{_esc(msg)}</div>'
    if not rows:
        return _wrap_main("profile", "/profile", "会社情報の変更履歴",
                          head + ui.empty("まだ変更の記録がありません。"
                                          "「業者情報」で保存すると、ここに履歴が残ります。"))

    items = ""
    for r in rows:
        v = int(r.get("version") or 0)
        b = r.get("brand") or {}
        is_now = bool(cur_hash and r.get("profile_hash") == cur_hash)
        chips = "".join(
            f'<span class="bh-chip"><span class="bh-k">{_esc(_MAISOKU_LABEL.get(k) or _BRAND_FIELD_LABEL.get(k, k))}</span>'
            f'<span class="bh-v">{_esc(str(b.get(k)))}</span></span>'
            for k in ("name", "license_no", "address", "tel", "brand_color") if b.get(k))
        color = b.get("brand_color") or ""
        swatch = (f'<span class="bh-sw" style="background:{_esc(color)}"></span>' if color else "")
        act = ("" if not can_edit or is_now else
               f'<form method="post" action="/brand/restore" style="margin:0">'
               f'<input type="hidden" name="version" value="{v}">'
               f'<button class="bh-go" type="submit">この状態に戻す</button></form>')
        items += (f'<div class="bh-row{" now" if is_now else ""}">'
                  f'<div class="bh-head"><span class="bh-v-no">版 {v}</span>'
                  f'{swatch}<span class="bh-when">{_esc(str(r.get("saved_at") or "")[:16].replace("T", " "))}</span>'
                  f'<span class="bh-src">{_esc(_brand_source_label(r.get("source")))}</span>'
                  + ('<span class="bh-now">いまの状態</span>' if is_now else "") + '</div>'
                  f'<div class="bh-chips">{chips}</div>{act}</div>')

    note = ('<div class="ms-note">戻しても履歴は消えません。戻した操作も新しい版として残るので、'
            'さらに戻すこともできます。</div>')
    return _wrap_main("profile", "/profile", "会社情報の変更履歴",
                      head + f'<div class="bh-list">{items}</div>' + note)


_BRAND_FIELD_LABEL = {
    "name": "会社名", "license_no": "免許番号", "address": "所在地", "tel": "電話",
    "fax": "FAX", "email": "メール", "association": "保証協会", "fair_trade": "公取協",
    "staff": "担当者", "takkenshi_reg": "宅建士登録番号", "brand_color": "ブランド色",
    "display_font": "見出しの書体", "tagline": "ひとこと", "business": "業種",
}


def render_profile(data_dir: Path, params) -> str:
    """業者プロフィール（会社情報）の編集。ここで一度入れれば、マイソクの帯・重説の業者欄に
    自動で反映されます（他社の原紙を借りる必要がありません）。責任者/代表のみ編集可。"""
    from hub_core.auth import load_company
    viewer = current_viewer()
    can_edit = bool(viewer and viewer.role in ("責任者", "代表"))
    company = load_company(data_dir)
    saved = (params.get("saved", [""])[0] == "1")
    ro = "" if can_edit else " readonly"
    dis = "" if can_edit else " disabled"

    def _field(key, label, ph, typ):
        val = _esc(company.get(key) or "")
        lab = f'<label class="pf-l" for="pf-{key}">{_esc(label)}</label>'
        if typ == "color":
            cur = (company.get(key) or "#b3261e")
            cur = cur if (isinstance(cur, str) and cur.startswith("#")) else "#b3261e"
            return (f'<div class="pf-f pf-f-color">{lab}'
                    f'<div class="pf-color"><input type="color" id="pf-{key}" name="{key}" '
                    f'value="{_esc(cur)}"{dis}>'
                    f'<span class="pf-color-v">{_esc(cur)}</span></div>'
                    f'<div class="pf-hint">マイソクのアクセント（帯・価格・地図線）に使われます</div></div>')
        if typ == "font":
            opts = "".join(f'<option value="{fk}"{" selected" if (company.get(key) or "gothic")==fk else ""}>{_esc(fl)}</option>'
                           for fk, fl in FONT_LABELS)
            return (f'<div class="pf-f">{lab}'
                    f'<select id="pf-{key}" name="{key}"{dis}>{opts}</select></div>')
        return (f'<div class="pf-f">{lab}'
                f'<input type="text" id="pf-{key}" name="{key}" value="{val}" '
                f'placeholder="{_esc(ph)}"{ro}></div>')

    blocks = ""
    for grp in PROFILE_GROUPS:
        fs = "".join(_field(k, l, ph, ty) for k, l, ph, ty, g2 in
                     [(k, l, ph, ty, g) for k, l, ph, g, ty in PROFILE_FIELDS] if g2 == grp)
        blocks += f'<fieldset class="pf-set"><legend>{_esc(grp)}</legend><div class="pf-grid">{fs}</div></fieldset>'

    note = ('<div class="pf-saved">保存しました。マイソクと重要事項説明書に反映されます。</div>'
            if saved else "")
    form = (note + '<form method="post" action="/profile/save" class="pf-wrap">' + blocks
            + ('<div class="pf-actions"><button class="ri-go" type="submit">業者情報を保存</button></div>'
               if can_edit else '<div class="gn">編集は責任者・代表のみです。</div>')
            + '</form>')
    from hub_core import branding as _brh
    n_hist = len(_brh.read_history(data_dir))
    hist_link = (f'<div style="margin:2px 0 16px"><a class="ri-go ghost" href="/brand/history">'
                 f'変更の履歴を見る・前の状態に戻す（{n_hist}件）</a></div>' if n_hist else "")
    inner = (ui.page_head("業者情報",
             "会社の情報を一度だけ登録します。マイソクの帯（取扱業者欄）と重要事項説明書の業者欄に、"
             "ここで入れた内容が自動で入ります。")
             + hist_link + form)
    return _wrap_main("home", "/profile", "業者情報", inner)


def render_materials(data_dir: Path, params) -> str:
    """物件素材ハブ（マイソク素材の合法な収集・再利用）: 物件別に集めた素材(写真/図面/調査/許諾)を
    一覧し、許諾状況と簡略化アクション(受領図面のOCR/間取り図SVG生成/帯替え)へ導く。読み取り専用。
    ※他社サイトからの写真/間取りの無断転載はしない。使うのは元付許諾つき・自社・生成した素材のみ。"""
    from hub_core.store import SqliteStore
    from hub_core import obi, provenance
    from hub_core.operations import OP_ROLES
    db = Path(data_dir) / "hub.db"
    try:
        assets = SqliteStore(db).query("vault_assets") if db.exists() else []
    except Exception:
        assets = []
    props = sorted({a.get("property") or "" for a in assets if a.get("property")})
    sel = (params.get("property", [""])[0] or (props[0] if props else "")).strip()
    tabs = "".join(f'<a class="facet{" on" if pr==sel else ""}" href="/materials?property={quote(pr)}">{_esc(pr)}</a>'
                   for pr in props[:20])
    body = (f'<div class="facets"><span class="flabel">物件:</span>{tabs}</div>'
            if props else ui.empty("物件フォルダに素材がありません（物件/<物件名>/素材 等に置くと索引します）。"))
    if sel:
        viewer = current_viewer()
        can_attest = bool(viewer and viewer.role in OP_ROLES.get("asset_attest", set()))
        by_cat = {}
        for a in assets:
            if (a.get("property") or "") == sel:
                by_cat.setdefault(a.get("category") or "その他", []).append(a)
        # 許諾状況（帯替え可否）
        try:
            obi.check_permission(data_dir, sel)
            permit = '<span class="qchip" style="background:#eef7f1;color:var(--ok)">帯替え許諾: 確認済</span>'
        except Exception:
            permit = '<span class="qchip" style="background:#faf0ee;color:var(--vermi)">帯替え許諾: 確認待ち</span>'
        secs = ""
        for cat in ("素材", "調査", "許諾", "書類", "その他"):
            items = by_cat.get(cat) or []
            if not items:
                continue
            row_html = []
            for asset in items:
                ref = str(asset.get("asset_key") or "")
                suffix = Path(ref).suffix.lower()
                image_asset = suffix in {".png", ".jpg", ".jpeg", ".webp"}
                state = ""
                action = ""
                resolved = provenance._resolve_vault_file(data_dir, ref) if image_asset else None
                if resolved is not None:
                    try:
                        provenance.check_asset(data_dir, resolved, sel, "advertise")
                        state = '<span class="ri-badge ok">広告利用を確認済み</span>'
                    except provenance.ProvenanceError:
                        state = '<span class="ri-badge warn">利用確認待ち</span>'
                        if can_attest and provenance.class_of(resolved) == "unknown":
                            action = (
                                '<form method="post" action="/op" style="margin-top:8px">'
                                '<input type="hidden" name="op" value="asset_attest">'
                                f'<input type="hidden" name="asset" value="{_esc(ref)}">'
                                f'<input type="hidden" name="property" value="{_esc(sel)}">'
                                '<input type="hidden" name="rights" value="advertise">'
                                '<label style="display:flex;gap:8px;align-items:flex-start;font-size:16px">'
                                '<input type="checkbox" name="confirm_self" value="1" required> '
                                '<span>自社で撮影または作成した素材であることを確認しました</span></label>'
                                '<button class="ri-go ghost" type="submit" style="margin-top:6px">広告用素材として登録</button>'
                                '</form>')
                row_html.append(
                    f'<div class="bulk-row"><span class="bulk-t">{_esc(asset.get("filename") or "")}</span>'
                    f'<span class="lref">{_esc(asset.get("kind") or "")}</span>{state}{action}</div>')
            rows = "".join(row_html)
            secs += ui.section(f"{cat}（{len(items)}件）") + f'<div class="bulk-list">{rows}</div>'
        actions = ('<div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">'
                   '<a class="ri-go ghost" href="/madori">間取り図を作る（概略SVG）→</a>'
                   '<a class="ri-go ghost" href="/case">受領図面をOCRで数値化 →</a>'
                   '<a class="ri-go ghost" href="/maisoku">マイソク帯替え →</a></div>')
        body += (f'<div style="margin:12px 0">{permit}</div>' + actions
                 + (secs or ui.empty("この物件の素材はまだありません。")))
    inner = (ui.page_head("物件素材ハブ",
             "マイソク用の素材を物件ごとに集約します。使うのは元付許諾つき・自社・生成した素材のみ。"
             "他社サイトからの写真/間取りの無断転載はしません（帯替えは許諾台帳で遵法ゲート）。")
             + body
             + '<div class="gn" style="margin-top:10px">元付から正規に受領した図面はOCRで数値化し、'
             + '間取り図は概略SVGで自作できます。帯替えは元付許諾（広告掲載可＋帯替え可）が台帳にある時のみ。</div>')
    return _wrap_main("properties", "/materials", "物件素材", inner)


def render_madori(data_dir: Path, params) -> str:
    """間取り図（概略SVG）: 間取りコードから概略平面図を生成（マイソク/提案書に埋め込み用・¥0）。"""
    from hub_core import madori
    sel = (params.get("layout", [""])[0] or "2LDK").strip().upper()
    if sel not in madori.layouts():
        sel = "2LDK"
    tabs = "".join(f'<a class="facet{" on" if L==sel else ""}" href="/madori?layout={L}">{_esc(L)}</a>'
                   for L in madori.layouts())
    svg = madori.schematic_for(sel)
    inner = (ui.page_head("間取り図（概略）",
             "間取りコードから概略の平面図を生成します。マイソクや提案書に埋め込めます。"
             "※これは概略図であり、実際の寸法・配置ではありません。")
             + f'<div class="facets"><span class="flabel">間取り:</span>{tabs}</div>'
             + f'<div style="max-width:520px;border:1px solid var(--line);border-radius:6px;'
             + f'padding:12px;background:#fff">{svg}</div>'
             + '<div class="gn" style="margin-top:8px">実寸の間取りは図面から作成してください（本図は概略）。</div>')
    return _wrap_main("properties", "/madori", "間取り図", inner)


def _followup_bulk_button() -> str:
    """追客漏れの一括ドラフト作成ボタン（BULK-02＝followup_generate op・可逆・送信は人間ゲート）。"""
    js = ("async function rioFollowupBulk(b){b.disabled=true;"
          "var m=document.getElementById('fuMsg');m.textContent='作成中…';"
          "try{var r=await fetch('/api/op',{method:'POST',"
          "headers:{'Content-Type':'application/json'},"
          "body:JSON.stringify({op:'followup_generate',params:{}})});"
          "var j=await r.json();"
          "m.textContent=r.ok?('追客ドラフト'+(j.drafted||0)+'件を作成しました（送信は確認ゲートを通ります）')"
          ":('失敗: '+(j.error||r.status));}catch(e){m.textContent='失敗しました';}b.disabled=false;}")
    return ('<div class="ri-quick" style="margin-top:2px">'
            '<span class="ri-quick-l">一括操作</span>'
            '<button class="ri-qbtn" onclick="rioFollowupBulk(this)">追客ドラフトを一括作成</button>'
            '<span id="fuMsg" class="gn"></span></div>'
            '<script>' + js + '</script>')


def render_pm_dashboard(data_dir: Path, params) -> str:
    """管理業務ダッシュボード（Tier2）: 更新期限・延滞請求・収支・追客漏れを一望（読み取り専用）。
    実データのみ集計・アクション(督促/更新案内生成)は各opの人間確認を通す。"""
    from hub_core import contract as _ct, analytics, followup as _fu
    from hub_core.store import SqliteStore
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db = Path(data_dir) / "hub.db"

    def _q(tbl, where=None, args=()):
        try:
            return SqliteStore(db).query(tbl, where, args) if db.exists() else []
        except Exception:
            return []

    # 更新期限（60日以内）
    contracts = _q("contract_register")
    expiring = _ct.expiring_contracts(contracts, today=today, within_days=60)
    # 延滞請求（30日期日超過・未消込）
    overdue = []
    for b in _q("billing_register", "kind = ?", ("請求",)):
        if (b.get("status") or "").strip() in ("消込済", "入金消込"):
            continue
        try:
            due = _dt.date.fromisoformat(str(b.get("created_at") or "")[:10]) + _dt.timedelta(days=30)
            if (_dt.date.fromisoformat(today) - due).days > 0:
                overdue.append(b)
        except ValueError:
            continue
    # 収支
    income = analytics.income_summary(data_dir)
    # 追客漏れ
    due_follow = _fu.due_followups(data_dir, today=today)

    def _metric(label, value, sub=""):
        # 枠なしメトリクス（ホームと統一＝設計思想の自己違反を解消）
        return (f'<div class="kpi"><div class="n">{value}</div><div class="l">{_esc(label)}</div>'
                + (f'<div class="l" style="margin-top:2px">{_esc(sub)}</div>' if sub else "") + '</div>')

    cards = ('<div class="ri-kpis">'
             + _metric("更新期限（60日内）", f"{len(expiring)}件")
             + _metric("延滞請求", f"{len(overdue)}件")
             + _metric("未回収", f"¥{income['outstanding']:,}", f"回収率{income['collection_rate']*100:.0f}%")
             + _metric("追客漏れ", f"{len(due_follow)}件") + '</div>'
             + (_followup_bulk_button() if due_follow else ''))

    def _list(title, rows_html, link, link_label):
        return (f'<div class="ri-sech" style="margin-top:16px">{_esc(title)} '
                f'<a class="lref" href="{link}">{_esc(link_label)} →</a></div>'
                + (rows_html or ui.empty("なし")))

    exp_rows = "".join(f'<div class="bulk-row"><span class="bulk-t">{_esc(c.get("property_name") or "")} '
                       f'{_esc(c.get("contract_type") or "")}（満了 {_esc(c.get("end_date") or "")}）</span>'
                       f'<span class="qchip">{"満了" if c["days_left"]<0 else f"あと{c['days_left']}日"}</span></div>'
                       for c in expiring[:5])
    ovd_rows = "".join(f'<div class="bulk-row"><span class="bulk-t">{_esc(b.get("customer_name") or "")} '
                       f'¥{_esc(b.get("amount") or "")}</span><span class="qchip">延滞</span></div>'
                       for b in overdue[:5])
    inner = (ui.page_head("管理業務ダッシュボード",
             "更新期限・延滞請求・収支・追客漏れを一望します。数値は台帳実績のみ。"
             "督促や更新案内の生成は各操作の人間確認を通します。")
             + cards
             + _list("更新期限が近い契約", ('<div class="bulk-list">' + exp_rows + '</div>') if exp_rows else "",
                     "/renewals", "更新期限管理")
             + _list("延滞している請求", ('<div class="bulk-list">' + ovd_rows + '</div>') if ovd_rows else "",
                     "/money", "請求台帳"))
    return _wrap_main("home", "/pm", "管理業務", inner)


def render_money(data_dir: Path, params) -> str:
    """金銭（Money）: 請求台帳（billing_register）＋収支サマリ。読み取り専用。
    請求作成/消込は各op(経理+人間確認)を通す。従来はtasks(money-gate)だったが請求実体を表示する。"""
    from hub_core import analytics
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    try:
        bills = SqliteStore(db).query("billing_register") if db.exists() else []
    except Exception:
        bills = []
    income = analytics.income_summary(data_dir)
    # 収支サマリ（枠なしメトリクス）
    cards = ('<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">'
             + f'<div class="ri-card"><div class="ct" style="font-size:20px;font-weight:700">¥{income["billed"]:,}</div><div class="gn">請求合計</div></div>'
             + f'<div class="ri-card"><div class="ct" style="font-size:20px;font-weight:700">¥{income["collected"]:,}</div><div class="gn">回収済</div></div>'
             + f'<div class="ri-card"><div class="ct" style="font-size:20px;font-weight:700">¥{income["outstanding"]:,}</div><div class="gn">未回収</div></div>'
             + '</div>')
    if bills:
        def _stat(s):
            s = (s or "").strip()
            return ('<span class="qchip" style="background:#e8f3ec;color:var(--confirm)">消込済</span>'
                    if s in ("消込済", "入金消込") else f'<span class="qchip">{_esc(s or "発行")}</span>')
        rows = "".join(
            f'<tr><td>{_esc(b.get("customer_name") or "")}</td><td>{_esc(b.get("kind") or "")}</td>'
            f'<td>¥{_esc(b.get("amount") or "")}</td><td>{_stat(b.get("status"))}</td>'
            f'<td>{_esc(str(b.get("created_at") or "")[:10])}</td></tr>' for b in bills)
        table = ('<div class="tablewrap"><table><thead><tr><th>顧客</th><th>種別</th>'
                 '<th>金額</th><th>状態</th><th>作成日</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div>')
    else:
        table = ui.empty("請求がありません（請求を作成すると台帳に表示されます）。")
    # 金銭関連タスク（gate=money）＝従来の /money 内容も保持（CSV-or-db 共通ヘルパで読む）
    try:
        _, _trows = _load_rows_for_ui(data_dir, "csv:tasks.csv")
    except Exception:
        _trows = []
    def _tg(r):
        return (r.get("ゲート") or r.get("gate") or "").strip()
    def _tv(r, ja, en):
        return r.get(ja) or r.get(en) or ""
    mtasks = [tk for tk in _trows if _tg(tk) == "money"]
    if mtasks:
        trows = "".join(f'<tr><td>{_esc(_tv(tk, "タイトル", "title"))}</td>'
                        f'<td>{_esc(_tv(tk, "顧客名", "customer_name"))}</td>'
                        f'<td>{_esc(_tv(tk, "状態", "status"))}</td></tr>' for tk in mtasks)
        tasks_sec = (ui.section(f"金銭関連タスク（{len(mtasks)}件）")
                     + '<div class="tablewrap"><table><thead><tr><th>タスク</th><th>顧客</th>'
                     f'<th>状態</th></tr></thead><tbody>{trows}</tbody></table></div>')
    else:
        tasks_sec = ""
    inner = (ui.page_head("金銭（請求・入金）",
             "請求台帳と収支を表示します。請求作成・入金消込は経理/責任者/代表の人間確認を通します（実送金はしません）。")
             + cards + ui.section(f"請求台帳（{len(bills)}件）") + table + tasks_sec
             + f'<div class="gn" style="margin-top:8px"><a class="lref" href="/reconcile">入金消込 →</a>　'
             + '<a class="lref" href="/pm">管理業務ダッシュボード →</a></div>')
    return _wrap_main("ledger", "/money", "金銭", inner)


def render_renewals(data_dir: Path, params) -> str:
    """契約台帳・更新期限管理（Tier2・/renewals）: 契約一覧＋更新期限が近い契約のアラート。読み取り専用。
    注: /contracts は契約書類の版管理（別画面）。ここは賃貸/保証/火災保険の更新期限管理。"""
    from hub_core import contract as _ct
    import datetime as _dt
    from hub_core.store import SqliteStore
    db = Path(data_dir) / "hub.db"
    try:
        rows = SqliteStore(db).query("contract_register") if db.exists() else []
    except Exception:
        rows = []   # 契約台帳が未生成（reindex前）でも描画は落ちない
    today = _dt.date.today().isoformat()
    expiring = _ct.expiring_contracts(rows, today=today, within_days=60)
    if expiring:
        arows = "".join(
            f'<div class="bulk-row"><span class="bulk-t">{_esc(c.get("property_name") or "")} '
            f'{_esc(c.get("contract_type") or "")}（満了 {_esc(c.get("end_date") or "")}）</span>'
            f'<span class="qchip">{"超過" if c["days_left"] < 0 else f"あと{c['days_left']}日"}</span></div>'
            for c in expiring)
        alert = (f'<div class="ri-sech">更新期限アラート（60日以内・{len(expiring)}件）</div>'
                 f'<div class="bulk-list">{arows}</div>')
    else:
        alert = '<div class="ri-sech">更新期限アラート</div>' + ui.empty("60日以内に満了する契約はありません。")
    if rows:
        trows = "".join(
            f'<tr><td>{_esc(c.get("property_name") or "")}</td><td>{_esc(c.get("contract_type") or "")}</td>'
            f'<td>{_esc(c.get("counterparty") or "")}</td><td>{_esc(c.get("end_date") or "")}</td>'
            f'<td>{_esc(c.get("status") or "")}</td></tr>' for c in rows)
        table = ('<div class="tablewrap"><table><thead><tr><th>物件</th><th>種別</th>'
                 '<th>相手方</th><th>満了日</th><th>状態</th></tr></thead>'
                 f'<tbody>{trows}</tbody></table></div>')
    else:
        table = ui.empty("契約がありません（契約を登録すると更新期限を管理します）。")
    inner = (ui.page_head("契約台帳・更新期限",
             "賃貸借・保証委託・火災保険等の契約を台帳化し、更新期限が近い契約を検出します（管理業務）。")
             + alert + ui.section("契約一覧") + table)
    return _wrap_main("ledger", "/renewals", "更新期限", inner)


def render_customer_portal(data_dir: Path, params) -> str:
    """顧客ポータル（G7・マジックリンク認証・内部RBAC非依存の限定ビュー）。
    トークンで案件状況・活動報告を読み取り表示。個人情報は最小。公開デプロイは人間ゲート。"""
    from hub_core import portal as _pt
    import datetime as _dt
    token = (params.get("token", [""])[0] or "").strip()
    today = _dt.date.today().isoformat()
    company = _portal_company(data_dir)
    contact = _portal_contact(data_dir)
    shell_open = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width, initial-scale=1">'
                  f'<title>お客様ページ | {_esc(company)}</title>'
                  f'<style>{ui.APP_CSS}{_portal_brand_css(data_dir)}</style></head><body>'
                  '<main class="portal-shell"><header class="portal-head">'
                  '<div>'
                  f'<div class="portal-company">{_esc(company)}</div>'
                  f'<div class="portal-contact">お問い合わせ {_esc(contact)}</div></div>'
                  '<div class="portal-kicker">お客様ページ</div></header>')
    shell_close = '</main></body></html>'
    if not token:
        return _deemoji(shell_open + '<div class="ri-empty">リンクが正しくありません。'
                        '担当者にお問い合わせください。</div>' + shell_close)
    try:
        payload = _pt.verify_token(token, today=today)
    except _pt.PortalError:
        return _deemoji(shell_open + '<div class="ri-empty">このリンクを確認できません。担当者に新しいリンクをご依頼ください。</div>' + shell_close)
    scope = payload.get("scope") or "customer"
    data = _pt.portal_view_data(data_dir, payload["case_id"], scope=scope, today=today)
    def public(field, value, fallback=""):
        return _visible_data_value(field, value) or fallback

    title = _esc(public("物件名", data.get("property"),
                        "お客様の物件" if scope != "owner" else "ご売却物件"))
    if scope == "tenant":
        cts = data.get("contracts") or []
        crows = "".join(
            f'<div class="bulk-row"><span class="bulk-t">{_esc(public("契約種別", ct.get("contract_type"), "契約"))}'
            f'（満了 {_esc(public("満了日", ct.get("end_date"), "未定"))}）</span>'
            + ("" if ct.get("days_left") is None
               else f'<span class="qchip">{"満了" if ct["days_left"] < 0 else f"あと{ct['days_left']}日"}</span>')
            + '</div>' for ct in cts)
        body = (f'<h1 style="font-size:20px;color:var(--sumi)">{title}</h1>'
                f'<div class="ph-sub">ご入居中のお住まいに関するご案内ページです。</div>'
                + ('<div class="ri-sech" style="margin-top:20px">ご契約・更新のご案内</div>'
                   f'<div class="bulk-list">{crows}</div>' if crows
                   else '<div class="ri-sech" style="margin-top:20px">ご契約</div>'
                        + ui.empty("現在ご案内する更新予定はありません。"))
                + '<div class="ri-sech" style="margin-top:16px">お問い合わせ・修繕のご依頼</div>'
                + '<form method="post" action="/portal/request" class="ri-actform">'
                + f'<input type="hidden" name="token" value="{_esc(token)}">'
                + '<input name="note" placeholder="設備の不具合・更新のご相談など" style="min-width:280px">'
                + '<button class="ri-go" type="submit">担当者に伝える</button></form>'
                + '<div class="gn" style="margin-top:8px">送信内容は担当者が確認します（自動では確定しません）。</div>')
    elif scope == "owner" and data.get("activity"):
        act = data["activity"]; c = act["counts"]
        rows = "".join(f'<div class="bulk-row"><span class="bulk-t">{_esc(public("活動日", a["date"]))} '
                       f'{_esc(public("活動経路", a.get("channel", "")))}</span></div>' for a in act["activities"])
        body = (f'<h1 style="font-size:20px;color:var(--sumi)">{title}</h1>'
                f'<div class="ph-sub">{_esc(company)}からの販売活動のご報告です（宅建業法34条の2）。</div>'
                f'<div class="ri-sech" style="margin-top:20px">活動状況（{_esc(public("報告期間", act["period"], "期間未定"))}）</div>'
                f'<div class="ri-card"><div class="ct">お問い合わせ {_esc(public("問い合わせ件数", c["inquiries"], "0"))}件 ／ '
                f'ご内見 {_esc(public("内見件数", c["viewings"], "0"))}件 ／ 当社対応 {_esc(public("対応件数", c["contacts"], "0"))}件</div></div>'
                + (f'<div class="ri-sech" style="margin-top:16px">活動の内訳</div>'
                   f'<div class="bulk-list">{rows}</div>' if rows else "")
                + '<div class="ri-sech" style="margin-top:16px">ご質問・ご要望</div>'
                + '<form method="post" action="/portal/request" class="ri-actform">'
                + f'<input type="hidden" name="token" value="{_esc(token)}">'
                + '<input name="note" placeholder="価格や販売方針のご相談など" style="min-width:280px">'
                + '<button class="ri-go" type="submit">担当者に伝える</button></form>')
    else:
        apps = "".join(f'<li class="alist" style="list-style:none">申込の状況: '
                       f'<b>{_esc(public("申込状況", a["status"], "確認中"))}</b></li>' for a in data["applications"])
        body = (f'<h1 style="font-size:20px;color:var(--sumi)">{title}</h1>'
                f'<div class="ph-sub">{_esc(company)}からの共有ページです。</div>'
                '<div class="ri-sech" style="margin-top:20px">進捗</div>'
                f'<div class="ri-card"><div class="ct">現在の状況: {_esc(public("進捗状況", data["status"], "確認中"))}</div></div>'
                + (f'<div class="ri-sech" style="margin-top:16px">お申込</div><ul style="padding:0">{apps}</ul>' if apps else "")
                + '<div class="ri-sech" style="margin-top:16px">内見のご希望</div>'
                + '<form method="post" action="/portal/request" class="ri-actform">'
                + f'<input type="hidden" name="token" value="{_esc(token)}">'
                + '<input name="note" placeholder="ご希望の日時・ご質問など" style="min-width:280px">'
                + '<button class="ri-go" type="submit">担当者に伝える</button></form>'
                + '<div class="ri-sech" style="margin-top:16px">お申込み</div>'
                + '<form method="post" action="/portal/apply" class="ri-actform">'
                + f'<input type="hidden" name="token" value="{_esc(token)}">'
                + '<input name="applicant" placeholder="お名前" style="min-width:160px">'
                + '<input name="note" placeholder="ご連絡先・ご希望条件など" style="min-width:220px">'
                + '<button class="ri-go" type="submit">この物件に申し込む</button></form>'
                + '<div class="gn" style="margin-top:8px">送信内容は担当者が確認します（自動では確定しません・審査は担当者が保証会社へ取次ぎます）。</div>')
    return _deemoji(shell_open + body + shell_close)


def render_portal_export(data_dir: Path, params) -> str:
    """ポータル掲載書式のexport（G5）: 物件を各ポータルのCSV書式で出力（自動出稿はしない・手動アップロード）。"""
    from hub_core import portal_export as _pe
    rows = _portal_export_rows(data_dir)
    _JP = {"suumo": "SUUMO", "homes": "LIFULL HOME'S", "athome": "athome", "jibun": "自社サイト"}
    links = "".join(
        f'<a class="ri-go ghost" href="/portal/export?portal={p}" >{_esc(_JP.get(p, p))} 書式でCSV出力 →</a>'
        for p in _pe.portals())
    # プレビュー（先頭ポータル）
    sel = (params.get("portal", [""])[0] or "suumo").strip()
    if sel not in _pe.portals():
        sel = "suumo"
    tabs = "".join(f'<a class="facet{" on" if p==sel else ""}" href="/portal-export?portal={p}">{_esc(_JP.get(p,p))}</a>'
                   for p in _pe.portals())
    try:
        csv_text = _pe.export_csv(rows[:5], sel)
    except ValueError:
        csv_text = ""
    preview = (f'<div class="tablewrap"><pre style="margin:0;padding:12px;font-family:var(--mono);'
               f'font-size:18px;white-space:pre;overflow-x:auto">{_esc(csv_text)}</pre></div>'
               if csv_text else ui.empty("物件データがありません（物件を登録すると書式が生成されます）。"))
    inner = (ui.page_head("ポータル書式export",
             "登録物件を各ポータルの掲載書式（CSV）で出力します。自動出稿はしません——生成したCSVを"
             "各ポータルの管理画面へ手動でアップロードしてください（乗換ゼロ摩擦の入口）。")
             + f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">{links}</div>'
             + ui.section(f"プレビュー（{len(rows)}物件・先頭5件）")
             + f'<div class="facets"><span class="flabel">書式:</span>{tabs}</div>' + preview
             + '<div class="gn" style="margin-top:6px">書式の列は公開情報に基づく定義です。実掲載の列は掲載契約後に較正します。</div>')
    return _wrap_main("properties", "/portal-export", "ポータル書式export", inner)


def render_reconcile(data_dir: Path, params) -> str:
    """入金消込（G3）: 入金/フォルダの全銀ファイルを未消込請求と突合→候補提示→1件ずつ消込。
    突合は読取専用・消込は金銭ゲート(経理/責任者/代表)・実送金はしない。"""
    from hub_core.operations import reconcile_deposits, OP_ROLES
    viewer = current_viewer()
    can = bool(viewer and viewer.role in OP_ROLES.get("billing_reconcile", set()))
    # 入金/フォルダの全銀ファイル一覧
    inbox = Path(data_dir) / "入金"
    files = sorted(p.name for p in inbox.glob("*")) if inbox.is_dir() else []
    sel = (params.get("source", [""])[0] or "").strip()
    if not sel and files:
        sel = "入金/" + files[0]
    picker = ('<div class="facets"><span class="flabel">全銀ファイル:</span>'
              + "".join(f'<a class="facet{" on" if sel.endswith(fn) else ""}" '
                        f'href="/reconcile?source={quote("入金/"+fn)}">{_esc(fn)}</a>' for fn in files)
              + ('<span class="gn">入金/ フォルダに全銀明細ファイルを置いてください</span>' if not files else "")
              + '</div>')
    body = picker
    if sel:
        try:
            from hub_core import operations as _ops
            r = _ops.reconcile_deposits(data_dir, {"source": sel},
                                        viewer.user if viewer else "", viewer.role if viewer else "")
        except Exception:
            return _wrap_main("ledger", "/reconcile", "入金消込",
                              ui.page_head("入金消込", "全銀明細と請求の突合。") + picker
                              + ui.empty("入金明細を確認できませんでした。ファイル形式と内容を確認してください。"))
        def _sec(title, rows_html, note=""):
            return (f'<div class="ri-sech" style="margin-top:16px">{_esc(title)}</div>'
                    + (rows_html or ui.empty("なし")) + (f'<div class="gn">{note}</div>' if note else ""))
        # 自動消込候補（金額+名義一致）
        mrows = []
        for m in r.get("matched", []):
            btn = (_op_button("billing_reconcile", {"billing_id": m["invoice_id"]}, "消込を確定", viewer)
                   if can else "")
            mrows.append(f'<div class="bulk-row"><span class="bulk-t">¥{_esc(str(m["amount"]))} '
                         f'{_esc(m["payer"])}</span><span class="lref">{_esc(m["invoice_id"])}</span>{btn}</div>')
        rev = "".join(f'<div class="bulk-row"><span class="bulk-t">¥{_esc(str(x["amount"]))} '
                      f'入金:{_esc(x["payer"])} / 請求:{_esc(x.get("invoice_payer",""))}</span>'
                      f'<span class="qchip">要確認</span></div>' for x in r.get("amount_only_review", []))
        undep = "".join(f'<div class="bulk-row"><span class="bulk-t">¥{_esc(str(x["amount"]))} '
                        f'{_esc(x["payer"])}</span><span class="qchip">請求なし</span></div>'
                        for x in r.get("unmatched_deposit", []))
        body += (f'<div class="gn" style="margin-top:8px">入金{r["deposits"]}件・未消込請求{r["invoices"]}件</div>'
                 + _sec("自動消込候補（金額+名義一致）", '<div class="bulk-list">' + "".join(mrows) + '</div>' if mrows else "",
                        "確定は経理/責任者/代表のみ・1件ずつ人間が確認。実送金は行いません（消込の記録のみ）。")
                 + _sec("要確認（金額一致・名義相違）", '<div class="bulk-list">' + rev + '</div>' if rev else "")
                 + _sec("消込先の請求なし", '<div class="bulk-list">' + undep + '</div>' if undep else ""))
    inner = (ui.page_head("入金消込", "全銀明細と未消込請求を突合します。自動消込は金額＋名義一致のみ・"
             "確定は人間（経理/責任者/代表）・実送金はしません。") + body)
    return _wrap_main("ledger", "/reconcile", "入金消込", inner)


def render_search(data_dir: Path, params) -> str:
    """横断全文検索（G1）: 案件/顧客/物件/書類/抽出値をFTS5で横断検索。読み取り専用。"""
    from hub_core import search as _search
    q = _public_display_param(params.get("q", [""])[0])
    if q:
        try:
            _search.build_search_index(data_dir)   # ops作成データを即反映（索引は派生・小データで軽量）
        except Exception:
            pass
    results = _search.search(data_dir, q) if q else []
    box = ('<form method="get" action="/search" class="ri-actform" style="margin-bottom:16px">'
           f'<input name="q" value="{_esc(q)}" placeholder="案件・顧客・物件・書類を横断検索…" '
           'autofocus style="min-width:360px;font-size:19px">'
           '<button class="ri-go" type="submit">検索</button></form>')
    if not q:
        body = box + ui.empty("キーワードを入力してください（3文字以上で中間一致・案件/顧客/物件/書類/抽出値を横断）。")
    elif not results:
        body = box + ui.empty(f"「{_esc(q)}」に一致する項目はありません。")
    else:
        rows = "".join(
            f'<a class="lcard" href="{_esc(r["link"])}"><div class="lc-head">'
            f'<span class="qchip">{_esc(_doc_kind_label(r.get("kind")))}</span>'
            f'<span class="lc-title">{_esc(_visible_data_value("検索結果", r.get("title")))}</span></div>'
            f'<div class="lc-meta">{_esc(_visible_data_value("検索内容", r.get("snippet")))}'
            + (f' <span class="lref">{_esc(_visible_data_value("参照", r.get("ref")))}</span>' if r.get("ref") else "")
            + '</div></a>'
            for r in results)
        body = box + f'<div class="gn" style="margin-bottom:8px">{len(results)}件</div><div class="lcards">{rows}</div>'
    inner = (ui.page_head("横断検索", "案件・顧客・物件・書類・抽出値を1つの索引で横断検索します（ローカル・全文検索）。")
             + body)
    return _wrap_main("home", "/search", "検索", inner)


def _conn_card(title, desc, status_ok, status_text, body_html):
    dot = "sov-ok" if status_ok else "sov-warn"
    st = ("接続済み" if status_ok else "未接続")
    return (f'<div class="conn-card"><div class="conn-h"><div class="conn-t">{_esc(title)}</div>'
            f'<span class="sov-badge {dot}"><span class="sov-dot"></span>{_esc(st)}</span></div>'
            f'<div class="conn-d">{_esc(desc)}</div>'
            f'{("<div class=" + chr(34) + "conn-note" + chr(34) + ">" + _esc(status_text) + "</div>") if status_text else ""}'
            f'{body_html}</div>')


def _merged_connection_params(data_dir: Path, kind: str, params: dict) -> dict:
    """空欄送信時だけ保存済み設定を補い、秘密をHTMLへ再表示せず再利用する。"""
    from hub_core import connections

    supplied = params if isinstance(params, dict) else {}

    def pick(name, saved=""):
        value = supplied.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return saved
        return value

    if kind == "smtp":
        saved = connections.load_smtp_config(data_dir)
        return {
            "host": pick("host", saved.get("host") or ""),
            "port": pick("port", saved.get("port") or "587"),
            "user": pick("user", saved.get("user") or ""),
            "password": pick("password", connections.smtp_password(data_dir)),
            "tls": pick("tls", saved.get("tls") if saved.get("tls") is not None else "1"),
        }
    if kind == "fax":
        saved = connections.load_fax_config(data_dir)
        return {
            "service_name": pick("service_name", saved.get("service_name") or ""),
            "endpoint": pick("endpoint", saved.get("endpoint") or ""),
            "method": pick("method", saved.get("method") or "POST"),
            "auth_style": pick("auth_style", saved.get("auth_style") or "bearer"),
            "from_number": pick("from_number", saved.get("from_number") or ""),
            "token": pick("token", connections.fax_token(data_dir)),
        }
    return dict(supplied)


def render_connections(data_dir: Path, params) -> str:
    """接続設定＝AI/メール送信/災害リスクなどの外部サービスを非技術者が自力で繋ぐ。
    各連携に「接続テスト」を用意し、本当に繋がったかを可視化する。責任者/代表のみ編集。"""
    from hub_core import backup as _backup
    from hub_core import connections, sovereignty, prs_juusetsu
    viewer = current_viewer()
    can_edit = bool(viewer and viewer.role in ("責任者", "代表"))
    smtp = connections.load_smtp_config(data_dir)
    ro = "" if can_edit else " readonly"

    # ローカルAI（Ollama）
    ai_body = (
        '<div class="conn-guide">パソコンにローカルAIを入れると、データを外に出さずにAIが使えます。'
        '<a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a>と対応モデルを準備してから、'
        '接続を確認してください。</div>'
        '<button class="ri-qbtn" onclick="connTest(\'ollama\',{},this)">接続テスト</button>'
        '<span class="conn-result" data-for="ollama"></span>')

    # メール送信（SMTP）
    smtp_ok = connections.smtp_configured(data_dir)
    smtp_configured_hint = "設定済み（変更時のみ入力）"
    smtp_body = (
        '<div class="conn-grid">'
        f'<div class="conn-row"><label class="pf-l">サーバ（SMTP）</label><input type="text" id="smtpHost" value="" placeholder="{smtp_configured_hint if smtp.get("host") else "smtp.gmail.com"}"' + ro + '></div>'
        f'<div class="conn-row"><label class="pf-l">ポート</label><input type="text" id="smtpPort" value="" placeholder="{smtp_configured_hint if smtp.get("port") else "587"}"' + ro + '></div>'
        f'<div class="conn-row"><label class="pf-l">ユーザー名（メール）</label><input type="text" id="smtpUser" value="" placeholder="{smtp_configured_hint if smtp.get("user") else "you@example.com"}"' + ro + '></div>'
        f'<div class="conn-row"><label class="pf-l">パスワード（アプリパスワード）</label><input type="password" id="smtpPass" placeholder="' + ("設定済み（変更時のみ入力）" if smtp_ok else "アプリパスワード") + '"' + ro + '></div>'
        '</div>'
        '<div class="conn-guide">Gmailの場合は「アプリパスワード」を作成して使います（通常のログインパスワードは不可）。'
        'テストではメールは送りません（認証確認のみ）。</div>'
        '<button class="ri-qbtn" onclick="connTestSmtp(this)">接続テスト</button>'
        + ('<button class="ri-qbtn" onclick="connSaveSmtp(this)">保存</button>' if can_edit else '')
        + '<span class="conn-result" data-for="smtp"></span>')

    # 災害リスク（PRS）
    prs_ok = prs_juusetsu.configured()
    prs_last = prs_juusetsu.load_last_receipt(data_dir)
    prs_last_text = ""
    if prs_last:
        prs_last_text = (
            '<div class="conn-guide"><b>最終取得receipt</b><br>'
            + _esc(str(prs_last.get("receipt_id") or "IDなし"))
            + ' ／ 取得 ' + _esc(str(prs_last.get("received_at") or ""))
            + ' ／ response SHA-256 ' + _esc(str(prs_last.get("response_sha256") or ""))
            + '</div>')
    prs_body = (
        '<div class="conn-guide">物件住所からPRSの <b>juusetsu-hazard-v1</b> を取得し、'
        '土砂・津波・造成宅地と洪水／内水／高潮を別々に重説下書きへ差し込みます。'
        'この操作は<b>住所を接続先へ送信</b>します。一次原典が無い項目は要確認のまま残ります。</div>'
        + prs_last_text
        + '<button class="ri-qbtn" onclick="connTest(\'prs\',{},this)">PRS重説調査の接続テスト</button>'
        '<span class="conn-result" data-for="prs"></span>')

    # LINE連携。接続テストは送信先一覧を1件読むだけで、メッセージは送らない。
    line_ok = connections.harness_configured()
    line_body = (
        '<div class="conn-guide">お使いのLINE連携サービスに接続できるか確認します。'
        '<b>接続テストは送信先一覧を1件だけ読み取り、メッセージは送りません。</b>'
        '接続情報は管理者が起動時の安全な設定に入れます。</div>'
        '<button class="ri-qbtn" type="button" onclick="connTest(\'harness\',{},this)">'
        'LINEの接続テスト</button>'
        '<span class="conn-result" data-for="harness"></span>')

    # 電子契約
    esign_body = (
        '<div class="conn-guide">クラウドサイン等の電子契約サービスを差し込む枠があります。'
        '実際の接続はサービスの契約とキー投入が必要です（各社の管理画面で発行）。</div>')

    fax_cfg = connections.load_fax_config(data_dir)
    fax_has_token = bool(connections.fax_token(data_dir))
    fax_ok = bool(fax_cfg.get("endpoint") and fax_has_token)
    fax_configured_hint = "設定済み（変更時のみ入力）"
    backup_ready = _backup.portable_crypto_available()
    fax_body = (
        '<div class="conn-guide">物確（物件確認）のFAXを、お使いのクラウドFAXサービスから送れるようにします。'
        '契約したサービスの管理画面で<b>送信APIのURL</b>と<b>APIトークン</b>を発行し、ここに入れてください。'
        '特定の会社に固定していないので、どのサービスでも使えます。'
        '<br><b>ここで登録しても、FAXは勝手に送られません。</b>送るたびに「送信する」を押す確認が入ります。</div>'
        '<div class="pf-grid">'
        '<div class="pf-f"><label class="pf-l" for="faxName">サービス名（控え）</label>'
        f'<input type="text" id="faxName" value="" placeholder="{fax_configured_hint if fax_cfg.get("service_name") else "サービス名"}"{ro}></div>'
        '<div class="pf-f"><label class="pf-l" for="faxEndpoint">送信APIのURL</label>'
        f'<input type="text" id="faxEndpoint" placeholder="{fax_configured_hint if fax_cfg.get("endpoint") else "https://..."}" value=""{ro}></div>'
        '<div class="pf-f"><label class="pf-l" for="faxFrom">発信番号</label>'
        f'<input type="text" id="faxFrom" placeholder="{fax_configured_hint if fax_cfg.get("from_number") else "03-0000-0000"}" value=""{ro}></div>'
        '<div class="pf-f"><label class="pf-l" for="faxAuth">トークンの渡し方</label>'
        '<select id="faxAuth" style="font-size:19px;padding:10px 12px;border:2px solid var(--ai-rule-strong);border-radius:10px;min-height:48px">'
        + "".join(f'<option value="{v}"{" selected" if (fax_cfg.get("auth_style") or "bearer")==v else ""}>{_esc(l)}</option>'
                  for v, l in (("bearer", "標準認証"), ("token", "トークン認証"), ("x-api-key", "APIキー認証")))
        + '</select></div>'
        '<div class="pf-f"><label class="pf-l" for="faxToken">APIトークン</label>'
        f'<input type="password" id="faxToken" placeholder="{fax_configured_hint if fax_has_token else "貼り付けてください"}"'
        + ro + '></div></div>'
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">'
        '<button class="ri-qbtn" type="button" onclick="connTestFax(this)">接続テスト（FAXは送りません）</button>'
        + ('<button class="ri-go" type="button" onclick="connSaveFax(this)">保存する</button>' if can_edit else '')
        + '<span class="conn-result" data-for="fax"></span></div>')

    cards = (
        _conn_card("ローカルAI（Ollama）", "データを外に出さずにAIを使う。無料。",
                   connections.test_ollama().get("ok", False), "", ai_body)
        + _conn_card("メール送信（SMTP）", "顧客への連絡メールを自分のメールアカウントから送る。",
                     smtp_ok, "設定済み" if smtp_ok else "", smtp_body)
        + _conn_card("PRS重説調査", "一次原典付きの6項目を重説下書きへ差し込む。",
                     prs_ok, "juusetsu-hazard-v1 接続設定あり" if prs_ok else "未接続", prs_body)
        + _conn_card("LINE連携", "お客様とのLINE窓口が利用できるか、送信せずに確かめる。",
                     line_ok, "接続情報あり" if line_ok else "未設定", line_body)
        + _conn_card("FAX送信", "物確FAXをクラウドFAXサービスから送る。送信のたびに確認が入ります。",
                     fax_ok, "設定済み" if fax_ok else "未設定（実際には送りません）", fax_body)
        + _conn_card("電子契約", "利用する電子契約サービスを接続する。", False, "", esign_body)
        + _conn_card("暗号化バックアップ", "顧客・物件・書類・監査記録を暗号化して手元に保存する。",
                     backup_ready,
                     ("AES-256-GCM / 復旧キーは必ず別保管"
                      if backup_ready else "標準暗号が利用できないため停止中"),
                     ('<div class="conn-guide">バックアップ本体はAES-256-GCMで保護されます。'
                         '別のパソコンで監査記録を検証するための鍵も、暗号化された本体の内側に保持します。'
                         '<br><b>復旧キーは別保管してください。</b>バックアップ本体と同じフォルダや同じ送付先に置かないでください。'
                     '</div>' if backup_ready else
                      '<div class="conn-guide">標準暗号を利用できないため、バックアップ機能は停止中です。'
                      '弱い暗号方式では書き出しません。</div>')
                     + (('<form method="post" action="/api/backup" style="display:inline-block">'
                         '<button class="ri-qbtn" type="submit">暗号化バックアップを保存</button></form>'
                         '<form method="post" action="/api/backup/recovery-key" style="display:inline-block;margin-left:8px">'
                         '<button class="ri-qbtn" type="submit">復旧キーを別に保存</button></form>')
                        if can_edit and backup_ready else
                        ('<div class="gn">保存できるのは責任者・代表のみです。</div>'
                         if backup_ready else ''))))

    js = ('<script>'
          'async function connTest(kind,params,btn){var s=btn.parentElement.querySelector(".conn-result[data-for="+kind+"]");'
          's.textContent="確認中…";s.className="conn-result";btn.disabled=true;'
          'try{var r=await fetch("/api/conn-test",{method:"POST",headers:{"Content-Type":"application/json"},'
          'body:JSON.stringify({kind:kind,params:params})});var j=await r.json();'
          's.textContent=(j.detail||"");s.className="conn-result "+(j.ok?"conn-ok":"conn-ng");}'
          'catch(e){s.textContent="確認に失敗しました";s.className="conn-result conn-ng";}btn.disabled=false;}'
          'function smtpParams(){return {host:document.getElementById("smtpHost").value,port:document.getElementById("smtpPort").value,'
          'user:document.getElementById("smtpUser").value,password:document.getElementById("smtpPass").value,tls:"1"};}'
          'function connTestSmtp(btn){connTest("smtp",smtpParams(),btn);}'
          'function faxParams(){return {service_name:document.getElementById("faxName").value,'
          'endpoint:document.getElementById("faxEndpoint").value,from_number:document.getElementById("faxFrom").value,'
          'auth_style:document.getElementById("faxAuth").value,token:document.getElementById("faxToken").value};}'
          'function connTestFax(btn){connTest("fax",faxParams(),btn);}'
          'async function connSaveFax(btn){var s=btn.parentElement.querySelector(".conn-result[data-for=fax]");'
          's.textContent="保存中…";btn.disabled=true;try{var r=await fetch("/connections/save-fax",{method:"POST",'
          'headers:{"Content-Type":"application/json"},body:JSON.stringify(faxParams())});var j=await r.json();'
          's.textContent=r.ok?"保存しました":("保存に失敗: "+(j.error||r.status));'
          's.className="conn-result "+(r.ok?"conn-ok":"conn-ng");}catch(e){s.textContent="保存に失敗しました";'
          's.className="conn-result conn-ng";}btn.disabled=false;}'
          'async function connSaveSmtp(btn){var s=btn.parentElement.querySelector(".conn-result[data-for=smtp]");s.textContent="保存中…";'
          'btn.disabled=true;try{var r=await fetch("/connections/save",{method:"POST",headers:{"Content-Type":"application/json"},'
          'body:JSON.stringify(smtpParams())});var j=await r.json();s.textContent=r.ok?"保存しました":("保存に失敗: "+(j.error||r.status));'
          's.className="conn-result "+(r.ok?"conn-ok":"conn-ng");}catch(e){s.textContent="保存に失敗しました";s.className="conn-result conn-ng";}btn.disabled=false;}'
          '</script>')

    inner = (ui.page_head("接続設定",
             "AI・メール送信・LINE・FAX送信・災害リスクなどの外部サービスをここで繋ぎます。"
             "各サービスの「接続テスト」で、本当に繋がったかを確認できます。")
             + _sovereignty_badge(data_dir) + '<div class="conn-wrap">' + cards + '</div>' + js)
    return _wrap_main("home", "/connections", "接続設定", inner)


def render_llm_settings(data_dir: Path, params) -> str:
    """一般配布版のLLMセレクタ。変更は責任者/代表だけが行える。"""
    from hub_core import chat_llm
    cfg = chat_llm.load_mode_config(data_dir)
    cur_provider = str(cfg.get("provider") or "").strip().lower()
    cur_model = cfg.get("model") or ""
    cur_base = cfg.get("base_url") or ""
    viewer = current_viewer()
    can = bool(viewer and viewer.role in ("責任者", "代表"))

    selected_mode = "none"
    if cur_provider == "anthropic":
        selected_mode = "anthropic"
    elif cur_provider == "openai" and chat_llm._is_local_host(
            cur_base or chat_llm.DEFAULT_LOCAL_BASE):
        selected_mode = "local"
    elif cur_provider:
        selected_mode = "legacy"
    cur_label = next((m["label"] for m in _LLM_MODES if m["mode"] == selected_mode),
                     "旧設定（一般配布版では未対応・停止中）")
    metrics = ('<div class="ri-kpis">'
               f'<div class="kpi"><div class="n" style="font-size:18px">{_esc(cur_label)}</div><div class="l">現在のモード</div></div>'
               f'<div class="kpi"><div class="n" style="font-size:18px">{_esc(cur_model or ("—" if selected_mode == "none" else "既定"))}</div><div class="l">モデル</div></div>'
               f'<div class="kpi"><div class="n">{_esc(_ai_key_state().split(chr(183))[0].strip() if chr(183) in _ai_key_state() else _ai_key_state())}</div><div class="l">状態</div></div></div>')

    # モード比較表
    mrows = "".join(
        f'<tr><td>{_esc(m["label"])}</td><td>{_esc(m["model"] or "—")}</td>'
        f'<td>{_esc(m["privacy"])}</td><td>{_esc(m["cost"])}</td>'
        f'<td class="muted">{_esc(m["note"])}</td></tr>'
        for m in _LLM_MODES)
    mtable = ('<div class="tablewrap"><table><thead><tr><th>モード</th><th>モデル</th>'
              '<th>プライバシー</th><th>コスト</th><th>備考</th></tr></thead>'
              f'<tbody>{mrows}</tbody></table></div>')

    # タスク別推奨表
    rrows = "".join(f'<tr><td>{_esc(a)}</td><td>{_esc(b)}</td><td class="muted">{_esc(c)}</td></tr>'
                    for a, b, c in _LLM_TASK_RECO)
    rtable = ('<div class="tablewrap"><table><thead><tr><th>用途</th><th>推奨</th><th>理由</th></tr></thead>'
              f'<tbody>{rrows}</tbody></table></div>')

    switch = ""
    if can:
        opts = "".join(
            f'<label class="chip" style="display:flex;align-items:center;min-height:48px;text-align:left;margin:4px 0">'
            f'<input type="radio" name="llm_mode" value="{m["mode"]}"'
            f'{" checked" if m["mode"] == selected_mode else ""}> {_esc(m["label"])}</label>'
            for m in _LLM_MODES)
        switch = (
            '<div class="ri-sech" style="margin-top:22px">モードを切り替える（実行時・いつでも変更可）</div>'
            '<form method="post" action="/llm/save" class="ri-actform" style="flex-direction:column;align-items:stretch;gap:6px;max-width:640px">'
            f'{opts}'
            '<div class="mf-row" style="grid-template-columns:120px 1fr;margin-top:6px"><span class="mf-l">モデル名（任意）</span>'
            f'<input name="model" value="{_esc(cur_model)}" placeholder="例: qwen3:8b / claude-haiku-4-5"></div>'
            '<div class="mf-row" style="grid-template-columns:120px 1fr"><span class="mf-l">APIキー（任意）</span>'
            '<input name="api_key" type="password" placeholder="③ Anthropicのキー。同期フォルダ外に保存"></div>'
            '<div class="mf-actions"><button class="ri-go" type="submit">この設定に切り替える</button>'
            '<span class="gn">APIキーの入力と外部接続は、責任者/代表が明示的に選んだ時だけ行います。</span></div>'
            '</form>')
    else:
        switch = '<div class="gn" style="margin-top:16px">モードの変更は 責任者/代表 のみ。</div>'

    inner = (ui.page_head("AI設定",
             "AIなし・この端末のOllama・自前のAnthropic APIから選べます。既定では外部へ送りません。")
             + metrics
             + ui.section("モード比較") + mtable
             + ui.section("用途別のおすすめ") + rtable
             + switch)
    return _wrap_main("console", "/llm", "AI設定", inner)


def render_billing(data_dir: Path, params) -> str:
    """課金明細（M-metering・テストモード）。PRS等の従量利用を集計。**実請求は発火しない**。
    実際の課金（Stripe等）接続は人間ゲート＝画面に「テストモード・未請求」を常時明示。"""
    from hub_core import metering
    tenant = (params.get("tenant", [""])[0] or "self").strip()
    start = (params.get("start", [""])[0] or "").strip() or None
    end = (params.get("end", [""])[0] or "").strip() or None
    try:
        stmt = metering.billing_statement(data_dir, tenant, start=start, end=end)
        err = ""
    except ValueError:
        stmt, err = None, "利用実績を集計できませんでした。期間の指定を確認してください。"

    # 最重要シグナル=朱(機能色)の罫線注記（塗り箱でなく紙イディオム・GATE-PV是正）
    gate = ('<div style="border-left:3px solid var(--vermi);padding:8px 0 8px 12px;margin-bottom:16px">'
            '<div style="font-family:var(--head);font-weight:700;font-size:18px;color:var(--vermi)">'
            '朱書き: テストモード（未請求）</div>'
            '<div style="font-size:18px;color:var(--ink2);margin-top:3px;max-width:72ch">'
            'これは利用実績の試算です。実際の請求・課金は行いません。'
            '課金サービス（Stripe等）の接続・キー投入・実請求の発火は人間の承認が必要です。</div></div>')

    if err:
        body = gate + ui.empty(_esc(err))
    elif not stmt or not stmt["lines"]:
        body = gate + ui.empty(f"対象「{_esc(_tenant_label(tenant))}」の利用実績がありません。")
    else:
        rows = "".join(
            f'<tr><td>{_esc(_jp(li["product"]))}</td><td>{li["quantity"]}</td>'
            f'<td>{("¥" + format(li["unit_price"], ",")) if li["priced"] else "未価格"}</td>'
            f'<td>{("¥" + format(li["amount"], ",")) if li["priced"] else "—"}</td></tr>'
            for li in stmt["lines"])
        table = ('<div class="tablewrap"><table><thead><tr><th>項目</th><th>数量</th>'
                 '<th>単価</th><th>金額</th></tr></thead><tbody>' + rows
                 + f'<tr><td colspan="3" style="text-align:right;font-weight:700">合計（試算）</td>'
                   f'<td style="font-weight:700">¥{stmt["total"]:,}</td></tr></tbody></table></div>')
        unpriced = (f'<div class="gn" style="margin-top:6px">未価格の項目 '
                    f'{len(stmt["unpriced_products"])}件（契約単価の設定待ち）。</div>'
                    if stmt["unpriced_products"] else "")
        body = (gate
                + '<div class="ri-kpis">'
                + f'<div class="kpi"><div class="n">¥{stmt["total"]:,}</div><div class="l">今期の試算合計</div></div></div>'
                + f'<div class="gn" style="margin:-14px 0 14px">対象テナント: {_esc(_tenant_label(tenant))}</div>'
                + ui.section("利用明細（試算）") + table + unpriced)

    inner = (ui.page_head("課金（テストモード）",
             "PRS等の従量利用を集計した試算です。実請求は発火しません（課金の接続・発火は人間ゲート）。")
             + body)
    return _wrap_main("ledger", "/billing", "課金", inner)


def render_analytics(data_dir: Path, params) -> str:
    """媒体別実績・活動サマリ（M-analytics）。台帳の実データのみ集計。広告費が無いため
    ROI（費用対効果）は出さず、反響数・成約・成約率＝媒体別実績として表示（過大主張しない）。"""
    from hub_core import analytics
    media = analytics.media_performance(data_dir)
    act = analytics.activity_summary(data_dir, days=30)
    funnel = analytics.conversion_funnel(data_dir)
    income = analytics.income_summary(data_dir)

    if media:
        mrows = "".join(
            f'<tr><td>{_esc(_jp(m["media"]))}</td><td>{m["leads"]}</td>'
            f'<td>{m["conversions"]}</td><td>{m["conv_rate"]*100:.1f}%</td>'
            f'<td class="muted">{("¥" + format(m["cpa"], ",")) if m.get("cpa") else "費用データなし"}</td></tr>'
            for m in media)
        mtable = ('<div class="tablewrap"><table><thead><tr><th>媒体</th><th>反響数</th>'
                  '<th>成約</th><th>成約率</th><th>反響単価</th></tr></thead>'
                  f'<tbody>{mrows}</tbody></table></div>')
    else:
        mtable = ui.empty("反響データがありません（反響を取り込むと媒体別に集計します）。")

    ch = act["by_channel"]
    chrows = ("".join(f'<tr><td>{_esc(_jp(k))}</td><td>{v}</td></tr>' for k, v in ch.items())
              if ch else '<tr><td class="muted" colspan="2">直近30日の接触記録はありません</td></tr>')
    ctable = ('<div class="tablewrap"><table><thead><tr><th>チャネル</th><th>接触回数</th></tr></thead>'
              f'<tbody>{chrows}</tbody></table></div>')

    # 転換ファネル（反響→内見→申込→契約）
    if funnel["total"]:
        frows = "".join(
            f'<tr><td>{_esc(s["stage"])}</td><td>{s["count"]}</td>'
            f'<td class="muted">{("—" if s["rate_from_prev"] is None else f"{s['rate_from_prev']*100:.0f}%")}</td></tr>'
            for s in funnel["stages"])
        ftable = ('<div class="tablewrap"><table><thead><tr><th>段階</th><th>到達件数</th>'
                  '<th>前段階からの転換</th></tr></thead>'
                  f'<tbody>{frows}</tbody></table></div>')
    else:
        ftable = ui.empty("案件がありません（反響を案件化すると転換ファネルを集計します）。")

    # 失注理由分析（理由別・失注時ステージ別。蓄積が仕入れ・掲載改善の源泉）
    lost = analytics.lost_summary(data_dir)
    if lost["total"]:
        lrows = "".join(f'<tr><td>{_esc(k)}</td><td>{v}</td></tr>' for k, v in lost["by_reason"])
        srows = "".join(f'<tr><td>{_esc(k)}</td><td>{v}</td></tr>' for k, v in lost["by_stage"])
        ltable = ('<div class="tablewrap"><table><thead><tr><th>失注理由</th><th>件数</th></tr></thead>'
                  f'<tbody>{lrows}</tbody></table></div>'
                  '<div class="tablewrap"><table><thead><tr><th>失注時ステージ</th><th>件数</th></tr></thead>'
                  f'<tbody>{srows}</tbody></table></div>')
    else:
        ltable = ui.empty("失注記録がありません（失注時に理由を記録すると仕入れ・掲載改善の源泉になります）。")

    # 収支サマリ（請求 vs 消込）
    if income["count"]:
        itable = ('<div class="tablewrap"><table><thead><tr><th>項目</th><th>金額</th></tr></thead>'
                  '<tbody>'
                  f'<tr><td>請求合計</td><td>¥{income["billed"]:,}</td></tr>'
                  f'<tr><td>消込済（回収）</td><td>¥{income["collected"]:,}</td></tr>'
                  f'<tr><td>未回収</td><td>¥{income["outstanding"]:,}</td></tr>'
                  f'<tr><td>回収率</td><td>{income["collection_rate"]*100:.0f}%</td></tr>'
                  '</tbody></table></div>')
    else:
        itable = ui.empty("請求データがありません（請求を作成すると収支を集計します）。")

    inner = (ui.page_head("実績・活動",
             "台帳の実データから媒体別の反響・成約と直近の活動を集計します。数値は実績であり、"
             "成約や費用対効果を保証するものではありません。")
             + ui.section("収支サマリ（請求・回収・未回収）") + itable
             + ui.section(f'転換ファネル（反響→内見→申込→契約・全{funnel["total"]}案件）') + ftable
             + ui.section(f'失注理由分析（全{lost["total"]}件）') + ltable
             + ui.section("媒体別実績（反響・成約・成約率）") + mtable
             + f'<div class="gn" style="margin-top:6px">反響単価は広告費を登録した媒体のみ表示します（費用データが無い媒体は「費用データなし」）。</div>'
             + ui.section(f'活動サマリ（直近{act["days"]}日・接触{act["total"]}件）') + ctable)
    return _wrap_main("properties", "/analytics", "実績・活動", inner)



def _keisan_own_price(current: int) -> str:
    """自分の金額を入れる欄。実務の売買代金はきりのいい数字にならない。

    このページは読み取り専用（フォーム送信をしない契約）なので、入力後は
    リンクと同じく location で移動する（他画面の select と同じやり方）。
    """
    js = ("var v=document.getElementById('kPrice').value.replace(/[^0-9]/g,'');"
          "if(v){location.href='/keisan?price='+v+'&kind='"
          "+encodeURIComponent(new URLSearchParams(location.search).get('kind')||'中古');}")
    return (f'<div class="ri-sech" style="margin-top:10px">'
            f'いまの売買代金 <b>¥{current:,}</b> で計算しています</div>'
            '<div class="facets" style="margin-top:6px">'
            '<label class="flabel" for="kPrice">自分の金額で:</label>'
            f'<input id="kPrice" type="text" inputmode="numeric" value="{current}" '
            'style="font-size:20px;padding:10px 13px;border:2px solid var(--ai-rule-strong);'
            'border-radius:10px;min-height:48px;width:190px">'
            '<span style="font-size:19px;margin:0 4px">円</span>'
            f'<a href="#" class="ri-go" style="min-height:48px;display:inline-flex;'
            f'align-items:center" onclick="{_esc(js)}return false;">'
            'この金額で計算する</a></div>')


def render_keisan(data_dir: Path, params) -> str:
    """費用計算（M-keisan/PRO-06）: 参考の上限手数料・印紙・諸費用概算・ローン月々。
    全て参考値（確定でない）。入力は GET クエリ（読み取り専用ページ契約=form不可のためリンク/クエリ駆動）。"""
    from hub_core import keisan
    def _int(name, default):
        try:
            return max(0, int((params.get(name, [""])[0] or "").replace(",", "") or default))
        except ValueError:
            return default
    price = _int("price", 30_000_000)
    down = _int("down", 0)
    years = max(1, _int("years", 35))   # years=0/負は元利均等でn=0クラッシュ→1にフロア(R3#6)
    try:
        rate = float((params.get("rate", [""])[0] or "1.5")) / 100.0
    except ValueError:
        rate = 0.015
    kind = (params.get("kind", [""])[0] or "中古")
    if kind not in ("中古", "新築"):   # allowlist（XSS防御・不正値は既定へ）
        kind = "中古"
    s = keisan.summary(price, kind=kind, annual_rate=rate, years=years, down_payment=down)
    b, st, bc, ln = s["brokerage"], s["stamp"], s["buyer_cost"], s["loan"]

    # クエリ値はすべて数値/allowlist化済みだが、リンク生成でも明示エスケープ（多層防御）
    def _q(pr, kd):
        return (f"/keisan?price={int(pr)}&kind={quote(kd)}&years={int(years)}"
                f"&rate={_esc(f'{rate*100:g}')}&down={int(down)}")
    # プリセット価格リンク（form禁止の読み取り専用契約＝GETリンクで操作）
    presets = "".join(
        f'<a class="facet{" on" if price == v else ""}" href="{_esc(_q(v, kind))}">{_esc(_yen(v))}</a>'
        for v in (10_000_000, 20_000_000, 30_000_000, 50_000_000, 80_000_000))
    kinds = "".join(
        f'<a class="facet{" on" if kind == k else ""}" href="{_esc(_q(price, k))}">{_esc(k)}</a>'
        for k in ("中古", "新築"))

    metrics = (
        '<div class="ri-kpis">'
        f'<div class="kpi"><div class="n">{_yen(b["fee_cap_incl_tax"])}</div><div class="l">仲介手数料の上限（税込）</div></div>'
        f'<div class="kpi"><div class="n">{_yen(st["stamp_duty"])}</div><div class="l">印紙税（本則）</div></div>'
        f'<div class="kpi"><div class="n">{_yen(bc["estimate_low"])}〜{_yen(bc["estimate_high"])}</div><div class="l">諸費用の概算</div></div>'
        + (f'<div class="kpi"><div class="n">{_yen(ln["monthly_payment"])}</div><div class="l">ローン月々（参考）</div></div>' if ln else "")
        + '</div>')

    rows = [
        ("媒介報酬の上限（税抜）", _yen(b["fee_cap_excl_tax"]), b["note"]),
        ("媒介報酬の上限（税込）", _yen(b["fee_cap_incl_tax"]), "上記に消費税を加算"),
        ("印紙税", _yen(st["stamp_duty"]), st["note"]),
        (f'諸費用概算（{kind}・{bc["rate_low"]*100:g}〜{bc["rate_high"]*100:g}%）',
         f'{_yen(bc["estimate_low"])} 〜 {_yen(bc["estimate_high"])}', bc["note"]),
    ]
    if ln:
        rows += [
            (f'ローン月々（{ln["years"]}年・{ln["annual_rate"]*100:g}%）', _yen(ln["monthly_payment"]), ln["note"]),
            ("総返済額", _yen(ln["total_payment"]), "元利均等・参考"),
            ("うち利息", _yen(ln["total_interest"]), "参考"),
        ]
    table = ('<div class="tablewrap"><table><thead><tr><th>項目</th><th>金額</th><th>備考</th></tr></thead><tbody>'
             + "".join(f'<tr><td>{_esc(k)}</td><td>{_esc(v)}</td><td class="muted">{_esc(n)}</td></tr>'
                       for k, v, n in rows)
             + '</tbody></table></div>')

    inner = (ui.page_head("費用計算（参考）",
             "売買代金から仲介手数料の上限・印紙税・諸費用の概算・ローン月々の目安を出します。"
             "すべて参考値で、確定額ではありません。")
             + '<div class="facets"><span class="flabel">売買代金:</span>' + presets + '</div>'
             + _keisan_own_price(price)
             + '<div class="facets"><span class="flabel">区分:</span>' + kinds + '</div>'
             + metrics
             + ui.section("内訳（参考）") + table
             + f'<div class="ri-alert" style="margin-top:14px;background:var(--warn-bg);color:var(--warn);border-radius:4px;padding:10px 13px;font-size:18px">{_esc(s["disclaimer"])}</div>')
    return _wrap_main("keisan", "/keisan", "費用計算", inner)


# あいのてのロゴをタイル化し、favicon として /favicon.svg で配る。
# data URI にすると xmlns の http:// が「外部URL禁止」ガードに引っかかるため自前ルートで返す。
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect x="3" y="3" width="94" height="94" rx="30" fill="#1B4DFF"/>'
    '<g fill="none" stroke="#fff" stroke-width="13" stroke-linecap="round"'
    ' transform="rotate(-10 50 50) translate(50 50) scale(0.74) translate(-50 -50)">'
    '<path d="M 57.98 52.77 A 20 20 0 1 0 57.98 35.23"/>'
    '<path d="M 42.02 64.77 A 20 20 0 1 1 42.02 47.23"/>'
    '<path d="M 47.07 62.71 A 20 20 0 0 1 36.08 63.61" stroke="#1B4DFF" stroke-width="20"/>'
    '<path d="M 47.07 62.71 A 20 20 0 0 1 36.08 63.61"/></g></svg>')

def build_response(data_dir: Path, path: str):
    """(status_code, html_str) を返す GET 専用ルータ。"""
    split = urlsplit(path)
    route = split.path
    params = parse_qs(split.query, keep_blank_values=False)

    if route == "/favicon.svg":
        return 200, FAVICON_SVG
    if route in ("", "/", "/home"):
        return 200, render_ri_workspace(data_dir, params)
    if route.rstrip("/") == "/juusetsu":
        return 200, render_juusetsu(data_dir, params)
    if route.rstrip("/") == "/case":
        return 200, render_case(data_dir, params)
    if route.rstrip("/") == "/timeline":
        return 200, render_timeline(data_dir, params)
    if route.rstrip("/") == "/keisan":
        return 200, render_keisan(data_dir, params)
    if route.rstrip("/") == "/analytics":
        return 200, render_analytics(data_dir, params)
    if route.rstrip("/") == "/billing":
        return 200, render_billing(data_dir, params)
    if route.rstrip("/") == "/llm":
        return 200, render_llm_settings(data_dir, params)
    if route.rstrip("/") == "/search":
        return 200, render_search(data_dir, params)
    if route.rstrip("/") == "/reconcile":
        return 200, render_reconcile(data_dir, params)
    if route.rstrip("/") == "/portal-export":
        return 200, render_portal_export(data_dir, params)
    if route.rstrip("/") == "/portal":
        return 200, render_customer_portal(data_dir, params)
    if route.rstrip("/") == "/renewals":
        return 200, render_renewals(data_dir, params)
    if route.rstrip("/") == "/money":
        return 200, render_money(data_dir, params)
    if route.rstrip("/") == "/pm":
        return 200, render_pm_dashboard(data_dir, params)
    if route.rstrip("/") == "/madori":
        return 200, render_madori(data_dir, params)
    if route.rstrip("/") == "/materials":
        return 200, render_materials(data_dir, params)
    if route.rstrip("/") == "/profile":
        return 200, render_profile(data_dir, params)
    if route.rstrip("/") == "/connections":
        return 200, render_connections(data_dir, params)
    if route.rstrip("/") == "/migrate":
        return 200, render_migrate(data_dir, params)
    if route.rstrip("/") == "/brand/history":
        return 200, render_brand_history(data_dir, params)
    if route.rstrip("/") == "/juusetsu/new":
        # 既定は窓口型（1画面1動作）。全項目を一度に見たい人は ?all=1 で従来の一枚もの。
        if (params.get("all", [""])[0] or "") == "1":
            return 200, render_juusetsu_new(data_dir, params)
        return 200, render_juusetsu_step(data_dir, params)
    if route.rstrip("/") == "/maisoku/new-form":
        # 既定は窓口型（1画面1動作）。全項目を一度に見たい人は ?all=1 で従来の一枚もの。
        if (params.get("all", [""])[0] or "") == "1":
            return 200, render_maisoku_new(data_dir, params)
        return 200, render_maisoku_step(data_dir, params)
    if route.rstrip("/") == "/property/collect":
        return 200, render_property_collect(data_dir, params)
    if route.rstrip("/") == "/fax":
        return 200, render_fax(data_dir, params)
    if route.rstrip("/") == "/calls":
        return 200, render_calls(data_dir, params)
    if route.rstrip("/") == "/line":
        return 200, render_line(data_dir, params)
    if route.rstrip("/") == "/reins":
        return 200, render_reins(data_dir, params)
    if route.rstrip("/") == "/it":
        return 200, render_it(data_dir, params)
    if route.rstrip("/") == "/agent":
        return 200, render_agent_mode(data_dir, params)
    if route.rstrip("/") == "/console":
        return 200, render_console(data_dir, params)
    if route.rstrip("/") == "/properties":
        return 200, render_properties(data_dir, params)
    if route.rstrip("/") == "/maisoku/edit":
        return 200, render_maisoku_edit(data_dir, params)
    if route.rstrip("/") == "/maisoku":
        return 200, render_maisoku(data_dir, params)
    if route.rstrip("/") == "/customers":
        return 200, render_customers(data_dir, params)
    if route.rstrip("/") == "/ledger":
        return 200, render_ledger(data_dir, params)
    if route.rstrip("/") == "/audit":
        return render_audit_status(data_dir, params)

    page = PAGE_BY_ROUTE.get(route) or PAGE_BY_ROUTE.get(route.rstrip("/"))
    if page is not None:
        return 200, render_screen(data_dir, page, params)
    return 404, render_not_found(data_dir, route)


SETUP_STEPS = ("company", "account", "ai")


def _setup_shell(inner: str, *, step: int, error: str = "") -> str:
    """はじめての設定の外枠。段ごとに中身だけ差し替える（見た目は1本）。"""
    err = f'<div class="alert">{_esc(error)}</div>' if error else ""
    sides = (("会社のこと", "社名と免許番号を入れます"),
             ("ログインを作る", "あなたが入るためのIDとパスワード"),
             ("AIをどうするか", "いまは無しでも始められます"))
    dots = "".join(
        f'<li class="{"done" if i < step else ("now" if i == step else "")}"'
        f'{" aria-current=\"step\"" if i == step else ""}>'
        '<span class="setup-knot" aria-hidden="true"><i></i><i></i></span>'
        f'<span class="sr-only">{_esc(title)}</span></li>'
        for i, (title, _description) in enumerate(sides))
    steps_html = "".join(
        f'<li><span class="num{"" if i <= step else " todo"}">{i + 1}</span>'
        f'<div><div class="st">{_esc(t)}</div><div class="sd">{_esc(d)}</div></div></li>'
        for i, (t, d) in enumerate(sides))
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>あいのて | はじめての設定</title>'
        # 初期設定画面だけでも第三者へ接続しないよう、外部フォントCDNは使わない。
        '<style>'
        ':root{--ink:#0E1116;--paper:#fff;--paper2:#f6f8fa;--line:#e3e8ee;--muted:#4A5158;'
        '--brand:#1638DB;--ok:#217645;--ok-bg:#eef7f1;'
        '--head:-apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic","Noto Sans JP",sans-serif}'
        '*{box-sizing:border-box}body{margin:0;background:#dfe2e6;font-family:var(--head);color:var(--ink)}'
        '.wrap{max-width:920px;margin:40px auto;background:var(--paper);border:1px solid var(--line);'
        'border-radius:16px;box-shadow:0 12px 34px rgba(17,20,24,.10);overflow:hidden;'
        'display:grid;grid-template-columns:1fr 280px}'
        '.main{padding:44px 48px 40px}.side{background:var(--paper2);border-left:1px solid var(--line);padding:40px 28px}'
        '.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}'
        '.setup-knotline{display:flex;list-style:none;margin:0 0 22px;padding:0;width:210px}'
        '.setup-knotline li{position:relative;flex:1;height:30px}'
        '.setup-knotline li::after{content:"";position:absolute;top:13px;left:calc(50% + 14px);width:calc(100% - 28px);height:3px;background:#cbd0d8}'
        '.setup-knotline li:last-child::after{display:none}'
        '.setup-knot{position:absolute;left:50%;top:1px;transform:translateX(-50%);width:30px;height:25px;background:#fff;z-index:1}'
        '.setup-knot i{position:absolute;top:5px;width:17px;height:14px;border:3px solid #cbd0d8;border-radius:9px}'
        '.setup-knot i:first-child{left:1px;transform:rotate(-12deg)}'
        '.setup-knot i:last-child{right:1px;transform:rotate(12deg)}'
        '.setup-knotline li.done::after{background:var(--brand)}'
        '.setup-knotline li.done .setup-knot i,.setup-knotline li.now .setup-knot i{border-color:var(--brand)}'
        'h1{font-size:32px;margin:0 0 10px;line-height:1.35}'
        '.lead{color:var(--muted);font-size:18px;margin-bottom:26px;line-height:1.7}'
        '.brand{font-weight:700;font-size:18px;margin-bottom:14px}.brand .s{color:#9aa1a9}'
        '.f{margin-bottom:20px}.lb{font-size:20px;font-weight:600;margin-bottom:8px}'
        '.hint{font-size:18px;color:var(--muted);margin-top:6px;line-height:1.6}'
        'input[type=text],input[type=password]{width:100%;border:2px solid #cbd0d8;border-radius:10px;'
        'padding:13px 14px;font-family:inherit;font-size:20px;min-height:48px}'
        'input:focus{outline:3px solid #1B4DFF;outline-offset:1px;border-color:#1B4DFF}'
        '.chips{display:flex;gap:10px;flex-wrap:wrap}'
        '.chip{font-size:19px;border:2px solid #cbd0d8;border-radius:12px;padding:0 18px;cursor:pointer;'
        'user-select:none;display:inline-flex;align-items:center;gap:9px;min-height:52px}'
        '.chip input{width:22px;height:22px;accent-color:var(--brand);margin:0}'
        '.chip:focus-within{outline:3px solid #1B4DFF;outline-offset:2px}'
        '.col{display:flex;flex-direction:column;gap:10px}'
        '.btn{font-weight:700;border:none;border-radius:12px;padding:0 26px;background:var(--brand);'
        'color:#fff;font-size:21px;min-height:56px;width:100%;cursor:pointer;margin-top:8px}'
        '.btn:focus-visible{outline:3px solid var(--ink);outline-offset:2px}'
        '.back{display:inline-flex;align-items:center;min-height:48px;margin-top:14px;font-size:18px;'
        'color:var(--brand);background:none;border:0;padding:0;font-family:inherit;cursor:pointer}'
        '.assure{display:flex;gap:10px;margin-top:22px;padding:14px 16px;background:var(--ok-bg);'
        'border-radius:12px;font-size:18px;color:var(--ok);line-height:1.6}'
        '.alert{background:#f6e6e6;color:#8f2a2a;border-radius:10px;padding:12px 14px;margin-bottom:18px;font-size:18px}'
        '.steps{list-style:none;padding:0;margin:0}.steps li{display:flex;gap:12px;margin-bottom:24px}'
        '.num{width:28px;height:28px;border-radius:50%;border:2px solid var(--ink);font-size:17px;'
        'font-weight:700;display:flex;align-items:center;justify-content:center;flex:none}'
        '.num.todo{border-color:#cbd0d8;color:#9aa1a9}'
        '.st{font-size:19px;font-weight:600}.sd{font-size:17px;color:var(--muted);margin-top:3px}'
        '@media(max-width:760px){.wrap{grid-template-columns:1fr}.side{display:none}}'
        '</style></head><body><div class="wrap"><div class="main">'
        '<div class="brand">あいのて <span class="s">/</span> はじめての設定</div>'
        f'<ol class="setup-knotline" aria-label="初回設定の進み具合">{dots}</ol>{err}{inner}</div>'
        f'<div class="side"><ol class="steps">{steps_html}</ol></div></div></body></html>')


def _hidden(form: dict, keys) -> str:
    out = ""
    for k in keys:
        for v in form.get(k, []):
            if v:
                out += f'<input type="hidden" name="{k}" value="{_esc(v)}">'
    return out


def render_setup(data_dir, error: str = "", form: dict | None = None, step: str = "company") -> str:
    """はじめての設定を1画面1話題に分ける（窓口型）。

    段の送りは **POST** にする。パスワードを URL に載せない（履歴・ログに残る）ため、
    GET で持ち回る他のウィザードとは意図的に別方式にしている。
    """
    f = form or {}
    profile_keys = ("company_name", "license_no", "business", "address", "tel",
                    "association", "fair_trade")
    account_keys = ("owner_user", "owner_pw", "owner_display_name")
    if step == "account":
        inner = (
            '<h1>あなたが入るための<br>IDとパスワードを決めます</h1>'
            '<div class="lead">この端末のあいのてに入るためのものです。'
            '外部のサービスには登録しません。</div>'
            '<form method="post" action="/setup/step">'
            + _hidden(f, profile_keys) +
            '<div class="f"><div class="lb">ログインID</div>'
            '<input type="text" name="owner_user" placeholder="daihyo" required '
            f'value="{_esc((f.get("owner_user", [""]) or [""])[0])}">'
            '<div class="hint">半角の英数字。あとから増やせます。</div></div>'
            '<div class="f"><div class="lb">あなたの名前</div>'
            '<input type="text" name="owner_display_name" placeholder="田中 花子" '
            f'value="{_esc((f.get("owner_display_name", [""]) or [""])[0])}">'
            '<div class="hint">台帳の担当者名と照合する名前です。空欄ならログインIDで照合します。</div></div>'
            '<div class="f"><div class="lb">パスワード（8文字以上）</div>'
            '<input type="password" name="owner_pw" placeholder="8文字以上" required>'
            '<div class="hint">忘れると入れません。手元に控えてください。</div></div>'
            '<button class="btn" type="submit" name="step" value="ai">つぎへ</button>'
            '<button class="back" type="submit" name="step" value="company" '
            'formnovalidate>ひとつ前へ</button></form>')
        return _setup_shell(inner, step=1, error=error)

    if step == "ai":
        inner = (
            '<h1>AIをどうしますか</h1>'
            '<div class="lead">いまは「使わない」で大丈夫です。'
            'あとから接続設定でいつでも足せます。</div>'
            '<form method="post" action="/setup">'
            + _hidden(f, profile_keys + account_keys) +
            '<div class="f"><div class="col">'
            '<label class="chip"><input type="radio" name="llm_mode" value="none" checked>'
            '<span>いまは使わない（あとで足せます・おすすめ）</span></label>'
            '<label class="chip"><input type="radio" name="llm_mode" value="local">'
            '<span>ローカルAIを使う（無料・AI処理はこの端末内）</span></label>'
            '<label class="chip"><input type="radio" name="llm_mode" value="anthropic">'
            '<span>自分のAPIキーを使う（氏名や住所が外部へ送られます）</span></label>'
            '</div></div>'
            '<div class="f"><div class="lb">APIキー（3番目を選んだ場合だけ）</div>'
            '<input type="password" name="api_key" placeholder="sk-ant-...">'
            '<div class="hint">同期フォルダの外に安全に保存します。空欄で構いません。</div></div>'
            '<button class="btn" type="submit">はじめる</button>'
            '<div class="assure">データは指定したローカル保存先に保存します。OSや保存先の同期設定も確認してください。'
            'いつでも全部CSVで書き出せるので、合わなければ元のやり方に戻せます。</div>'
            '</form>'
            '<form method="post" action="/setup/step">'
            + _hidden(f, profile_keys + ("owner_user", "owner_display_name")) +
            '<input type="hidden" name="step" value="account">'
            '<button class="back" type="submit">ひとつ前へ</button></form>')
        return _setup_shell(inner, step=2, error=error)

    selected_business = set(f.get("business", []))
    biz = "".join(
        f'<label class="chip"><input type="checkbox" name="business" value="{b}"'
        f'{" checked" if b in selected_business else ""}>'
        f'<span>{b}</span></label>' for b in ("賃貸仲介", "売買仲介", "管理"))
    inner = (
        '<h1>まず、会社のことを<br>教えてください</h1>'
        '<div class="lead">ここで入れた内容が、マイソクの帯（取扱業者欄）と'
        '重要事項説明書の業者欄に自動で入ります。'
        '<b>お客様に渡す書類は、すべてあなたの会社の名前で出ます。</b></div>'
        '<form method="post" action="/setup/step">'
        + _hidden(f, ("owner_user",)) +
        '<input type="hidden" name="step" value="account">'
        '<div class="f"><div class="lb">会社名</div>'
        '<input type="text" name="company_name" placeholder="株式会社みなと不動産" required '
        f'value="{_esc((f.get("company_name", [""]) or [""])[0])}"></div>'
        '<div class="f"><div class="lb">宅地建物取引業 免許番号</div>'
        '<input type="text" name="license_no" placeholder="東京都知事（2）第12345号" '
        f'value="{_esc((f.get("license_no", [""]) or [""])[0])}">'
        '<div class="hint">広告に必ず要る項目です。あとから入れることもできます。</div></div>'
        '<div class="f"><div class="lb">所在地</div>'
        '<input type="text" name="address" placeholder="東京都港区芝1-1-1" '
        f'value="{_esc((f.get("address", [""]) or [""])[0])}">'
        '<div class="hint">マイソクの取扱業者欄にそのまま入ります。</div></div>'
        '<div class="f"><div class="lb">電話番号</div>'
        '<input type="text" name="tel" placeholder="03-0000-0000" '
        f'value="{_esc((f.get("tel", [""]) or [""])[0])}"></div>'
        '<div class="f"><div class="lb">保証協会</div>'
        '<input type="text" name="association" placeholder="（公社）全国宅地建物取引業保証協会" '
        f'value="{_esc((f.get("association", [""]) or [""])[0])}"></div>'
        '<div class="f"><div class="lb">公正取引協議会</div>'
        '<input type="text" name="fair_trade" placeholder="首都圏不動産公正取引協議会" '
        f'value="{_esc((f.get("fair_trade", [""]) or [""])[0])}"></div>'
        '<div class="f"><div class="lb">主なお仕事（いくつでも）</div>'
        f'<div class="chips">{biz}</div></div>'
        '<button class="btn" type="submit">つぎへ</button></form>')
    return _setup_shell(inner, step=0, error=error)


def setup_company(data_dir, form):
    """オンボーディング送信を処理。(status, location, cookie) を返す。"""
    company_name = (form.get("company_name", [""])[0] or "").strip()
    license_no = (form.get("license_no", [""])[0] or "").strip()
    business = [b for b in form.get("business", []) if b]
    owner_user = (form.get("owner_user", [""])[0] or "").strip()
    owner_pw = form.get("owner_pw", [""])[0] or ""
    owner_display_name = (form.get("owner_display_name", [""])[0] or "").strip()
    if not company_name or not owner_user or len(owner_pw) < 8:
        return (400, None, None)
    # 業者情報一式（帯・重説に使い回す）
    prof = {"name": company_name, "license_no": license_no, "business": business}
    for k in ("tel", "fax", "email", "address", "association", "fair_trade", "staff", "holiday"):
        v = (form.get(k, [""])[0] or "").strip()
        if v:
            prof[k] = v
    # 履歴に残る経路へ一本化（直書きすると「いつ誰が何を変えたか」が消え、戻せなくなる）
    from hub_core import branding as _br
    _br.save(data_dir, prof, actor="setup", source="setup")   # 保存と履歴を一手で行う
    save_user(data_dir, owner_user, owner_pw, "代表", display_name=owner_display_name)
    # AIの頭脳(LLMモード)を保存。未知のPOST値はAIなしへ倒す。
    try:
        _save_public_llm_mode(Path(data_dir), form)
    except Exception:
        pass
    sid = create_session(Viewer(owner_user, "代表"))
    cookie = _session_cookie(sid)
    return (303, "/home", cookie)


def render_login(error: str = "") -> str:
    err = f'<p style="color:#f88;font-size:18px">{_esc(error)}</p>' if error else ""
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>あいのて | ログイン</title><style>{STYLE}</style></head><body>'
        '<div style="max-width:360px;margin:12vh auto;padding:28px;'
        'background:#11161c;border:1px solid #243;border-radius:10px">'
        '<h2 style="margin-top:0">あいのて</h2><p>この端末のアカウントでログイン</p>'
        f'{err}'
        '<form method="post" action="/login">'
        '<p><label>ユーザー<br><input name="user" autocomplete="username" '
        'style="width:100%;padding:8px;margin-top:4px"></label></p>'
        '<p><label>パスワード<br><input name="password" type="password" '
        'autocomplete="current-password" style="width:100%;padding:8px;margin-top:4px"></label></p>'
        '<p><button type="submit" style="padding:8px 18px">ログイン</button></p>'
        '</form><details style="margin-top:18px"><summary>パスワードを忘れた場合</summary>'
        '<p style="font-size:16px;line-height:1.7">メールでの自動再設定は行いません。'
        'この端末であいのてを終了し、管理者が認証ファイルを保全したうえでアカウントを再設定します。'
        '業務データは消さず、管理責任者または導入担当者へ連絡してください。</p>'
        '</details></div></body></html>'
    )


def make_handler(data_dir: Path):
    class RiHubHandler(BaseHTTPRequestHandler):
        server_version = "ainote"
        sys_version = ""

        def version_string(self):
            return "ainote"

        def _route(self):
            return urlsplit(self.path).path.rstrip("/") or "/"

        def _session_viewer(self):
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                morsel = SimpleCookie(raw).get(SESSION_COOKIE)
            except Exception:
                return None
            return get_session(morsel.value) if morsel else None

        def _resolve_viewer(self):
            """セッション viewer。無ければ: 認証必須なら None(要ログイン)、devなら代表自動。"""
            v = self._session_viewer()
            if v is not None:
                return v
            if auth_required(data_dir):
                return None
            return Viewer("dev", "代表", is_dev=True)

        @staticmethod
        def _url_origin(value, *, serialized_origin=False):
            """URLを比較可能な (scheme, host, port) にする。不正値は None。"""
            try:
                parsed = urlsplit((value or "").strip())
                if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
                    return None
                if parsed.username is not None or parsed.password is not None:
                    return None
                if serialized_origin and (parsed.path not in ("", "/")
                                          or parsed.query or parsed.fragment):
                    return None
                port = parsed.port
            except ValueError:
                return None
            scheme = parsed.scheme.lower()
            if port is None:
                port = 443 if scheme == "https" else 80
            return scheme, parsed.hostname.lower(), port

        def _request_origin(self):
            """Host と実サーバのHTTP schemeから、この要求自身のoriginを得る。"""
            host = (self.headers.get("Host") or "").strip()
            if not host:
                return None
            origin = self._url_origin("http://" + host, serialized_origin=True)
            if origin is None or origin[1] not in ("127.0.0.1", "localhost", "::1"):
                return None  # DNS rebinding由来のHostを同一originとして扱わない
            return origin

        def _host_is_allowed(self):
            """DNS rebindingを防ぎ、実際に待受中のloopback originだけを受ける。"""
            values = self.headers.get_all("Host") or []
            if len(values) != 1:
                return False
            raw = values[0].strip()
            if not raw or any(ch.isspace() for ch in raw):
                return False
            origin = self._url_origin("http://" + raw, serialized_origin=True)
            if origin is None or origin[1] not in ("127.0.0.1", "localhost", "::1"):
                return False
            try:
                actual_port = int(self.server.server_address[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                return False
            return origin[2] == actual_port

        def _reject_bad_host(self):
            if self._host_is_allowed():
                return False
            self._send_html(403, "<h1>この接続は受け付けられません</h1>")
            return True

        def parse_request(self):
            """未実装methodも含め、dispatchより前にHostを一律検証する。"""
            if not super().parse_request():
                return False
            if not self._host_is_allowed():
                self._send_html(403, "<h1>この接続は受け付けられません</h1>")
                return False
            return True

        def end_headers(self):
            """HTML・JSON・redirect・downloadを含む全応答へ公開境界ヘッダーを付ける。"""
            preview = self._route() == "/doc/preview"
            frame_policy = "frame-ancestors 'self'" if preview else "frame-ancestors 'none'"
            if preview:
                frame_policy = (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                    "font-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'; "
                    + frame_policy
                )
            self.send_header("Content-Security-Policy", frame_policy)
            self.send_header("X-Frame-Options", "SAMEORIGIN" if preview else "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def _unsafe_post_is_same_origin(self, route):
            """ブラウザ由来のPOSTをscheme/host/portの完全一致で検証する。

            Originを送らない同一origin formはReferer/Fetch Metadataで判定する。
            それらも無いCLIと署名webhookは従来互換を保ち、各経路の認証・署名へ渡す。
            """
            origin_header = (self.headers.get("Origin") or "").strip()
            referer = (self.headers.get("Referer") or "").strip()
            fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if not origin_header and not referer and not fetch_site:
                return True  # 非ブラウザ互換。webhookは後段で署名を必ず検証する。

            expected = self._request_origin()
            if expected is None:
                return False
            if origin_header:
                supplied = self._url_origin(origin_header, serialized_origin=True)
                if supplied != expected:
                    return False
            if referer:
                if self._url_origin(referer) != expected:
                    return False
            if fetch_site and fetch_site != "same-origin":
                return False
            # Originなしの場合は、ブラウザが制御するRefererかFetch Metadataの
            # 少なくとも一方が上の検査を通った時だけブラウザ要求として受ける。
            return bool(origin_header or referer or fetch_site == "same-origin")

        def _sensitive_post_is_same_origin(self):
            """秘密情報の書き出しは、CLI互換のOrigin無しPOSTを許可しない。"""
            has_browser_proof = any((self.headers.get("Origin"), self.headers.get("Referer"),
                                     self.headers.get("Sec-Fetch-Site")))
            return bool(has_browser_proof and self._unsafe_post_is_same_origin(self._route()))

        def _serve_public_portal(self):
            """顧客用magic linkを社内ログインより先に署名検証して返す。"""
            if self._route() != "/portal":
                return False
            import datetime as _dt
            from hub_core import portal as _portal
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            token = (params.get("token", [""])[0] or "").strip()
            try:
                _portal.verify_token(token, today=_dt.date.today().isoformat())
                status = 200
            except _portal.PortalError as exc:
                status = exc.code if exc.code in (400, 401, 403) else 401
            _REQUEST.viewer = None
            self._send_html(status, render_customer_portal(data_dir, params), allow_form=True)
            return True

        def _send_svg(self, body):
            """静的SVG（favicon）を返す。スクリプトを持たない図形のみで、認証前に返してよい。"""
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, status, body, allow_form=False, connect_self=False, frame_self=False, img_self=False):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # CSP: 外部 default-src 'none'。form-action は操作画面のみ 'self'、
            # connect-src は会話コンソール(/console の fetch /chat)のみ 'self'(他は 'none')。
            # img-src は受領写真を表示する画面(/timeline等)のみ 'self'。
            form_action = "'self'" if allow_form else "'none'"
            connect = "'self'" if connect_self else "'none'"
            frame_src = "'self'" if frame_self else "'none'"
            img = "'self'" if img_self else "'none'"
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"script-src 'unsafe-inline'; img-src {img}; font-src 'none'; "
                f"connect-src {connect}; frame-src {frame_src}; form-action {form_action}; base-uri 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _send_json(self, status, obj):
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _redirect(self, location, cookie=None):
            self.send_response(303)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_body(self):
            # 上限つき・非UTF-8でもクラッシュしない（errors=replace）。巨大Content-LengthでのメモリDoSを防ぐ。
            n = min(int(self.headers.get("Content-Length") or 0), 32 * 1024 * 1024)
            return self.rfile.read(n).decode("utf-8", "replace") if n > 0 else ""

        def _reject_blank_document_form_before_auth(self, route: str) -> bool:
            """空の書類POSTは既定IDへ保存する前に拒否する。非空の未認証POSTは通常の認証ゲートへ渡す。"""
            content_type = (self.headers.get("Content-Type") or "").lower()
            if route not in ("/juusetsu/new/create", "/maisoku/new-create"):
                return False
            if "application/x-www-form-urlencoded" not in content_type:
                return False
            form = parse_qs(self._read_body(), keep_blank_values=True)
            property_name = (form.get("property_name", [""])[0] or "").strip()
            if property_name:
                return False
            if route == "/juusetsu/new/create":
                self._send_html(
                    400,
                    "<h1>物件名を入力してください</h1>"
                    "<p>空の重要事項説明書は作成しません。物件名を確認してから、もう一度作成してください。</p>"
                    "<p><a href='/juusetsu/new'>重要事項説明書の作成へ戻る</a></p>",
                )
            else:
                self._send_html(
                    400,
                    "<h1>物件名を入力してください</h1>"
                    "<p>空のマイソクは作成しません。物件名を確認してから、もう一度作成してください。</p>"
                    "<p><a href='/maisoku/new-form'>マイソクの作成へ戻る</a></p>",
                )
            return True

        def _parse_multipart(self):
            """multipart/form-data を解析し (fields:dict[str,str], files:dict[str,(filename,bytes)]) を返す。
            バイナリ安全(先頭/末尾CRLFのみ厳密に剥がす・content内の改行は保持)。"""
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return {}, {}
            m = re.search(r'boundary=([^;]+)', ctype)
            if not m:
                return {}, {}
            boundary = m.group(1).strip().strip('"')
            n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024 * 1024)   # 上限つき(DoS防止)
            body = self.rfile.read(n) if n > 0 else b""
            delim = b"--" + boundary.encode()
            fields, files = {}, {}
            for seg in body.split(delim):
                if seg.startswith(b"\r\n"):
                    seg = seg[2:]
                if seg.endswith(b"\r\n"):
                    seg = seg[:-2]
                if not seg or seg == b"--" or b"\r\n\r\n" not in seg:
                    continue
                head, content = seg.split(b"\r\n\r\n", 1)
                hs = head.decode("utf-8", "replace")
                nm = re.search(r'name="([^"]*)"', hs)
                if not nm:
                    continue
                fn = re.search(r'filename="([^"]*)"', hs)
                if fn is not None:
                    if fn.group(1):
                        files[nm.group(1)] = (fn.group(1), content)
                else:
                    fields[nm.group(1)] = content.decode("utf-8", "replace")
            return fields, files

        def _logout(self):
            raw = self.headers.get("Cookie")
            if raw:
                try:
                    morsel = SimpleCookie(raw).get(SESSION_COOKIE)
                    if morsel:
                        destroy_session(morsel.value)
                except Exception:
                    self._send_html(
                        503,
                        "<h1>依頼を受け付けられませんでした</h1>"
                        "<p>受付記録を安全に保存できなかったため、送信を完了していません。"
                        "時間をおいてもう一度お試しください。</p>",
                        allow_form=True,
                    )
                    return
            self._redirect("/login",
                           cookie=f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")

        def _serve_document_file(self, viewer, *, strict_four_kind: bool = False):
            """Serve legacy or strict case-bound Office output without route overlap."""
            from hub_core import documents
            from hub_core.access import (
                case_bound_document_metadata,
                document_access_allowed,
            )

            qs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            try:
                if strict_four_kind:
                    strict_keys = {"case", "customer", "doc", "v", "as"}
                    if set(qs) != strict_keys:
                        raise documents.DocError(404, "書類が見つかりません。")

                    def required_value(key: str) -> str:
                        values = qs.get(key, [])
                        if len(values) != 1:
                            raise documents.DocError(404, "書類が見つかりません。")
                        raw_value = str(values[0] or "")
                        value = raw_value.strip()
                        # The strict tuple is an identifier, not free text.
                        # Do not silently canonicalize surrounding whitespace.
                        if not value or value != raw_value:
                            raise documents.DocError(404, "書類が見つかりません。")
                        return value

                    # No defaulting is permitted on the case-workspace trust
                    # boundary.  In particular, an omitted v must never select
                    # the latest version and an omitted as must never become PDF.
                    doc_id = required_value("doc")
                    requested_case = required_value("case")
                    requested_customer = required_value("customer")
                    raw_version = required_value("v")
                    raw_format = required_value("as")
                    if re.fullmatch(r"[1-9]\d*", raw_version) is None:
                        raise documents.DocError(404, "書類が見つかりません。")
                    version = int(raw_version)
                    as_fmt = raw_format.lower()
                    # Content-changing legacy controls are intentionally absent
                    # from the exact five-part case-bound route.
                    prs_addr = ""
                    publish = False
                else:
                    # case/customer-bearing requests belong exclusively to
                    # /case/doc/file.  Their presence, even blank, cannot turn
                    # this compatibility endpoint into a weaker strict route.
                    if "case" in qs or "customer" in qs:
                        raise documents.DocError(404, "書類が見つかりません。")
                    doc_id = (qs.get("doc", [""])[0] or "").strip()
                    as_fmt = (qs.get("as", ["pdf"])[0] or "pdf").strip().lower()
                    raw_version = (qs.get("v", [""])[0] or "").strip()
                    version = int(raw_version) if raw_version else None
                    if version is not None and version < 1:
                        raise ValueError("invalid version")
                    requested_case = ""
                    requested_customer = ""
                    prs_addr = (qs.get("addr", [""])[0] or "").strip()
                    publish = (qs.get("publish", [""])[0] or "").strip() == "1"
                with documents.document_transaction(data_dir, doc_id):
                    if strict_four_kind:
                        authorized = case_bound_document_metadata(
                            data_dir, viewer,
                            case_id=requested_case,
                            customer_id=requested_customer,
                            doc_id=doc_id,
                            version=version,
                            requested_format=as_fmt,
                            require_four_kind=True,
                        )
                        if authorized is None:
                            raise documents.DocError(404, "書類が見つかりません。")
                        # Use exactly the already-authorized canonical values.
                        doc_id = str(authorized["doc_id"])
                        version = int(authorized["version"])
                        as_fmt = str(authorized["output_format"])
                    else:
                        try:
                            legacy_meta = documents.get_version_metadata(
                                data_dir, doc_id, version)
                        except Exception as exc:
                            raise documents.DocError(
                                404, "書類が見つかりません。") from exc
                        # Four-kind output has one route only.  This closes the
                        # no-case/no-customer fallback for owner and privileged
                        # viewers before any body is opened.
                        if documents.canonical_four_document_kind(
                                legacy_meta.get("kind") or "") is not None:
                            raise documents.DocError(404, "書類が見つかりません。")
                        if not document_access_allowed(data_dir, viewer, doc_id, version):
                            raise documents.DocError(404, "書類が見つかりません。")
                    data, fname, ctype = _generate_doc_file(
                        data_dir, doc_id, as_fmt, prs_address=prs_addr,
                        version=version, publish=publish)
            except Exception as exc:
                status = int(getattr(exc, "code", 400))
                if status not in (400, 403, 404, 409):
                    status = 400
                # 上の認可ゲートは越境・不在・版不一致をすべて404として発生させる。
                # 生成後の「確定記録なし」等の業務上の403まで不在へ偽装しない。
                if status == 404:
                    self._send_html(404, "<h1>404</h1><p>書類が見つかりません。</p>")
                else:
                    self._send_html(status, "<h1>出力できません</h1><p>" + _esc(
                        _public_failure("書類を出力できませんでした。入力内容と出力形式を確認してください。"))
                                    + "</p><p><a href='/maisoku'>← マイソク</a> / "
                                    "<a href='/juusetsu'>重説</a></p>")
                return
            try:
                audit_route = "/case/doc/file" if strict_four_kind else "/doc/file"
                record_view(data_dir, viewer.user, viewer.role, audit_route, action="doc_export")
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(fname))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self._reject_bad_host():
                return
            route = self._route()
            if route == "/favicon.svg":
                # ブランドのタブアイコンは認証・初期設定より前に返す（機密を含まない静的図形）。
                self._send_svg(FAVICON_SVG)
                return
            if self._serve_public_portal():
                return
            if route == "/setup":
                if is_configured(data_dir):
                    self._redirect("/")
                    return
                _REQUEST.viewer = None
                self._send_html(200, render_setup(data_dir), allow_form=True)
                return
            if os.environ.get("RI_HUB_ONBOARD") == "1" and not is_configured(data_dir):
                self._redirect("/setup")
                return
            if route == "/login":
                _REQUEST.viewer = None
                self._send_html(200, render_login(), allow_form=True)
                return
            if route == "/logout":
                self._logout()
                return
            viewer = self._resolve_viewer()
            if viewer is None:  # 認証必須かつ未ログイン
                self._redirect("/login")
                return
            _REQUEST.viewer = viewer
            case_query_keys = {
                "/case": "id",
                "/timeline": "id",
                "/juusetsu": "case",
                "/juusetsu/new": "case",
                "/maisoku/new-form": "case",
                "/property/collect": "case",
            }
            case_key = case_query_keys.get(route)
            if case_key:
                requested_case = (
                    parse_qs(urlsplit(self.path).query).get(case_key, [""])[0] or ""
                ).strip()
                if requested_case:
                    from hub_core.access import case_access_allowed
                    if not case_access_allowed(data_dir, viewer, requested_case):
                        self._send_html(404, "<h1>404</h1><p>案件が見つかりません。</p>")
                        return
            if route == "/doc/preview":
                query = parse_qs(urlsplit(self.path).query)
                doc_id = (query.get("doc", [""])[0] or "").strip()
                try:
                    raw_version = (query.get("v", [""])[0] or "").strip()
                    version = int(raw_version) if raw_version else None
                    from hub_core.access import document_access_allowed
                    if not document_access_allowed(data_dir, viewer, doc_id, version):
                        self._send_html(404, "<h1>404</h1><p>書類が見つかりません。</p>")
                        return
                    payload = render_doc_preview(data_dir, query).encode("utf-8")
                except Exception as exc:
                    from hub_core import maisoku as _ms
                    missing = getattr(exc, "missing", [])
                    if missing:
                        detail = "、".join(_ms.MAISOKU_LABELS.get(key, key) for key in missing)
                    else:
                        detail = str(exc)
                    self._send_html(
                        int(getattr(exc, "code", 409)),
                        "<h1>公開用出力を作成できません</h1>"
                        "<p>必要事項、広告表現、または写真の利用権を確認してください。</p>"
                        f"<p>{_esc(detail)}</p><p><a href='/maisoku'>マイソクへ戻る</a></p>",
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                # 整形済み書類(自前テンプレHTML等)を見せるため style/img を許可(scriptは禁止のまま)
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                                 "font-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
            if route == "/portal/export":
                # ポータル掲載書式のCSVを生成してダウンロード（G5・自動出稿はしない・手動アップロード用）。
                qs = parse_qs(urlsplit(self.path).query)
                portal = (qs.get("portal", ["suumo"])[0] or "suumo").strip()
                from hub_core import portal_export as _pe
                if portal not in _pe.portals():
                    self._send_html(400, "<h1>未知のポータル書式</h1>")
                    return
                try:
                    rows = _portal_export_rows(data_dir)
                    payload = _pe.export_bytes(rows, portal)
                except Exception:
                    self._send_html(400, "<h1>出力できません</h1><p>" + _esc(
                        _public_failure("入力データを確認できないため、出力を作成できませんでした。")) + "</p>")
                    return
                fname = f"portal_{portal}.csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=shift_jis")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(fname))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
            if route == "/api/backup":
                self._send_html(405, "<h1>この操作方法は使えません</h1>"
                                     "<p>接続設定の保存ボタンから実行してください。</p>")
                return
            if route == "/case/doc/file":
                self._serve_document_file(viewer, strict_four_kind=True)
                return
            if route == "/doc/file":
                self._serve_document_file(viewer, strict_four_kind=False)
                return
            if route == "/file/raw":
                # 受領ファイル(写真/収入証明等)をローカルフォルダから配信。写真はinline表示・書類はDL。
                from hub_core import files as _files
                qs = parse_qs(urlsplit(self.path).query)
                scope = (qs.get("scope", [""])[0] or "").strip()
                eid = (qs.get("id", [""])[0] or "").strip()
                name = (qs.get("name", [""])[0] or "").strip()
                if not _file_access_allowed(data_dir, viewer, scope, eid, name):
                    self._send_html(404, "<h1>404</h1><p>ファイルが見つかりません。</p>")
                    return
                try:
                    raw = _files.read_file(data_dir, scope, eid, name)
                except Exception:
                    raw = None
                if raw is None:
                    self._send_html(404, "<h1>404</h1><p>ファイルが見つかりません。</p>")
                    return
                ext = Path(name).suffix.lower()
                img_ct = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                          ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                          ".heic": "image/heic", ".heif": "image/heif"}
                inline = ext in img_ct
                ctype = img_ct.get(ext) or _DOC_CONTENT_TYPES.get(ext, "application/octet-stream")
                try:
                    record_view(data_dir, viewer.user, viewer.role, "/file/raw", action="file_view")
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                disp = "inline" if inline else "attachment"
                self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''" + quote(name))
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)
                return
            if route == "/cal/ics":
                # 案件の予定を .ics で書き出し→Googleカレンダー等で開ける(OAuth不要)。
                qs = parse_qs(urlsplit(self.path).query)
                cid = (qs.get("case", [""])[0] or "").strip()
                from hub_core.access import case_access_allowed
                if not case_access_allowed(data_dir, viewer, cid):
                    self._send_html(404, "<h1>404</h1><p>予定が見つかりません。</p>")
                    return
                ics = _ics_for_case(data_dir, cid)
                if ics is None:
                    self._send_html(404, "<h1>予定なし</h1><p>カレンダーに書き出せる予定（内見・契約日等）がありません。</p>")
                    return
                try:
                    record_view(data_dir, viewer.user, viewer.role, "/cal/ics", action="cal_export")
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(cid + ".ics"))
                self.send_header("Content-Length", str(len(ics)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(ics)
                return
            if route == "/go":
                params = parse_qs(urlsplit(self.path).query)
                dest = resolve_acquisition_url(data_dir, params)
                if dest:
                    try:
                        record_view(data_dir, viewer.user, viewer.role, "/go", action="external_open")
                    except Exception:
                        pass
                    self._redirect(dest)  # 公的取得先(allowlist検証済)へ302。本文HTMLには内部/goリンクのみ
                else:
                    self._send_html(403, "<h1>403</h1><p>許可された公式取得先ではありません。</p>")
                return
            if route == "/map":
                mp = parse_qs(urlsplit(self.path).query)
                mq = (mp.get("q", [""])[0] or "").strip()
                if mq:
                    try:
                        record_view(data_dir, viewer.user, viewer.role, "/map", action="map_open")
                    except Exception:
                        pass
                    self._redirect("https://www.google.com/maps/search/?api=1&query=" + quote(mq))
                else:
                    self._send_html(400, "<h1>400</h1><p>地図クエリがありません。</p>")
                return
            # S0-7: エクスポート禁止(集約画面含む全画面)。試行は audit(action='export')に記録し403で拒否。
            if is_export_request(self.path):
                try:
                    record_view(data_dir, viewer.user, viewer.role, route, action="export")
                except Exception:
                    pass
                self._send_html(403, "<h1>403 エクスポートは許可されていません</h1>"
                                "<p>本システムは閲覧専用です。データのエクスポート/ダウンロードは"
                                "禁止されています(S0-7・§4.2)。バックアップは暗号化経路のみ。</p>")
                return
            try:
                status, body = build_response(data_dir, self.path)
            except Exception as exc:  # 未捕捉例外でコネクション切断させない=graceful 500(R3#6b多層防御)
                self._send_html(500, "<h1>500</h1><p>ページの生成でエラーが発生しました。"
                                "入力値を確認してください。</p><p><a href='/home'>← ホーム</a></p>")
                return
            if status == 200:  # S0-5: 閲覧監査(実ユーザーで誰が何を見たか)
                try:
                    record_view(data_dir, viewer.user, viewer.role, route)
                except Exception:
                    pass  # 監査記録失敗で閲覧を妨げない(可用性)。記録は best-effort
            self._send_html(status, body, allow_form=(route in _FORM_ROUTES),
                            connect_self=(route in _CONNECT_ROUTES),
                            frame_self=(route in _FRAME_ROUTES),
                            img_self=(route in _IMG_ROUTES))

        def do_HEAD(self):  # noqa: N802
            if self._reject_bad_host():
                return
            if self._serve_public_portal():
                return
            viewer = self._resolve_viewer()
            if viewer is None and self._route() != "/login":
                self._redirect("/login")
                return
            _REQUEST.viewer = viewer
            case_query_keys = {
                "/case": "id", "/timeline": "id", "/juusetsu": "case",
                "/juusetsu/new": "case", "/maisoku/new-form": "case",
                "/property/collect": "case",
            }
            case_key = case_query_keys.get(self._route())
            if viewer is not None and case_key:
                requested_case = (
                    parse_qs(urlsplit(self.path).query).get(case_key, [""])[0] or ""
                ).strip()
                if requested_case:
                    from hub_core.access import case_access_allowed
                    if not case_access_allowed(data_dir, viewer, requested_case):
                        self._send_html(404, "")
                        return
            if is_export_request(self.path):
                self._send_html(403, "")
                return
            status, body = build_response(data_dir, self.path)
            self._send_html(status, body, allow_form=(self._route() in _FORM_ROUTES))

        def do_POST(self):  # noqa: N802
            if self._reject_bad_host():
                return
            from hub_core.backup import portable_snapshot_lock
            route = self._route()
            if not _post_route_allowed(route):
                self._send_html(
                    501,
                    "<h1>この操作は利用できません</h1>"
                    "<p>画面の操作ボタンからやり直してください。</p>",
                )
                return
            # Originと認証を排他lock・body読取・業務分岐より前に一度だけ強制する。
            if not self._unsafe_post_is_same_origin(route):
                self._send_html(403, "<h1>この操作は受け付けられません</h1>"
                                     "<p>別のサイトから送られてきた指示のため、"
                                     "安全のために実行しませんでした。</p>")
                return
            if route in ("/api/backup", "/api/backup/recovery-key") \
                    and not self._sensitive_post_is_same_origin():
                self._send_html(403, "<h1>この操作は受け付けられません</h1>"
                                     "<p>接続設定の画面からやり直してください。</p>")
                return
            if route not in _PUBLIC_POST_ROUTES:
                viewer = self._resolve_viewer()
                if viewer is None:
                    content_type = (self.headers.get("Content-Type") or "").lower()
                    if self._reject_blank_document_form_before_auth(route):
                        return
                    if "application/json" in content_type:
                        self._send_json(401, {"status": "error", "error": "ログインが必要です。"})
                    else:
                        self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
            else:
                _REQUEST.viewer = None
            # SSEは外部AI待ちの全時間を排他しない。発話保存だけroute内で短くlockする。
            if route == "/chat/stream":
                return self._do_POST()
            with portable_snapshot_lock(data_dir):
                return self._do_POST()

        def _do_POST(self):
            route = self._route()
            if route == "/api/backup":
                # 復旧キーとは別取得し、監査鍵は暗号化された本体の内側だけに保持する。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_html(403, "<h1>権限がありません</h1><p>バックアップは責任者/代表のみです。</p>")
                    return
                try:
                    from hub_core import backup as _backup
                    if not _backup.portable_crypto_available():
                        self._send_html(503, "<h1>暗号化バックアップは利用できません</h1>"
                                             "<p>この端末では標準暗号AES-256-GCMを利用できません。</p>")
                        return
                    payload = _backup.make_portable_backup(data_dir, actor=viewer.user)
                except Exception:  # noqa: BLE001 鍵や絶対パスをHTTP応答へ漏らさない
                    self._send_html(500, "<h1>バックアップを作成できません</h1>"
                                    "<p>データと鍵の状態を管理責任者が確認してください。</p>")
                    return
                fname = "ainote-backup.enc"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(fname))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
                return
            if route == "/api/backup/recovery-key":
                # バックアップ本体とは別の、明示操作でだけ取得できる復旧キー。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_html(403, "<h1>権限がありません</h1><p>復旧キーは責任者/代表のみ保存できます。</p>")
                    return
                try:
                    from hub_core import backup as _backup
                    if not _backup.portable_crypto_available():
                        self._send_html(503, "<h1>復旧キーは取得できません</h1>"
                                             "<p>標準暗号を利用できないため、バックアップ機能は停止中です。</p>")
                        return
                    payload = _backup.export_recovery_key()
                    record_view(data_dir, viewer.user, viewer.role,
                                "/api/backup/recovery-key", action="export")
                except Exception:  # noqa: BLE001 鍵や絶対パスをHTTP応答へ漏らさない
                    self._send_html(500, "<h1>復旧キーを保存できません</h1>"
                                    "<p>鍵の状態を管理責任者が確認してください。</p>")
                    return
                fname = "ainote-recovery-key.txt"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=us-ascii")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(fname))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
            if route == "/juusetsu/new/parse":
                # 貼り付けテキストから既知フィールドを抽出（フォーム自動入力の補助）。
                from hub_core import juusetsu_draft as _jd
                try:
                    body = json.loads(self._read_body() or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                self._send_json(200, {"fields": _jd.parse_pasted(str(body.get("text") or ""))})
                return
            if route == "/juusetsu/new/create":
                # フォーム入力→35条の下書き(md)を生成→保存→プレビュー。「マイソクも作成」ONなら同じ情報で両方。
                import datetime as _dt2
                import json as _json
                from hub_core import documents      # do_POST内で後段がローカルimportするため明示（shadowing回避）
                from hub_core import juusetsu_draft as _jd
                from hub_core.chat_bridge import _safe_juusetsu_id
                form = parse_qs(self._read_body(), keep_blank_values=True)
                fields = {k: (form.get(k, [""])[0] or "").strip() for k in _jd.FIELD_KEYS}
                if not fields.get("property_name"):
                    self._send_html(
                        400,
                        "<h1>物件名を入力してください</h1>"
                        "<p>空の重要事項説明書は作成しません。物件名を確認してから、もう一度作成してください。</p>"
                        "<p><a href='/juusetsu/new'>重要事項説明書の作成へ戻る</a></p>",
                    )
                    return
                property_kind = (form.get("property_kind", [""])[0] or "").strip()  # 会話ファーストの1問で確定した物件種別
                from hub_core.auth import load_company as _lc
                fields = _jd.fill_from_company(fields, _lc(data_dir, strict=True))  # 業者/宅建士を profile から必ず補完(法定)
                name = fields.get("property_name") or "重要事項説明書"
                base_id = _safe_juusetsu_id(name)[3:] or "doc"   # "JU-"を除いた安全名
                md = _jd.render_juusetsu_md(fields, property_kind=property_kind, today=_dt2.date.today().isoformat())
                from hub_core import prs_juusetsu as _prs_j
                md = _prs_j.apply_to_draft(
                    md, _prs_j.fetch_for_draft(data_dir, fields.get("address") or ""))
                case_id = (form.get("case", [""])[0] or "").strip()
                viewer = self._resolve_viewer()
                from hub_core.access import authorized_case_binding
                binding = authorized_case_binding(data_dir, viewer, case_id)
                if binding is None:
                    self._send_html(
                        403,
                        "<h1>この案件の書類は作成できません</h1>"
                        "<p>担当案件を選んでから作成してください。</p>",
                    )
                    return
                customer_id = str(binding.get("customer_id") or "").strip()
                if not customer_id:
                    self._send_html(403, "<h1>案件に顧客が紐付いていません</h1>")
                    return
                res = documents.save_version(
                    data_dir, "JU-" + base_id, md, kind="juusetsu", fmt="md",
                    author=viewer.user, case_id=case_id, customer_id=customer_id)
                also = (form.get("also_maisoku", ["1"])[0] or "").strip() not in ("", "0", "off")
                if also:
                    from hub_core import maisoku as _ms
                    from hub_core.auth import load_company
                    ms = _ms.fields_with_company(_ms.from_property_fields(fields),
                                                 load_company(data_dir, strict=True))
                    ms["_variant"] = "survey"
                    ms["_xlsx_variant"] = "A"
                    ms["property"] = fields.get("property_name") or ""
                    documents.save_version(data_dir, "MS-" + base_id, _json.dumps(ms, ensure_ascii=False),
                                           kind="maisoku", fmt="txt", author=viewer.user,
                                           case_id=case_id, customer_id=customer_id)
                self._redirect("/doc/preview?doc=" + quote(res["doc_id"])
                               + ("&also=MS-" + quote(base_id) if also else ""))
                return
            if route == "/maisoku/new-create":
                # マイソク単独の新規作成: 要点フィールド→帯自動fill→survey様式で保存→プレビュー。
                import json as _json
                from hub_core import documents, maisoku as _ms
                from hub_core.auth import load_company as _lc
                from hub_core.chat_bridge import _safe_juusetsu_id
                form = parse_qs(self._read_body(), keep_blank_values=True)
                fields = {k: (form.get(k, [""])[0] or "").strip() for k, _l, _g in MAISOKU_NEW_FIELDS}
                if not fields.get("property_name"):
                    self._send_html(
                        400,
                        "<h1>物件名を入力してください</h1>"
                        "<p>空のマイソクは作成しません。物件名を確認してから、もう一度作成してください。</p>"
                        "<p><a href='/maisoku/new-form'>マイソクの作成へ戻る</a></p>",
                    )
                    return
                fields = _ms.fields_with_company(fields, _lc(data_dir, strict=True))
                fields["_variant"] = "survey"
                fields["_xlsx_variant"] = "A"
                fields["property"] = fields.get("property_name") or ""
                name = fields.get("property_name") or "マイソク"
                mid = "MS-" + (_safe_juusetsu_id(name)[3:] or "doc")
                case_id = (form.get("case", [""])[0] or "").strip()
                viewer = self._resolve_viewer()
                from hub_core.access import authorized_case_binding
                binding = authorized_case_binding(data_dir, viewer, case_id)
                if binding is None:
                    self._send_html(
                        403,
                        "<h1>この案件の書類は作成できません</h1>"
                        "<p>担当案件を選んでから作成してください。</p>",
                    )
                    return
                customer_id = str(binding.get("customer_id") or "").strip()
                if not customer_id:
                    self._send_html(403, "<h1>案件に顧客が紐付いていません</h1>")
                    return
                res = documents.save_version(data_dir, mid, _json.dumps(fields, ensure_ascii=False),
                                             kind="maisoku", fmt="txt", author=viewer.user,
                                             case_id=case_id, customer_id=customer_id)
                self._redirect("/doc/preview?doc=" + quote(res["doc_id"]))
                return
            if route == "/api/conn-test":
                # 接続テスト（利用者起動・自動送信しない）。責任者/代表のみ（SSRF/資格情報悪用防止）。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_json(403, {"ok": False, "detail": "権限がありません（責任者/代表のみ）"})
                    return
                from hub_core import connections
                try:
                    body = json.loads(self._read_body() or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                kind = str(body.get("kind") or "")
                test_params = _merged_connection_params(data_dir, kind, body.get("params") or {})
                res = connections.run_test(kind, test_params, data_dir=data_dir)
                if kind == "harness":
                    try:
                        from hub_core.audit import append_events
                        append_events(Path(data_dir) / "audit_log.jsonl", [{
                            "actor": viewer.user,
                            "action": "connection_tested",
                            "target": "line",
                            "gate_status": "connected" if res.get("ok") else "failed",
                            "source_ref": "serve/connections/test",
                            "timestamp": now_jst_iso(),
                        }])
                    except Exception:
                        self._send_json(503, {
                            "ok": False,
                            "detail": "接続結果を監査記録へ安全に保存できませんでした。",
                        })
                        return
                self._send_json(200, {
                    "ok": bool(res.get("ok")),
                    "detail": ("接続できました。" if res.get("ok") else
                               "接続できませんでした。設定内容とサービスの稼働状況を確認してください。"),
                })
                return
            if route == "/connections/save-fax":
                # FAX送信サービスの保存（責任者/代表のみ）。トークンは keysファイル(0600)へ。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_json(403, {"error": "権限がありません（責任者/代表のみ）"})
                    return
                from hub_core import connections
                try:
                    body = json.loads(self._read_body() or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                merged = _merged_connection_params(data_dir, "fax", body)
                endpoint = str(merged.get("endpoint") or "").strip()
                if endpoint and not endpoint.startswith("https://"):
                    self._send_json(400, {"error": "送信先URLは https で始まる必要があります"})
                    return
                cfg = {"service_name": str(merged.get("service_name") or "").strip(),
                       "endpoint": endpoint,
                       "method": "POST",
                       "auth_style": str(merged.get("auth_style") or "bearer").strip(),
                       "from_number": str(merged.get("from_number") or "").strip()}
                try:
                    connections.save_fax_config(data_dir, cfg, str(body.get("token") or ""))
                except OSError:
                    self._send_json(500, {"error": _public_failure("FAX接続設定を保存できませんでした。")})
                    return
                self._send_json(200, {"ok": True})
                return
            if route == "/connections/save":
                # SMTP設定の保存（責任者/代表のみ）。非秘密=config・パスワード=keysファイル(0600)。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_json(403, {"error": "権限がありません（責任者/代表のみ）"})
                    return
                from hub_core import connections
                try:
                    body = json.loads(self._read_body() or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                merged = _merged_connection_params(data_dir, "smtp", body)
                cfg = {"host": str(merged.get("host") or "").strip(),
                       "port": str(merged.get("port") or "587").strip(),
                       "user": str(merged.get("user") or "").strip(),
                       "tls": "1" if str(merged.get("tls", "1")) not in ("0", "false") else "0"}
                pw = str(body.get("password") or "")
                connections.save_smtp_config(data_dir, cfg, pw)
                self._send_json(200, {"ok": True})
                return
            if route in ("/migrate/preview", "/migrate/apply"):
                # 名簿の取り込み。責任者・代表のみ。**下見と実行を分ける**。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_html(403, "<h1>権限がありません</h1>"
                                         "<p>名簿の取り込みは責任者・代表のみです。</p>")
                    return
                _REQUEST.viewer = viewer
                from hub_core import migrate as _mig
                if route == "/migrate/preview":
                    fields, files = self._parse_multipart()
                    got = files.get("file")
                    if not got or not got[1]:
                        self._redirect("/migrate?msg=" + quote("ファイルを選んでください。"))
                        return
                    fname, raw = got[0], got[1]
                    tool = (fields.get("source_tool") or "").strip()
                    try:
                        plan = _mig.plan(raw, fname)
                    except _mig.MigrateError as exc:
                        self._redirect("/migrate?msg=" + quote(_public_exception_message(
                            exc, "取り込み処理を完了できませんでした。")))
                        return
                    token = _stash_migration(raw, fname)
                    plan["_token"] = token
                    self._send_html(200, render_migrate_preview(
                        data_dir, plan, filename=fname, source_tool=tool), allow_form=True)
                    return
                form = parse_qs(self._read_body(), keep_blank_values=True)
                token = (form.get("token", [""])[0] or "").strip()
                tool = (form.get("source_tool", [""])[0] or "").strip()
                stashed = _take_migration(token)
                if not stashed:
                    self._redirect("/migrate?msg=" + quote(
                        "時間が経ったため、もう一度ファイルを選んでください。"))
                    return
                raw, fname = stashed
                # 誰がいつ何件取り込んだかを監査台帳へ（後から「これはどこから来た？」に答える）。
                # CSV/DBと監査を一手にし、監査だけ失敗した成功を作らない。
                def _audit_import(res):
                    from hub_core.audit import append_events
                    append_events(Path(data_dir) / "audit_log.jsonl", [{
                        "actor": getattr(viewer, "user", ""),
                        "action": "customers_imported",
                        "target": fname,
                        "gate_status": "imported",
                        "added": str(res["added"]),
                        "skipped": str(len(res["skipped"])),
                        "source_tool": tool,
                        "timestamp": now_jst_iso(),
                        "source_ref": "serve/migrate/apply",
                    }])

                try:
                    res = _mig.apply(data_dir, raw, fname, source_tool=tool,
                                     audit_commit=_audit_import)
                except _mig.MigrateError as exc:
                    self._send_html(exc.code, _ri_shell(
                        "/migrate", "取り込みを完了できませんでした",
                        '<div class="ri-section"><h1>取り込みを完了できませんでした</h1>'
                        f'<p>{_esc(_public_exception_message(exc, "名簿を取り込めませんでした。ファイルの内容を確認してください。"))}</p>'
                        '<a class="ri-qbtn" href="/migrate">取り込みへ戻る</a></div>'
                    ))
                    return
                self._redirect("/customers?msg=" + quote(
                    f"{res['added']} 名を取り込みました"
                    + (f"（{res['dup_existing']} 名はすでにお取引のある方でした）"
                       if res["dup_existing"] else "")))
                return
            if route == "/brand/restore":
                # 過去の版へ戻す。責任者/代表のみ・戻した操作も履歴に残る。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_html(403, "<h1>権限がありません</h1>"
                                         "<p>会社情報を戻せるのは責任者/代表のみです。</p>")
                    return
                from hub_core import branding as _br
                form = parse_qs(self._read_body(), keep_blank_values=True)
                try:
                    ver = int((form.get("version", ["0"])[0] or "0").strip())
                except ValueError:
                    ver = 0
                try:
                    rec = _br.restore(data_dir, ver, str(getattr(viewer, "user", "") or ""))
                except _br.BrandError as exc:
                    self._send_html(400, render_profile_error(
                        data_dir, _public_exception_message(exc, "指定した会社情報へ戻せませんでした。")),
                        allow_form=True)
                    return
                self._redirect("/brand/history?msg=" + quote(
                    f"版 {ver} の状態に戻しました（この操作は版 {rec['version']} として記録しました）"))
                return
            if route == "/profile/save":
                # 業者プロフィール保存（責任者/代表のみ）。company.json へマージ。
                viewer = self._resolve_viewer()
                if viewer is None or viewer.role not in ("責任者", "代表"):
                    self._send_html(403, "<h1>権限がありません</h1><p>業者情報の編集は責任者/代表のみです。</p>")
                    return
                from hub_core.auth import load_company, save_company
                form = parse_qs(self._read_body(), keep_blank_values=True)
                company = dict(load_company(data_dir))
                for key, _label, _ph, _grp, _typ in PROFILE_FIELDS:
                    company[key] = (form.get(key, [""])[0] or "").strip()
                # ブランド項目は履歴に1版積んでから保存する（restore で戻せる）
                from hub_core import branding as _br
                try:
                    # 保存と履歴は一手。検証に落ちた値は**正本にも入れない**
                    # （握り潰すと「履歴に無いのに正本にはある」不正値が残る）。
                    _br.save(data_dir, company, actor=str(getattr(viewer, "user", "") or ""),
                             source="profile")
                except _br.BrandError as exc:
                    self._send_html(400, render_profile_error(
                        data_dir, _public_exception_message(exc, "会社情報を保存できませんでした。入力内容を確認してください。")),
                        allow_form=True)
                    return
                self._redirect("/profile?saved=1")
                return
            if route in ("/portal/request", "/portal/apply"):
                # 顧客ポータルのセルフ内見希望/セルフ申込（G7/Tier2・トークン認証・内部RBAC非依存）。
                # 自動確定しない＝業者側の依頼として監査に載る（人間が正式受付・確認）。
                import datetime as _dt
                from hub_core import portal as _pt
                is_apply = route == "/portal/apply"
                form = parse_qs(self._read_body(), keep_blank_values=True)
                token = (form.get("token", [""])[0] or "").strip()
                note = (form.get("note", [""])[0] or "").strip()[:500]
                applicant = (form.get("applicant", [""])[0] or "").strip()[:100]
                try:
                    payload = _pt.verify_token(token, today=_dt.date.today().isoformat())
                except _pt.PortalError:
                    self._send_html(401, "<h1>リンクの有効期限が切れています</h1>"
                                    "<p>担当者にお問い合わせください。</p>")
                    return
                try:
                    from hub_core.operations import _audit, _event
                    if is_apply:
                        # セルフ申込＝draft的な申込意思の記録。正式なapplication_createは業者(RBAC)が行う。
                        _audit(data_dir, _event("customer", "portal_apply", payload["case_id"],
                                                "requested", applicant=applicant, note=note, channel="portal"))
                    else:
                        _audit(data_dir, _event("customer", "portal_request", payload["case_id"],
                                                "requested", note=note, channel="portal"))
                except Exception:
                    self._send_html(
                        503,
                        "<h1>送信を完了していません</h1>"
                        "<p>記録を保存できなかったため、担当者へはまだ伝えていません。"
                        "時間をおいてもう一度お試しください。</p>",
                    )
                    return
                _msg = ("お申込を受け付けました" if is_apply else "担当者に伝えました")
                self._send_html(200, f"<div style='max-width:520px;margin:8vh auto;font-family:sans-serif;"
                                f"text-align:center'><h2>{_msg}</h2>"
                                "<p>内容を確認してご連絡します。ありがとうございます。</p></div>",
                                allow_form=True)
                return
            if route == "/setup/step":
                # 段の送り。パスワードを URL に載せないため POST で持ち回る。
                if is_configured(data_dir):
                    self._redirect("/")
                    return
                form = parse_qs(self._read_body(), keep_blank_values=True)
                step = (form.get("step", ["company"])[0] or "company").strip()
                if step not in SETUP_STEPS:
                    step = "company"
                _REQUEST.viewer = None
                self._send_html(200, render_setup(data_dir, form=form, step=step),
                                allow_form=True)
                return
            if route == "/setup":
                if is_configured(data_dir):
                    self._redirect("/")
                    return
                form = parse_qs(self._read_body())
                status, location, cookie = setup_company(data_dir, form)
                if location:
                    self._redirect(location, cookie=cookie)
                else:
                    _REQUEST.viewer = None
                    self._send_html(status, render_setup(
                        data_dir, "会社名・ログインID・パスワード(8文字以上)が要ります",
                        form=form, step="ai"), allow_form=True)
                return
            if route == "/file/upload":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core import files as _files
                fields, files = self._parse_multipart()
                scope = (fields.get("scope") or "").strip()
                eid = (fields.get("id") or "").strip()
                back = _safe_back(fields.get("back"))  # オープンリダイレクト防止
                saved = []
                for _key, (fn, content) in files.items():
                    if not _file_access_allowed(data_dir, viewer, scope, eid, fn):
                        self._send_html(
                            403,
                            "<h1>このファイルを保存できません</h1>"
                            "<p>担当範囲またはファイル種別の権限を確認してください。</p>",
                        )
                        return
                    try:
                        saved.append(_files.save_upload(data_dir, scope, eid, fn, content))
                    except Exception:
                        self._send_html(400, "<h1>アップロードできません</h1><p>" + _esc(
                            _public_failure("ファイルを保存できませんでした。形式と容量を確認してください。"))
                                        + f"</p><p><a href='{_esc(back)}'>← 戻る</a></p>")
                        return
                if saved:
                    try:
                        record_view(data_dir, viewer.user, viewer.role, "/file/upload",
                                    action="file_upload")
                    except Exception:
                        pass
                self._redirect(back)
                return
            if route in ("/maisoku/from-photo", "/juusetsu/from-photo"):
                # 販売図面/登記等の写真/PDF → 無料ローカルOCR＋幾何構造化 → 新規フォームに事前入力。
                is_ju = route == "/juusetsu/from-photo"
                stash_name = ".juusetsu_ocr_prefill.json" if is_ju else ".maisoku_ocr_prefill.json"
                back_form = "/juusetsu/new" if is_ju else "/maisoku/new-form"
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                import os as _os
                import tempfile as _tf
                from hub_core import ocr_structure as _ocst, local_ocr as _loc
                fields_mp, files = self._parse_multipart()
                if not files:
                    self._redirect(back_form)
                    return
                _fn, content = next(iter(files.values()))
                if not content or len(content) > 25 * 1024 * 1024:
                    self._send_html(400, "<h1>読み取れません</h1><p>空、または大きすぎるファイルです（上限25MB）。</p>"
                                    f"<p><a href='{back_form}'>← 戻る</a></p>")
                    return
                # 一時ファイルにOCR（vaultに残さない・読取れた項目のみ・捏造しない）。
                suffix = Path(_fn).suffix.lower() or ".png"
                if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                                  ".heic", ".heif", ".bmp", ".tif", ".tiff"}:
                    suffix = ".png"
                tf = _tf.NamedTemporaryFile(suffix=suffix, delete=False)
                st = {}
                try:
                    tf.write(content); tf.close()
                    if _loc.available():
                        try:
                            st = _ocst.structure_document(tf.name)
                            st.pop("_meta", None)
                        except Exception:
                            st = {}
                finally:
                    try:
                        _os.unlink(tf.name)
                    except OSError:
                        pass
                try:
                    import json as _j
                    (Path(data_dir) / stash_name).write_text(
                        _j.dumps(st, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
                try:
                    record_view(data_dir, viewer.user, viewer.role, route, action="ocr_read")
                except Exception:
                    pass
                self._redirect((back_form + "?from=ocr"))
                return
            if route == "/property/collect":
                # 物件に書類を追加 → OCR構造化 → 既存の合流レコードに合流して保存（完成度が上がる）。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                import os as _os
                import tempfile as _tf
                from hub_core import ocr_structure as _ocst, local_ocr as _loc, property_info as _pi
                cid = (parse_qs(urlsplit(self.path).query).get("case", [""])[0] or "").strip()
                from hub_core.access import case_access_allowed
                if not case_access_allowed(data_dir, viewer, cid):
                    self._send_html(403, "<h1>この案件には追加できません</h1>")
                    return
                _fields_mp, files = self._parse_multipart()
                if not cid or not files:
                    self._redirect("/case?id=" + quote(cid))
                    return
                docs = []
                for _k, (fn, content) in files.items():
                    if not content or len(content) > 25 * 1024 * 1024:
                        continue
                    suffix = Path(fn).suffix.lower() or ".png"
                    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                                      ".heic", ".heif", ".bmp", ".tif", ".tiff"}:
                        suffix = ".png"
                    tf = _tf.NamedTemporaryFile(suffix=suffix, delete=False)
                    try:
                        tf.write(content); tf.close()
                        if _loc.available():
                            f = _ocst.structure_document(tf.name)
                            f.pop("_meta", None)
                            docs.append((fn, {k: v for k, v in f.items() if not str(k).startswith("_")}))
                    finally:
                        try:
                            _os.unlink(tf.name)
                        except OSError:
                            pass
                if docs:
                    merged = _ocst.merge_property_fields(docs)
                    _pi.save_property_info(data_dir, cid, merged)
                    try:
                        record_view(data_dir, viewer.user, viewer.role, "/property/collect",
                                    action="ocr_collect")
                    except Exception:
                        pass
                self._redirect("/case?id=" + quote(cid))
                return
            if route in ("/fax/new", "/fax/send"):
                # 物確FAX作成 / 送信ゲート（人間確認）。ともに operation 経由（役割ゲート＋HMAC監査）。既定Mock。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                op = "bukkaku_send" if route == "/fax/new" else "fax_confirm_send"
                if route == "/fax/new":
                    p = {"case_id": (form.get("case_id", [""])[0] or "").strip(),
                         "to_number": (form.get("to_number", [""])[0] or "").strip()}
                else:
                    p = {"job_id": (form.get("job_id", [""])[0] or "").strip(),
                         "allow_real_send": (form.get("allow_real_send", [""])[0] or "").strip()}
                try:
                    _apply(data_dir, op, p, viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/fax'>← FAXへ戻る</a></p>")
                    return
                self._redirect("/fax")
                return
            if route == "/fax/webhook":
                # クラウドFAX業者からの着信Webhook（署名検証→正規化→OCR→物確回答抽出）。実プロバイダは人間ゲート。
                # secret未設定は未接続として拒否。loopback上の別プロセスも信頼しない。
                from hub_core import fax as _fax, local_ocr as _loc
                from hub_core.operations import apply_operation as _apply
                body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64*1024*1024)) if self.headers.get("Content-Length") else b""
                # CSRF防御(状態変更POST): application/json必須(text/plainのsimple-requestバイパスを塞ぐ・
                # クロスオリジンはプリフライトを強制)＋Originが非loopbackなら拒否。ブラウザからのCSRF-to-localhostを封じる。
                _ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if _ct != "application/json":
                    self._send_json(415, {"error": "JSON形式で送信してください。"})
                    return
                _origin = (self.headers.get("Origin") or "").strip()
                if _origin and (urlsplit(_origin).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                    self._send_json(403, {"error": "安全確認できない送信元です。"})
                    return
                secret = os.environ.get("FAX_WEBHOOK_SECRET", "").strip()
                sig = self.headers.get("X-Fax-Signature", "")
                if not secret:
                    self._send_json(503, {"error": "受信接続の署名設定が未完了です。"})
                    return
                if not _fax.verify_webhook(secret, body, sig):
                    self._send_json(401, {"error": "受信元を確認できませんでした。"})
                    return
                try:
                    payload = json.loads(body or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._send_json(400, {"error": "受信内容の形式が不正です。"})
                    return
                try:
                    inbound = _fax.normalize_inbound(payload)
                except _fax.FaxError as exc:
                    self._send_json(exc.code, {"error": _public_exception_message(exc)})
                    return
                # OCR: 実運用は media を無料ローカルOCR。mock/testは ocr_text をインラインで受ける。
                ocr_text = str(payload.get("ocr_text") or "")
                mp = str(payload.get("media_path") or "")
                if not ocr_text and mp and _loc.available():
                    p = Path(mp).resolve()
                    if p.is_relative_to(Path(data_dir).resolve()) and p.is_file():
                        try:
                            ocr_text = _loc.ocr_any(str(p))
                        except Exception:
                            ocr_text = ""
                res = _apply(data_dir, "fax_receive",
                             {"inbound": inbound, "ocr_text": ocr_text}, "fax-webhook", "代表")
                self._send_json(200, {"ok": True, "fax_id": res.get("fax_id"),
                                      "reply": res.get("reply"), "verified": bool(secret)})
                return
            if route in ("/reins/prepare", "/reins/record"):
                # REINS入稿準備 / 登録番号記録（operation経由・役割ゲート＋HMAC監査）。REINSには接続しない。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                if route == "/reins/prepare":
                    op = "reins_prepare"
                    p = {"case_id": (form.get("case_id", [""])[0] or "").strip(),
                         "mediation": (form.get("mediation", [""])[0] or "").strip(),
                         "contract_date": (form.get("contract_date", [""])[0] or "").strip()}
                else:
                    op = "reins_record"
                    p = {"case_id": (form.get("case_id", [""])[0] or "").strip(),
                         "reins_no": (form.get("reins_no", [""])[0] or "").strip()}
                try:
                    _apply(data_dir, op, p, viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/reins'>← REINSへ戻る</a></p>")
                    return
                self._redirect("/reins")
                return
            if route in ("/it/create", "/it/check", "/it/advance", "/it/gate/save",
                         "/it/consent", "/it/deliver", "/it/propose", "/it/schedule", "/it/keiyaku37"):
                # IT重説の表面（operation経由・役割ゲート＋HMAC監査＋fail-closedゲートはコア側で強制）。
                # 実施可は運用開始ゲート＋法定4要件、交付は承諾＋記名確定＝いずれも op が 403 で返す。実送信なし。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)

                def _f(k):
                    return (form.get(k, [""])[0] or "").strip()
                try:
                    if route == "/it/keiyaku37":
                        # 実施済セッションから37条書面（電子契約封筒・esign_create）を作る。BYO・Mock既定・実送信なし。
                        case_id = _f("case_id")
                        signer = _f("signer")
                        env_id = "KY37-" + (case_id or "case") + "-" + hashlib.sha256(
                            f"{case_id}|{now_jst_iso()}".encode("utf-8")).hexdigest()[:8].upper()
                        _apply(data_dir, "esign_create",
                               {"envelope_id": env_id, "title": f"37条書面（契約書面） {case_id}".strip(),
                                "signers": signer}, viewer.user, viewer.role)
                    elif route == "/it/propose":
                        # 候補日時をLINEで送付（line_send=queued・実送信しない）。M5導線の①。
                        _apply(data_dir, "line_send",
                               {"to_user": _f("to_user"), "kind": "push", "text": _f("text")},
                               viewer.user, viewer.role)
                    else:
                        opmap = {"/it/create": "it_session_create", "/it/check": "it_check_requirement",
                                 "/it/advance": "it_advance", "/it/gate/save": "it_gate_set",
                                 "/it/consent": "juusetsu_consent_record", "/it/deliver": "juusetsu_deliver",
                                 "/it/schedule": "it_schedule_confirm"}
                        keymap = {"/it/create": ("case_id", "scheduled_at", "video_url"),
                                  "/it/check": ("session_id", "requirement", "met"),
                                  "/it/advance": ("session_id", "to_state"),
                                  "/it/gate/save": ("license_no", "takkenshi_reg", "guideline_confirmed"),
                                  "/it/consent": ("case_id", "recipient", "method", "file_format"),
                                  "/it/deliver": ("case_id", "doc_id", "recipient", "version"),
                                  "/it/schedule": ("session_id", "scheduled_at", "video_url", "to_user")}
                        _apply(data_dir, opmap[route], {k: _f(k) for k in keymap[route]},
                               viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404, 409) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/it'>← IT重説へ戻る</a></p>")
                    return
                self._redirect("/it")
                return
            if route == "/line/viewing":
                # LINE着信の内見希望→内見予約(viewing_schedule)＋確認返信ドラフト(line_send=queued・送信は別途ゲート)。
                # 日時・物件は担当が確定した値のみ。実送信はしない（確認返信はoutboxに積むだけ）。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                cid = (form.get("case_id", [""])[0] or "").strip()
                at = (form.get("event_at", [""])[0] or "").strip()
                to_user = (form.get("to_user", [""])[0] or "").strip()
                try:
                    _apply(data_dir, "viewing_schedule", {"case_id": cid, "event_at": at}, viewer.user, viewer.role)
                    if to_user:  # 確認返信を outbox に積む（送信は /line/send の人間ゲートで）
                        _apply(data_dir, "line_send",
                               {"to_user": to_user, "kind": "push",
                                "text": f"内見のご予約を承りました。日時：{at.replace('T', ' ')}。"
                                        "ご都合が合わない場合はこのままご返信ください。"},
                               viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/line'>← LINEへ戻る</a></p>")
                    return
                self._redirect("/line")
                return
            if route == "/line/property-card":
                # 公開物件をFlexカード（カルーセル）で outbox に積む（認証viewer・operation経由＝役割ゲート＋HMAC監査）。
                # property_ids はチェックボックスの複数値（リスト）を保持する（handle_op はリストを畳むので専用route）。
                # 実送信はしない＝送信は /line/send（line_confirm_send）の人間ゲートで。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                to_user = (form.get("to_user", [""])[0] or "").strip()
                pids = [x.strip() for x in form.get("property_ids", []) if x.strip()]
                badge = (form.get("badge", [""])[0] or "").strip()
                try:
                    _apply(data_dir, "line_flex_property_send",
                           {"to_user": to_user, "property_ids": pids, "badge": badge},
                           viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/line'>← LINEへ戻る</a></p>")
                    return
                self._redirect("/line")
                return
            if route == "/line/pull":
                # harnessから着信をpull取込（認証済みviewerのみ・operation経由＝役割ゲート＋HMAC監査）。
                # 未設定なら no-op、認証/接続失敗は台帳に何も書かない（fail-closed）。実送信は発生しない。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                try:
                    _apply(data_dir, "line_harness_pull", {}, viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/line'>← LINEへ戻る</a></p>")
                    return
                self._redirect("/line")
                return
            if route == "/line/it-start":
                # 会話ファースト: 会話スレッドから IT重説を1クリックで開始。案件が無ければ顧客＋案件を自動作成し、
                # IT重説セッションを用意して /it へ。ユーザーに案件IDを打たせない（operation経由＝役割ゲート＋監査）。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                p = {"to_user": (form.get("to_user", [""])[0] or "").strip(),
                     "display_name": (form.get("display_name", [""])[0] or "").strip()}
                try:
                    _apply(data_dir, "line_start_it_juusetsu", p, viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='/line'>← LINEへ戻る</a></p>")
                    return
                self._redirect("/it")   # 起点は会話・確認と証跡は /it へ橋渡し
                return
            if route in ("/calls/directory", "/calls/code", "/line/new", "/line/send",
                         "/line/inquiry", "/line/inquiry-resolve", "/line/hearing"):
                # 物確電話設定 / LINE送信 / 案内可否確認 / 希望条件台帳化（全て operation 経由・役割ゲート＋HMAC監査）。既定Mock。
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                from hub_core.operations import OpError as _OpErr, apply_operation as _apply
                form = parse_qs(self._read_body(), keep_blank_values=True)
                opmap = {"/calls/directory": "caller_directory_add", "/calls/code": "bukkaku_code_assign",
                         "/line/new": "line_send", "/line/send": "line_confirm_send",
                         "/line/inquiry": "inquiry_create", "/line/inquiry-resolve": "inquiry_resolve",
                         "/line/hearing": "hearing_create"}
                keymap = {"/calls/directory": ("number", "company", "note"),
                          "/calls/code": ("case_id", "code"),
                          "/line/new": ("to_user", "text", "kind", "case_id"),
                          "/line/send": ("msg_id", "allow_real_send"),
                          "/line/inquiry": ("to_user", "text"),
                          "/line/inquiry-resolve": ("inquiry_id", "availability", "property_status", "note"),
                          "/line/hearing": ("to_user", "text")}
                p = {k: (form.get(k, [""])[0] or "").strip() for k in keymap[route]}
                back = "/line" if route.startswith("/line") else "/calls"
                try:
                    _apply(data_dir, opmap[route], p, viewer.user, viewer.role)
                except _OpErr as exc:
                    self._send_html(exc.code if exc.code in (400, 403, 404) else 400,
                                    f"<h1>できません</h1><p>{_esc(_public_exception_message(exc))}</p><p><a href='{back}'>← 戻る</a></p>")
                    return
                self._redirect(back)
                return
            if route in ("/telephony/webhook", "/line/webhook"):
                # 着信webhook（電話/LINE）。secret未設定は未接続として拒否し、署名を必須にする。
                from hub_core.operations import apply_operation as _apply
                body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64*1024*1024)) if self.headers.get("Content-Length") else b""
                _ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if _ct != "application/json":
                    self._send_json(415, {"error": "JSON形式で送信してください。"})
                    return
                _origin = (self.headers.get("Origin") or "").strip()
                if _origin and (urlsplit(_origin).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                    self._send_json(403, {"error": "安全確認できない送信元です。"})
                    return
                is_line = route == "/line/webhook"
                if is_line:
                    from hub_core import line as _mod
                    secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
                    sig = self.headers.get("X-Line-Signature", "")
                    verify = _mod.verify_webhook
                else:
                    from hub_core import fax as _faxmod
                    secret = os.environ.get("TELEPHONY_WEBHOOK_SECRET", "").strip()
                    sig = self.headers.get("X-Telephony-Signature", "")
                    verify = _faxmod.verify_webhook
                if not secret:
                    self._send_json(503, {"error": "受信接続の署名設定が未完了です。"})
                    return
                if not verify(secret, body, sig):
                    self._send_json(401, {"error": "受信元を確認できませんでした。"})
                    return
                try:
                    payload = json.loads(body or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._send_json(400, {"error": "受信内容の形式が不正です。"})
                    return
                try:   # webhookは接続を落とさない（未捕捉例外→500 JSONで返す）
                    if is_line:
                        from hub_core import line as _mod2
                        events = _mod2.normalize_inbound(body)   # parse_events は bytes/str を要する
                        res = _apply(data_dir, "line_receive", {"events": events}, "line-webhook", "代表")
                        self._send_json(200, {"ok": True, "received": res.get("received")})
                    else:
                        from hub_core import telephony as _tel
                        call = _tel.normalize_inbound_call(payload)
                        res = _apply(data_dir, "call_receive",
                                     {"call": call, "caller_fax": str(payload.get("caller_fax") or "")},
                                     "telephony-webhook", "代表")
                        self._send_json(200, {"ok": True, "say": res.get("say"), "action": res.get("action"),
                                              "company": res.get("company"), "status": res.get("status")})
                except Exception as exc:
                    code = getattr(exc, "code", 500)
                    self._send_json(code if isinstance(code, int) and code in (400, 401, 403, 404) else 500,
                                    {"error": _public_exception_message(exc)})
                return
            if route == "/line/harness-webhook":
                # line-harness-oss の outgoing webhook 受け口（LINEネイティブの /line/webhook とは別）。
                # 署名 X-Webhook-Signature = hex(HMAC-SHA256(secret, body))。secret未設定は拒否。
                from hub_core import line as _lh
                from hub_core.operations import apply_operation as _apply
                body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64 * 1024 * 1024)) if self.headers.get("Content-Length") else b""
                if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    self._send_json(415, {"error": "JSON形式で送信してください。"})
                    return
                secret = os.environ.get("LINE_HARNESS_WEBHOOK_SECRET", "").strip()
                if not secret:
                    self._send_json(503, {"error": "受信接続の署名設定が未完了です。"})
                    return
                if not _lh.verify_harness_webhook(
                        secret, body, self.headers.get("X-Webhook-Signature", "")):
                    self._send_json(401, {"error": "受信元を確認できませんでした。"})
                    return
                try:
                    events = _lh.normalize_harness_inbound(body)
                    res = _apply(data_dir, "line_receive", {"events": events}, "line-harness-webhook", "代表")
                    self._send_json(200, {"ok": True, "received": res.get("received")})
                except Exception as exc:
                    code = getattr(exc, "code", 500)
                    self._send_json(code if isinstance(code, int) and code in (400, 401, 403, 404) else 500,
                                    {"error": _public_exception_message(exc)})
                return
            if route == "/login":
                form = parse_qs(self._read_body())
                user = (form.get("user", [""])[0] or "").strip()
                pw = form.get("password", [""])[0] or ""
                viewer = authenticate(data_dir, user, pw)
                if viewer is None:
                    _REQUEST.viewer = None
                    self._send_html(401, render_login("ユーザー名またはパスワードが違います"),
                                    allow_form=True)
                    return
                sid = create_session(viewer)
                self._redirect("/", cookie=_session_cookie(sid))
                return
            if route == "/logout":
                self._logout()
                return
            if route == "/juusetsu/finalize":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=False)
                status, body, location = finalize_juusetsu(data_dir, form, viewer)
                if location:
                    self._redirect(location)
                else:
                    self._send_html(status, body, allow_form=True)
                return
            if route == "/case/advance":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=False)
                status, body, location = advance_case(data_dir, form, viewer)
                if location:
                    self._redirect(location)
                else:
                    self._send_html(status, body)
                return
            if route == "/llm/save":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                if viewer.role not in ("責任者", "代表"):
                    self._redirect("/llm")
                    return
                form = parse_qs(self._read_body(), keep_blank_values=False)
                try:
                    _save_public_llm_mode(Path(data_dir), form)
                except Exception:
                    pass
                self._redirect("/llm")
                return
            if route == "/op":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=False)
                op = (form.get("op", [""])[0] or "").strip()
                status, body, location = handle_op(data_dir, op, form, viewer)
                if location:
                    self._redirect(location)
                else:
                    self._send_html(status, body)
                return
            if route == "/api/op":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._send_json(401, {"status": "error", "error": "ログインが必要です。"})
                    return
                _REQUEST.viewer = viewer
                try:
                    data = json.loads(self._read_body() or "{}")
                except Exception:
                    self._send_json(400, {"status": "error", "error": "受信内容の形式が不正です。"})
                    return
                if not isinstance(data, dict):   # 非オブジェクトJSON([]/文字列/数値)で data.get が例外→400へ
                    self._send_json(400, {"status": "error", "error": "受信内容の形式が不正です。"})
                    return
                # batchモード（後方互換: {batch:[{op,params}]}）＝監査P0 BULK-01
                from hub_core.operations import OpError, apply_operation, batch_apply
                if isinstance(data.get("batch"), list):
                    try:
                        res = batch_apply(data_dir, data["batch"], viewer.user, viewer.role)
                        res["status"] = "ok" if res["failed"] == 0 else "partial"
                        self._send_json(200, res)
                    except OpError as exc:
                        self._send_json(exc.code if exc.code in (400, 403, 404, 409) else 400,
                                        {"status": "error", "code": exc.code,
                                         "error": _public_exception_message(exc)})
                    except Exception:   # 未捕捉例外を500へ（トレース露出/切断を防ぐ・F3多層）
                        self._send_json(500, {"status": "error", "error": _public_failure()})
                    return
                op = str(data.get("op") or "").strip()
                params = data["params"] if isinstance(data.get("params"), dict) else {k: v for k, v in data.items() if k != "op"}
                try:
                    res = apply_operation(data_dir, op, params, viewer.user, viewer.role)
                    res["status"] = "ok"
                    self._send_json(200, res)
                except OpError as exc:
                    self._send_json(exc.code if exc.code in (400, 403, 404, 409) else 400,
                                    {"status": "error", "code": exc.code,
                                     "error": _public_exception_message(exc)})
                except Exception:   # 未捕捉例外を500へ（F3多層防御）
                    self._send_json(500, {"status": "error", "error": _public_failure()})
                return
            if route == "/doc/finalize":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=True)
                doc_id = (form.get("doc_id", [""])[0] or "").strip()
                ver = form.get("version", [""])[0]
                try:
                    version = int(ver) if ver else None
                except ValueError:
                    version = None
                from hub_core.access import document_access_allowed
                if not document_access_allowed(data_dir, viewer, doc_id, version):
                    self._send_html(403, "<h1>この書類は確定できません</h1>")
                    return
                # 多層防御: コア(chat_bridge.finalize)もrole gateを持つが、エンドポイントでも明示的に拒否する
                # (UI可視性に依存しない・サーバ側で必ず役割を強制)。
                if not viewer or viewer.role != "宅建士":
                    self._redirect("/console?doc=" + quote(doc_id) + "&fin_err="
                                   + quote("記名確定は本人確認済みの宅地建物取引士だけが実行できます。"))
                    return
                name = (form.get("takkenshi_name", [""])[0] or "").strip()
                lic = (form.get("license_no", [""])[0] or "").strip()
                from hub_core import chat_bridge as _cb
                try:
                    _cb.finalize(data_dir, "", name, lic, None, viewer, confirm=True, doc_id=doc_id, version=version)
                    self._redirect("/console?doc=" + quote(doc_id) + "&finalized=1")
                except _cb.BridgeError as exc:
                    self._redirect("/console?doc=" + quote(doc_id) + "&fin_err=" + quote(
                        _public_exception_message(
                            exc, "記名確定できませんでした。入力内容と権限を確認してください。")))
                return
            if route == "/doc/save":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=True)
                doc_id = (form.get("doc_id", [""])[0] or "").strip()
                if not doc_id:
                    self._redirect("/console")
                    return
                from hub_core.access import document_access_allowed
                if not document_access_allowed(data_dir, viewer, doc_id):
                    self._send_html(403, "<h1>この書類は編集できません</h1>")
                    return
                body = form.get("body", [""])[0] or ""
                kind = (form.get("kind", [""])[0] or "").strip()
                fmt = (form.get("fmt", ["md"])[0] or "md").strip()
                try:
                    from hub_core import documents
                    with documents.document_transaction(data_dir, doc_id):
                        # 確定状態の判定から監査コミットまでを一つの排他範囲に置く。
                        finalized = _doc_is_finalized(data_dir, doc_id)
                        if finalized and viewer.role not in ("宅建士", "責任者", "代表"):
                            self._send_html(403, "<h1>この書類は編集できません</h1>"
                                                 "<p>記名確定された書類の書き換えは、"
                                                 "宅地建物取引士・責任者・代表のみが行えます。</p>")
                            return
                        doc_meta_path = (
                            Path(data_dir) / "documents" / documents._safe_doc_id(doc_id) / "meta.json")
                        previous_meta = (
                            doc_meta_path.read_bytes() if doc_meta_path.is_file() else None)
                        previous_version = None
                        latest = documents.latest_version(data_dir, doc_id)
                        if latest:
                            previous_version = documents.get_version(data_dir, doc_id, latest)
                            previous_fields = previous_version.get("meta") or {}
                            kind = str(previous_fields.get("kind") or kind)
                            fmt = str(previous_fields.get("fmt") or fmt)
                        saved = documents.save_version(
                            data_dir, doc_id, body, kind=kind, fmt=fmt, author=viewer.user,
                            company_profile_hash=(
                                (previous_version.get("meta") or {}).get("company_profile_hash", "")
                                if previous_version is not None else None
                            ),
                        )
                        if finalized:
                            try:
                                from hub_core.audit import append_events
                                append_events(data_dir / "audit_log.jsonl", [{
                                    "actor": viewer.user, "action": "finalized_doc_edited",
                                    "target": doc_id, "gate_status": "edited",
                                    "timestamp": now_jst_iso(),
                                    "source_ref": "serve/doc/save"}])
                            except Exception:
                                documents.rollback_uncommitted_version(
                                    data_dir, doc_id, saved, previous_meta)
                                raise
                except Exception:      # noqa: BLE001
                    # 黙って saved=1 にしない（保存できていないのに保存済みに見えるのが最悪）
                    self._send_html(500, "<h1>保存できませんでした</h1>"
                                         "<p>書類本文と監査記録を一緒に保存できませんでした。"
                                         "元の版は変更していません。</p>")
                    return
                self._redirect("/console?doc=" + quote(doc_id) + "&saved=1")
                return
            if route == "/maisoku/new":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=True)
                case_id = (form.get("case", [""])[0] or "").strip()
                from hub_core.access import case_access_allowed
                if not case_access_allowed(data_dir, viewer, case_id):
                    self._send_html(403, "<h1>この案件の書類は作成できません</h1>")
                    return
                try:
                    did = make_maisoku_from_case(data_dir, case_id)
                except Exception:
                    did = None
                self._redirect("/maisoku?doc=" + quote(did) if did else "/maisoku")
                return
            if route == "/maisoku/edit":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._redirect("/login")
                    return
                _REQUEST.viewer = viewer
                form = parse_qs(self._read_body(), keep_blank_values=True)
                doc_id = (form.get("doc_id", [""])[0] or "").strip()
                if not doc_id:
                    self._redirect("/maisoku")
                    return
                from hub_core.access import document_access_allowed
                if not document_access_allowed(data_dir, viewer, doc_id):
                    self._send_html(403, "<h1>このマイソクは編集できません</h1>")
                    return
                from hub_core import maisoku as _ms, documents
                try:
                    raw_base = (form.get("base_version", [""])[0] or "").strip()
                    base_version = int(raw_base)
                    if base_version < 1:
                        raise ValueError("invalid version")
                    with documents.document_transaction(data_dir, doc_id):
                        latest = documents.latest_version(data_dir, doc_id)
                        if latest != base_version:
                            self._send_html(
                                409,
                                "<h1>ほかの変更が先に保存されています</h1>"
                                "<p>内容を上書きしないため保存を止めました。最新版を開き直して、変更を反映してください。</p>"
                                f"<p><a href='/maisoku/edit?doc={quote(doc_id)}'>最新版を開く</a></p>",
                            )
                            return
                        cur = documents.get_version(data_dir, doc_id, latest)
                        current = json.loads(cur["body"] or "{}")
                        if not isinstance(current, dict) or (cur.get("meta") or {}).get("kind") != "maisoku":
                            raise ValueError("invalid maisoku data")

                        profile_hash = str(
                            (cur.get("meta") or {}).get("company_profile_hash") or "")
                        from hub_core import branding as _branding
                        bound_company = _branding.load_snapshot(data_dir, profile_hash)
                        company_fields = set(_ms.COMPANY_TO_OBI.values())
                        fields = {k: current.get(k, "") for k in _ms.PRESERVED_KEYS}
                        for key in _ms.EDITABLE_FIELD_KEYS:
                            if key not in company_fields:
                                fields[key] = form.get(key, [""])[0] or ""
                        for slot in _ms.PHOTO_SLOTS:
                            if slot in form:
                                fields[slot] = form.get(slot, [""])[0] or ""

                        variant = (form.get("_variant", [""])[0] or "").strip()
                        xlsx_variant = (form.get("_xlsx_variant", [""])[0] or "").strip().upper()
                        accent = (form.get("_accent", [""])[0] or "").strip()
                        font = (form.get("_font", [""])[0] or "").strip()
                        if (variant not in _ms.VARIANTS
                                or xlsx_variant not in _ms.XLSX_VARIANTS
                                or not _ms._valid_hex(accent)
                                or font not in _ms.DISPLAY_FONTS):
                            self._send_html(
                                400,
                                "<h1>保存内容を確認してください</h1>"
                                "<p>様式、ブランド色、または書体の指定を確認できません。</p>"
                                f"<p><a href='/maisoku/edit?doc={quote(doc_id)}'>編集画面へ戻る</a></p>",
                            )
                            return
                        fields["_variant"] = variant
                        fields["_xlsx_variant"] = xlsx_variant
                        fields["_accent"] = accent.lower()
                        fields["_font"] = font
                        if not str(fields.get("property") or "").strip():
                            fields["property"] = str(fields.get("property_name") or "").strip()
                        fields = _ms.fields_with_company(
                            fields, bound_company, overwrite=True)

                        documents.save_version(
                            data_dir, doc_id,
                            json.dumps(fields, ensure_ascii=False, indent=2),
                            kind="maisoku", fmt="txt", author=viewer.user,
                            company_profile_hash=profile_hash,
                        )
                except (ValueError, json.JSONDecodeError):
                    self._send_html(
                        400,
                        "<h1>保存できませんでした</h1>"
                        "<p>保存データまたは版情報を確認できません。元の版は変更していません。</p>"
                        f"<p><a href='/maisoku/edit?doc={quote(doc_id)}'>編集画面へ戻る</a></p>",
                    )
                    return
                except Exception:
                    self._send_html(
                        500,
                        "<h1>保存できませんでした</h1>"
                        "<p>書類を保存できませんでした。元の版は変更していません。時間をおいてもう一度お試しください。</p>"
                        f"<p><a href='/maisoku/edit?doc={quote(doc_id)}'>編集画面へ戻る</a></p>",
                    )
                    return
                self._redirect("/maisoku/edit?doc=" + quote(doc_id) + "&saved=1")
                return
            if route == "/chat/stream":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._send_json(401, {"error": "ログインが必要です。"})
                    return
                _REQUEST.viewer = viewer
                try:
                    data = json.loads(self._read_body() or "{}")
                except Exception:
                    self._send_json(400, {"error": "受信内容の形式が不正です。"})
                    return
                msg = str(data.get("message") or "").strip()
                history = data.get("history") if isinstance(data.get("history"), list) else []
                if not msg:
                    self._send_json(400, {"error": "メッセージが空です。"})
                    return
                from hub_core import chat_history
                thread = str(data.get("thread") or "").strip()
                # IDOR防止: 他ユーザー所有のスレッドには書かせない→新規スレッドに切替
                if thread:
                    o = chat_history.owner_of(data_dir, thread)
                    if o is not None and o != viewer.user:
                        thread = ""
                if not thread:
                    thread = chat_history.new_thread_id()
                # ユーザー発話を会話スレッドに保存(所有者=viewer・リロードで復元・履歴一覧に出る)
                try:
                    from hub_core.backup import portable_snapshot_lock
                    with portable_snapshot_lock(data_dir):
                        chat_history.append_turn(data_dir, thread, "user", msg, owner=viewer.user)
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                from hub_core.chat_llm import converse_stream
                final_ev = None
                try:
                    for ev in converse_stream(data_dir, msg, history, viewer):
                        if ev.get("type") == "final":
                            ev["thread_id"] = thread  # クライアントに採番したスレッドを返す
                            final_ev = ev
                        self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                except Exception as exc:
                    try:
                        self.wfile.write(("data: " + json.dumps(
                            {"type": "final", "reply": "会話を続けられませんでした。時間をおいてもう一度お試しください。", "thread_id": thread,
                             "tool_events": [], "pending_confirmations": []}, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                if final_ev:
                    from hub_core.backup import portable_snapshot_lock
                    with portable_snapshot_lock(data_dir):
                        _log_chat(data_dir, viewer, msg, final_ev)
                        # AI応答を会話スレッドに保存
                        try:
                            chat_history.append_turn(
                                data_dir, thread, "assistant", final_ev.get("reply", ""),
                                owner=viewer.user,
                                meta={"provider": final_ev.get("provider"),
                                      "tools": [e.get("tool") for e in (final_ev.get("tool_events") or [])],
                                      "pending": len(final_ev.get("pending_confirmations") or [])})
                        except Exception:
                            pass
                return
            if route == "/chat":
                viewer = self._resolve_viewer()
                if viewer is None:
                    self._send_json(401, {"error": "ログインが必要です", "reply": "ログインしてください。"})
                    return
                _REQUEST.viewer = viewer
                try:
                    data = json.loads(self._read_body() or "{}")
                except Exception:
                    self._send_json(400, {"error": "受信内容の形式が不正です。", "reply": "リクエストが不正です。"})
                    return
                msg = str(data.get("message") or "").strip()
                history = data.get("history") if isinstance(data.get("history"), list) else []
                if not msg:
                    self._send_json(400, {"error": "メッセージが空です。", "reply": "メッセージが空です。"})
                    return
                try:
                    from hub_core.chat_llm import converse
                    res = converse(data_dir, msg, history, viewer)
                except Exception:  # API/ネットワーク失敗で500を返す(本文はPIIを含めない)
                    self._send_json(502, {"reply": "会話を処理できませんでした。時間をおいてもう一度お試しください。",
                                          "tool_events": [], "pending_confirmations": []})
                    return
                _log_chat(data_dir, viewer, msg, res)  # 会話の利用ログ(redact済)
                self._send_json(200, res)
                return
            # 未実装の業務データ書込POSTは拒否(明示ルートのみ許可)
            self._send_html(501, "<h1>この操作は利用できません</h1><p>画面の操作ボタンからやり直してください。</p>")

        def log_message(self, fmt, *args):
            # PIIを含みうる検索語・クエリ・POST body をアクセスログへ残さない。
            safe_route = urlsplit(getattr(self, "path", "") or "").path or "-"
            sys.stderr.write(f"[ainote] {getattr(self, 'command', '-')} {safe_route}\n")

    return RiHubHandler


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(data_dir: Path, port: int, open_browser: bool = False):
    handler = make_handler(data_dir)
    with _Server(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"あいのて {url}  data-dir={data_dir}")
        print("Ctrl-C で停止")
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n停止しました")


# ---------------------------------------------------------------------------
# selftest: 一時fixtureで全ルート200 + POST allowlist + 外部資産0 を検証
# ---------------------------------------------------------------------------
def _write_fixture(out_dir: Path):
    """全画面に最低1行ヒットする代表fixtureを書く (hub.py は実行しない)。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks_header = [
        "タスクID", "キュー", "状態", "優先度", "タイトル", "ポータル", "顧客名",
        "物件参照", "担当", "ゲート", "保留理由", "承認役割", "元反響ID", "作成日時",
    ]
    tasks = [
        ["T-001", "Inbox", "open", "P1", "SUUMO反響分類: 佐藤 太郎 / 内見", "suumo",
         "佐藤 太郎", "SUUMO-B-001", "田村", "", "", "", "PLEAD-001", "2026-06-13T20:57:51+09:00"],
        ["T-002", "Today", "open", "P0", "一次返信ドラフト: 鈴木 花子 / 資料請求", "lifull_homes",
         "鈴木 花子", "HOMES-R-002", "田村", "send", "", "", "PLEAD-002", "2026-06-13T20:57:51+09:00"],
        ["T-003", "Viewing", "open", "P1", "内見前日確認: 駅前レジデンス101", "suumo",
         "山田 太郎", "CASE-001", "田村", "", "", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-004", "Research", "waiting", "P2", "行政資料請求/確認: 不動産登記情報", "",
         "", "CASE-001", "田村", "professional", "", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-005", "Hold", "hold", "P1", "OCR/書類確認: 本人確認 / DOC-002", "",
         "鈴木 花子", "CASE-002", "田村", "privacy", "個人情報注意", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-006", "Applications", "open", "P1", "申込書受領: 申込 / DOC-001", "",
         "佐藤 太郎", "CASE-001", "田村", "document", "要分類", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-007", "Ads", "hold", "P0", "広告公開Hold: 断定表現", "rakumachi",
         "投資法人A", "AD-001", "田村", "publish", "広告/転載可否不明", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-008", "Hold", "hold", "P0", "契約Hold: 青山ハイツ202 / 37条書面が未整備", "",
         "高橋 三郎", "CASE-003", "田村", "contract", "37条未整備", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-009", "Hold", "hold", "P0", "Money Hold: 入金 / MONEY-002 / 入金未確認", "",
         "高橋 三郎", "CASE-003", "経理", "money", "入金未確認", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-010", "Management", "open", "P1", "Management Hold: 退去精算根拠未確認", "",
         "山田家主", "KANRI-001", "田村", "", "", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-011", "Reports", "open", "P2", "媒介活動報告ドラフト: CASE-001", "",
         "山田 太郎", "CASE-001", "田村", "", "", "", "", "2026-06-13T20:57:51+09:00"],
        ["T-012", "Hold", "hold", "P0", "送信/対応保留: 在庫未確認 - 佐藤 太郎", "suumo",
         "佐藤 太郎", "SUUMO-B-001", "田村", "send", "stock_unknown", "", "PLEAD-001",
         "2026-06-13T20:57:51+09:00"],
        ["T-013", "Approval", "approval", "P0", "重説ドラフト確認: juusetsu_draft.md", "",
         "高橋 三郎", "CASE-003", "田村", "contract", "", "宅建士", "", "2026-06-13T20:57:51+09:00"],
    ]
    with (out_dir / "tasks.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(tasks_header)
        w.writerows(tasks)

    with (out_dir / "portal_leads.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["反響ID", "ポータル", "取込方法", "受信日時", "顧客名", "連絡先",
                    "物件参照", "問い合わせ種別", "同意状態", "返信ゲート", "保留理由", "原文参照"])
        w.writerow(["PLEAD-001", "SUUMO", "mail", "2026-06-12T10:00:00+09:00", "佐藤 太郎",
                    "saito@example.com", "SUUMO-B-001", "内見", "unknown", "hold",
                    "stock_unknown", "mail:suumo:001"])
        w.writerow(["PLEAD-002", "LIFULL HOME'S", "mail", "2026-06-12T11:00:00+09:00", "鈴木 花子",
                    "suzuki@example.com", "HOMES-R-002", "資料請求", "opt_in", "ready",
                    "", "mail:homes:002"])

    with (out_dir / "cases.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["案件ID", "顧客ID", "顧客名", "物件ID", "物件名", "取引種別",
                    "状態", "ゲート状態", "保留種別", "元データ", "元ツール"])
        w.writerow(["CASE-001", "CUST-001", "山田 太郎", "PROP-001", "駅前レジデンス101",
                    "賃貸", "進行中", "pass", "", "input.csv", "ri-crm"])
        w.writerow(["CASE-003", "CUST-003", "高橋 三郎", "PROP-003", "青山ハイツ202",
                    "売買", "契約準備", "hold", "contract", "input.csv", "ri-keiyaku"])

    with (out_dir / "customers.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["顧客ID", "顧客名", "連絡先", "LINEユーザーID", "状態",
                    "ゲート状態", "保留種別", "元データ", "元ツール"])
        w.writerow(["CUST-001", "山田 太郎", "yamada@example.com", "U111", "内見確定",
                    "pass", "", "L-001", "ri-crm"])

    with (out_dir / "events.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["イベントID", "元ツール", "イベント種別", "イベント日時",
                    "顧客ID", "案件ID", "物件ID", "元データ"])
        w.writerow(["EV-001", "ri-crm", "内見確定", "2026-06-12T11:00:00",
                    "CUST-001", "CASE-001", "PROP-001", "events.csv:2"])

    with (out_dir / "hold_queue.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["保留ID", "タスクID", "反響ID", "ポータル", "顧客名", "物件参照",
                    "保留種別", "理由", "解除条件", "解除役割", "ゲート"])
        w.writerow(["HOLD-001", "T-012", "PLEAD-001", "SUUMO", "佐藤 太郎", "SUUMO-B-001",
                    "stock_unknown", "在庫未確認", "物確ログで募集中を確認する", "担当者", "send"])
        w.writerow(["HOLD-002", "T-008", "", "", "高橋 三郎", "CASE-003",
                    "contract_incomplete", "37条書面が未整備", "宅建士が37条書面を整備する",
                    "宅建士", "contract"])

    with (out_dir / "approval_queue.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["承認ID", "タスクID", "反響ID", "ポータル", "顧客名",
                    "承認役割", "理由", "判断"])
        w.writerow(["APP-001", "T-013", "", "", "高橋 三郎", "宅建士",
                    "重説ドラフトは宅建士確認前に交付不可", "pending"])
        w.writerow(["APP-002", "T-009", "", "", "高橋 三郎", "経理/責任者",
                    "契約金額・精算額の確認が必要", "pending"])

    with (out_dir / "approval_ledger.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["承認ID", "タスクID", "ゲート", "確認役割", "確認対象",
                    "理由", "判断", "元データ", "記録日時"])
        w.writerow(["APP-001", "T-013", "contract", "宅建士", "重説ドラフト",
                    "宅建士確認前は交付不可", "pending", "approval.csv:2", "2026-06-13T21:00:00"])

    with (out_dir / "claims_register.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["受付ID", "受付日時", "案件ID", "物件ID", "顧客名", "種別",
                    "緊急度", "緊急度理由", "クレーム化リスク", "近隣トラブル",
                    "行政連絡", "終結条件", "終結状態", "元データ"])
        w.writerow(["CLM-001", "2026-06-12 09:30", "CASE-001", "PROP-001", "入居者A",
                    "設備故障", "P1", "設備不具合", "なし", "非該当", "なし",
                    "業者手配と復旧確認", "未終結", "claims.csv:2"])

    with (out_dir / "recurrence_checklist.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["受付ID", "種別", "手順", "状態", "担当", "元データ"])
        w.writerow(["CLM-001", "設備故障", "原因の特定", "未確認", "営業 一郎", "claims.csv:2"])

    with (out_dir / "contract_version_register.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["書類ID", "案件ID", "書類種別", "版", "最新版", "最新判定", "元データ"])
        w.writerow(["DOC-002", "CASE-002", "重要事項説明書", "v2", "v2", "最新", "docs.csv:3"])
        w.writerow(["DOC-004", "CASE-003", "売買契約書", "v1", "v3", "旧版", "docs.csv:5"])

    with (out_dir / "filename_standardization.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["書類ID", "現ファイル名", "標準提案名", "案件ID", "リネーム要否", "元データ"])
        w.writerow(["DOC-001", "見積もり.pdf", "CASE-001_mitsumori_20260610.pdf",
                    "CASE-001", "要", "docs.csv:2"])

    with (out_dir / "original_disposal_register.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["書類ID", "案件ID", "書類種別", "原本状態", "処理要否", "元データ"])
        w.writerow(["DOC-004", "CASE-003", "売買契約書", "保管中", "要", "docs.csv:5"])

    with (out_dir / "governance_register.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["台帳ID", "カテゴリ", "名称", "状態", "詳細", "解除/管理役割", "元データ"])
        w.writerow(["GOV-001", "staff", "田村", "active", "宅建士登録あり",
                    "責任者", "staff.csv:2"])
        w.writerow(["GOV-002", "credential", "宅建士証", "expired", "更新期限切れ",
                    "責任者", "gov.csv:3"])

    with (out_dir / "id_crosswalk.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Hubキー", "元ツール", "案件ID", "顧客ID", "物件ID", "名寄せ別名", "元データ"])
        w.writerow(["HUBID-001", "ri-hub", "CASE-001", "CUST-001", "SUUMO-B-001",
                    "prop:suumo-b-001", "PLEAD-001"])

    audit_events = [
        {"event_id": "AUD-0001", "task_id": None, "actor": "ri-hub",
         "action": "portal_lead_ingested", "portal_lead_id": "PLEAD-001",
         "platform_id": "suumo", "reply_gate": "hold",
         "hold_reasons": ["stock_unknown"], "timestamp": "2026-06-13T20:57:51+09:00",
         "source_ref": "mail:suumo:001", "gate_status": "pass", "seq": 1},
        {"event_id": "AUD-0002", "task_id": "T-013", "actor": "ri-hub",
         "action": "chousa_outputs_ingested", "timestamp": "2026-06-13T20:57:52+09:00",
         "source_ref": "ri-chousa/out", "gate_status": "pass", "seq": 2},
    ]
    # 監査ログは**署名して書く**。HMACの無い行を直書きすると、その端末の鍵で検証できず、
    # 「お試しデータを入れた直後に最初の業務操作が409で止まる」（別のPCで必ず起きる）。
    # 追記APIを通せば、その端末の鍵で正しい連鎖になる。
    audit_log = out_dir / "audit_log.jsonl"
    try:
        from hub_core.audit import (
            _anchor_path as _audit_anchor_path,
            _anchor_state_path as _audit_anchor_state_path,
            append_events as _append,
        )
        # この関数は既存データへ足すseedではなく、全台帳を作り直すfixture生成器。
        # 本体だけ消して署名anchorを残すと改ざん検知が正しく発火するため、fixtureの
        # 監査3点も同時に初期化する。
        for old in (audit_log, _audit_anchor_path(audit_log),
                    _audit_anchor_state_path(audit_log)):
            old.unlink(missing_ok=True)
        _append(audit_log,
                [{k: v for k, v in ev.items() if k != "seq"} for ev in audit_events])
    except Exception:      # noqa: BLE001 鍵が使えない環境では監査サンプルを置かない
        pass                # （空でも画面は「該当データなし」で成立する）

    # 同梱の調査見本は業務データへコピーしない。見本を現在のドラフトとして記名確定する
    # 経路を作らず、デモ書類は sample 属性つきの documents ストアだけで扱う。

    # 閲覧監査(S0-5) サンプル — /viewlog 画面に行が出る状態にする
    view_events = [
        {"ts": "2026-06-13T21:00:01+09:00", "actor": "田村", "role": "担当",
         "action": "VIEW", "target": "/today"},
        {"ts": "2026-06-13T21:00:05+09:00", "actor": "代表", "role": "代表",
         "action": "VIEW", "target": "/hold"},
    ]
    with (out_dir / "view_audit.jsonl").open("w", encoding="utf-8") as fh:
        for ev in view_events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


# 外部ネットワーク混入検知用パターン (HTMLレスポンスに出てはならない)
EXTERNAL_MARKERS = ("http://", "https://", "//cdn", "googleapis", "gstatic",
                    "<script src", "url(http")


def _assert_no_external(body: str, where: str, failures: list):
    low = body.lower()
    for m in EXTERNAL_MARKERS:
        if m.lower() in low:
            failures.append(f"{where}: 外部URLマーカー検出 '{m}'")
    # <link> は一律禁止でなく href がローカルパスかで判定する（favicon をローカルで配るため）。
    for href in re.findall(r'<link\b[^>]*?href="([^"]*)"', low):
        if not href.startswith("/") or href.startswith("//"):
            failures.append(f"{where}: <link> の href が外部 '{href}'")



def _selftest_shows_data(body: str, rows: list) -> bool:
    """その画面に実データが出ているか。表・カードなど描き方に依存しない判定。"""
    if not rows:
        return "該当データなし" in body
    vals = [str(v or "").strip() for v in rows[0].values()]
    vals = [v for v in vals if len(v) >= 3 and v != "—"]
    if not vals:
        return "<td>" in body
    return any(html.escape(v) in body for v in vals)


def selftest() -> int:
    """一時fixtureで全ルート200 + データ行 + POST allowlist + 外部資産0を検証。"""
    failures = []
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "out"
        _write_fixture(out_dir)

        # 1) home
        status, body = build_response(out_dir, "/")
        if status != 200:
            failures.append(f"/ -> status {status}")
        elif 'class="kpi' not in body:
            failures.append("/ -> KPIカードが無い")
        elif "ダッシュボード" not in body:
            failures.append("/ -> ダッシュボードマーカー無し")
        else:
            print("PASS  /                       200 (KPIカードあり)")
        _assert_no_external(body, "/", failures)

        status, body = build_response(out_dir, "/home")
        if status != 200 or "何をしますか？" not in body:
            failures.append(f"/home -> status {status} or ワークスペースマーカー無し")
        else:
            print("PASS  /home                   200 (あいのてワークスペース)")
        _assert_no_external(body, "/home", failures)

        status, body = build_response(out_dir, "/juusetsu")
        if (status != 200
                or "記名できるドラフトがありません" not in body
                or 'action="/juusetsu/finalize"' in body):
            failures.append(f"/juusetsu -> status {status} or 実ドラフト不在ゲート異常")
        else:
            print("PASS  /juusetsu               200 (実ドラフト不在・記名不可)")
        _assert_no_external(body, "/juusetsu", failures)

        # 2) 全ページ (16画面 + 統合台帳)
        for p in ALL_PAGES:
            route = p["route"]
            status, b = build_response(out_dir, route)
            _, rows = load_page_data(out_dir, p)
            ok = True
            if status != 200:
                failures.append(f"{route} -> status {status}")
                ok = False
            elif not _selftest_shows_data(b, rows):
                # 表かカードかの見た目でなく「実データが画面に出ているか」で見る。
                # 表を前提にすると、読みやすいカード表示へ変えた画面が誤って落ちる。
                failures.append(f"{route} -> データが画面に出ていない (rows={len(rows)})")
                ok = False
            elif len(rows) == 0:
                failures.append(f"{route} -> fixtureで0行")
                ok = False
            _assert_no_external(b, route, failures)
            if ok:
                print(f"PASS  {route:<24} 200  rows={len(rows)}")

        # 3) 案件串刺し
        status, b = build_response(out_dir, "/case?id=CASE-001")
        if status != 200:
            failures.append(f"/case -> status {status}")
        elif "案件串刺し" not in b or "<td>" not in b:
            failures.append("/case -> 集約データが無い")
        else:
            print("PASS  /case?id=CASE-001         200 (串刺し集約あり)")
        _assert_no_external(b, "/case", failures)

        # 4) 検索 ?q= が件数を絞ること
        _, b_all = build_response(out_dir, "/hold")
        _, b_q = build_response(out_dir, "/hold?q=stock_unknown")
        if b_all.count("<tr") <= b_q.count("<tr"):
            # フィルタ後の方が行数が少ない(=絞れている)はず
            failures.append("?q= でHold画面が絞れていない")
        else:
            print("PASS  ?q= 検索で件数が絞られる")
        _assert_no_external(b_q, "/hold?q=", failures)

        # 5) 404
        status, b = build_response(out_dir, "/nope")
        if status != 404:
            failures.append(f"/nope -> {status} (404期待)")
        else:
            print("PASS  /nope                    404")
        _assert_no_external(b, "/nope", failures)

        # 6) 状態変更の境界: PUT/DELETE/PATCH は持たず、POST は明示allowlistだけ。
        handler_cls = make_handler(out_dir)
        post_ok = True
        for forbidden in ("do_PUT", "do_DELETE", "do_PATCH"):
            if hasattr(handler_cls, forbidden):
                failures.append(f"書き込みハンドラ検出: {forbidden}")
                post_ok = False
        src = Path(__file__).read_text(encoding="utf-8")
        for method in ("PUT", "DELETE", "PATCH"):
            if ("def " + "do_" + method) in src:
                failures.append(f"ソースに書き込みハンドラ定義: do_{method}")
                post_ok = False
        essential_posts = {"/login", "/setup", "/op", "/api/op", "/doc/save",
                           "/doc/finalize", "/chat/stream", "/api/backup"}
        if not essential_posts <= _POST_ROUTES or _post_route_allowed("/unknown-write"):
            failures.append("POST allowlist が主要操作を欠くか、未知経路を許可している")
            post_ok = False
        if post_ok:
            print(f"PASS  状態変更境界 (明示POST {len(_POST_ROUTES)}経路・未知POSTは501)")

        # 7) HTMLにフォーム/送信ボタンが無いこと
        for route in ("/", "/hold", "/approval", "/money", "/case?id=CASE-001"):
            _, b = build_response(out_dir, route)
            if "<form" in b or 'type="submit"' in b or "<button" in b:
                failures.append(f"{route}: フォーム/操作ボタンを描画")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nALL PASS (全ルート200 / KPIカード / 串刺し / 検索絞込 / 404 / "
          "POST allowlist / 外部URL0)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="あいのて ローカルWeb UI (ループバック限定 / 外部送信は既定OFF)")
    parser.add_argument("--data-dir", default="out",
                        help="ri-hub 出力ディレクトリ (デフォルト: out)")
    parser.add_argument("--port", type=int, default=8765,
                        help="待ち受けポート (デフォルト: 8765)")
    parser.add_argument("--selftest", action="store_true",
                        help="一時fixtureで全ルート200+POST allowlist+外部資産0を検証し exit 0/1")
    parser.add_argument("--open", action="store_true",
                        help="起動時にブラウザを開く。未設定なら初回オンボーディング(/setup)へ誘導")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.open:
        os.environ["RI_HUB_ONBOARD"] = "1"  # 未設定なら / は /setup へ誘導

    data_dir = Path(args.data_dir).expanduser().resolve()
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"data-dir を新規作成しました: {data_dir}（初回オンボーディングへ）", file=sys.stderr)
    serve(data_dir, args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
