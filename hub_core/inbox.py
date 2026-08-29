"""M-inbox — 反響取込（Wave1 V5・Vault流）。

思想: **ファイルが正本**。`<data_dir>/反響/` に置かれた .eml（メール転送で自動化可能・
BYOメールで運用コスト¥0）を決定的にパースして portal_leads と一次返信タスクを生成する。
- 取込は冪等（lead_id = メールの決定的ハッシュ）＝何度走っても二重登録しない
- hub.db 再構築（vault.rebuild_business_tables）はこのモジュールの replay で反響を復元する
  （.eml 再パース＋手動登録は監査イベントのリプレイ）
- パーサは登録制（PARSERS）: SUUMO / LIFULL HOME'S の形式定義＋汎用フォールバック。
  ※両ポータルの形式は公開情報に基づく**形式定義**であり、実メールでの較正は掲載契約後に行う。
- stdlib のみ（email / hashlib / re）。LLM不使用・全ローカル。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

JST = timezone(timedelta(hours=9))
INBOX_DIR = "反響"


class InboxSkip(Exception):
    """取込スキップ（巨大ファイル等）。クラッシュさせず件数報告に載せる。"""


# ---------------------------------------------------------------------------
# パース（登録制パーサ）
# ---------------------------------------------------------------------------
_LABELS = {
    "customer_name": ("お名前", "氏名", "名前", "お客様名"),
    "customer_contact": ("メールアドレス", "電話番号", "連絡先", "TEL", "Mail"),
    "property_ref": ("物件名", "物件番号", "対象物件", "物件"),
    "inquiry_type": ("問い合わせ種別", "ご希望", "種別"),
}


def _labeled_lines(body: str) -> dict:
    """「ラベル：値」形式の行から値を抜く（全角/半角コロン両対応）。"""
    out = {}
    for line in body.splitlines():
        m = re.match(r"\s*[【\[]?([^：:【\]\[]{1,12})[】\]]?\s*[：:]\s*(.+)\s*$", line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        for key, names in _LABELS.items():
            if label in names and value and key not in out:
                out[key] = value
    return out


def _parse_suumo(msg, body: str) -> dict | None:
    """SUUMO反響通知の形式定義（From=@suumo.jp系 or 件名マーカー）。"""
    frm = str(msg.get("From", ""))
    subj = str(msg.get("Subject", ""))
    if "suumo" not in frm.lower() and "SUUMO" not in subj:
        return None
    d = _labeled_lines(body)
    d["platform_id"] = "SUUMO"
    return d


def _parse_homes(msg, body: str) -> dict | None:
    """LIFULL HOME'S反響通知の形式定義（From=@homes.co.jp系 or 件名マーカー）。"""
    frm = str(msg.get("From", ""))
    subj = str(msg.get("Subject", ""))
    if "homes.co.jp" not in frm.lower() and "HOME'S" not in subj and "LIFULL" not in subj:
        return None
    d = _labeled_lines(body)
    d["platform_id"] = "HOMES"
    return d


def _parse_generic(msg, body: str) -> dict:
    """汎用メール（自社サイトフォーム通知・直接メール）。最優先の実運用経路。"""
    d = _labeled_lines(body)
    frm = str(msg.get("From", ""))
    m = re.match(r"\s*\"?([^\"<]+?)\"?\s*<([^>]+)>", frm)
    if m:
        d.setdefault("customer_name", m.group(1).strip())
        d.setdefault("customer_contact", m.group(2).strip())
    else:
        d.setdefault("customer_contact", frm.strip())
    d.setdefault("platform_id", "メール")
    return d


PARSERS = [("suumo", _parse_suumo), ("homes", _parse_homes)]  # 汎用は常に最後


MAX_EML_BYTES = 5 * 1024 * 1024  # 反響/は転送自動化の投入口=準外部面。巨大ファイルは取込まない(F-d4)


def parse_eml(path: Path) -> dict:
    """1通の .eml → 反響dict（決定的・冪等キー lead_id つき）。
    冪等キー = Message-ID **＋本文内容ハッシュ** の複合（F-d1是正）:
    Message-IDは送信者が任意設定できるため、同一IDの別内容メールで正当反響が
    黙って抑止される攻撃を封じる（同一ファイル再走査は同一キー=冪等維持）。"""
    # サイズ判定は read の**前**に stat() で（wv敵対R2 B4-eml: 巨大ファイルを丸読みしない=poison-pill DoS封じ）
    try:
        sz = path.stat().st_size
    except OSError as e:
        raise InboxSkip(f"{path.name}: stat失敗({e})")
    if sz > MAX_EML_BYTES:
        raise InboxSkip(f"{path.name}: {sz}bytes > 上限{MAX_EML_BYTES}（取込スキップ）")
    raw = path.read_bytes()  # read は1回（F-d4・上限確認後）
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    part = msg.get_body(preferencelist=("plain",))
    body = part.get_content() if part is not None else ""
    if not body:
        # HTML専用メールのフォールバック（F-d2）: タグを剥いで本文に落とす
        hpart = msg.get_body(preferencelist=("html",))
        if hpart is not None:
            html = hpart.get_content()
            html = re.sub(r"<(br|/p|/div|/tr)[^>]*>", "\n", html, flags=re.I)
            body = re.sub(r"<[^>]+>", "", html)
            body = re.sub(r"&nbsp;", " ", body)
    data = None
    for _name, fn in PARSERS:
        data = fn(msg, body)
        if data is not None:
            break
    if data is None:
        data = _parse_generic(msg, body)
    mid = str(msg.get("Message-ID", "")).strip()
    # 内容ハッシュは**正規化本文＋From/Subject**（生バイトでない）: 正当な再転送では
    # Received等のヘッダだけが変わるため同一キー=重複しない。偽装（同一MID・別内容）は
    # 本文が違うので別キー=抑止不能（wv敵対R2の重複退行指摘の是正）。
    norm = re.sub(r"\s+", " ", body or "").strip()
    content_h = hashlib.sha256(
        (norm + "|" + str(msg.get("From", "")) + "|" + str(msg.get("Subject", "")))
        .encode("utf-8")).hexdigest()
    key = (mid + "|" + content_h) if mid else content_h
    lead_id = "L-EML-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10].upper()
    received = str(msg.get("Date", "")).strip()
    return {
        "portal_lead_id": lead_id,
        "platform_id": data.get("platform_id", "メール"),
        "source_method": "eml",
        "received_at": received,
        "customer_name": data.get("customer_name", ""),
        "customer_contact": data.get("customer_contact", ""),
        "property_ref": data.get("property_ref", ""),
        "inquiry_type": data.get("inquiry_type", ""),
        "consent_status": "",
        "reply_gate": "pending",
        "hold_reason": "",
        "raw_ref": str(path.name),
    }


# ---------------------------------------------------------------------------
# 取込（冪等）・再構築リプレイ
# ---------------------------------------------------------------------------
def _first_reply_task(lead: dict) -> dict:
    tid = "T-INBOX-" + lead["portal_lead_id"].split("-")[-1]
    return {
        "task_id": tid, "queue": "Today", "status": "open", "priority": "P1",
        "title": "一次返信ドラフト: " + (lead.get("customer_name") or lead["portal_lead_id"]),
        "platform_id": lead.get("platform_id", ""), "customer_name": lead.get("customer_name", ""),
        "property_id": lead.get("property_ref", ""), "assignee": "", "gate": "send",
        "hold_reason": "", "approval_role": "", "portal_lead_id": lead["portal_lead_id"],
        "created_at": datetime.now(JST).replace(microsecond=0).isoformat(),
    }


def scan_inbox(data_dir) -> list[dict]:
    """反響/ の .eml を全パース（取込はしない・読むだけ）。スキップは黙殺せず件数を持つ。"""
    root = Path(data_dir) / INBOX_DIR
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*.eml")):
        try:
            out.append(parse_eml(p))
        except InboxSkip:
            continue  # スキップ件数は count_skipped()（ingest結果の skipped_large）で開示
        except Exception:
            # 1通の異常（破損/想定外）が反響箱全体をクラッシュさせない（poison-pill隔離）
            continue
    return out


def count_skipped(data_dir) -> int:
    root = Path(data_dir) / INBOX_DIR
    if not root.is_dir():
        return 0
    n = 0
    for p in sorted(root.glob("*.eml")):
        try:
            if p.stat().st_size > MAX_EML_BYTES:
                n += 1
        except OSError:
            n += 1
    return n


def ingest_inbox(data_dir, actor: str = "inbox", *, _audit_write: bool = True) -> dict:
    """反響/ の .eml を portal_leads＋一次返信タスクへ冪等取込。
    戻り値: {"new": 新規件数, "seen": 既存スキップ, "tasks": 生成タスク数}。"""
    from hub_core.operations import _audit, _event, _full_row
    from hub_core.store import SqliteStore
    root = Path(data_dir)
    if not (root / "hub.db").exists() and not scan_inbox(data_dir):
        return {"new": 0, "seen": 0, "tasks": 0}
    st = SqliteStore(root / "hub.db")
    new = seen = tasks = 0
    for lead in scan_inbox(data_dir):
        if st.query("portal_leads", "portal_lead_id = ?", (lead["portal_lead_id"],)):
            seen += 1
            continue
        st.insert_row("portal_leads", _full_row("portal_leads", lead))
        new += 1
        task = _first_reply_task(lead)
        if not st.query("tasks", "task_id = ?", (task["task_id"],)):
            st.insert_row("tasks", _full_row("tasks", task))
            tasks += 1
        if _audit_write:
            _audit(data_dir, _event(actor, "portal_lead_ingested", lead["portal_lead_id"],
                                    "pending", platform=lead["platform_id"],
                                    raw_ref=lead["raw_ref"]))
    return {"new": new, "seen": seen, "tasks": tasks, "skipped_large": count_skipped(data_dir)}


def replay(data_dir, audit_rows: list) -> dict:
    """hub.db 再構築時の反響復元。
    ① .eml 再パース取込（ファイル=正本・監査は再発行しない=チェーン汚染防止）
    ② 手動クイック登録（lead_quick_added イベント）を監査リプレイで復元。"""
    import json as _json
    from hub_core.operations import _full_row
    from hub_core.store import SqliteStore
    res = ingest_inbox(data_dir, _audit_write=False)
    st = SqliteStore(Path(data_dir) / "hub.db")
    manual = 0
    for r in audit_rows:
        if r.get("action") != "lead_quick_added":
            continue
        d = _json.loads(r.get("payload_json") or "{}")
        lead = d.get("lead") or {}
        lid = lead.get("portal_lead_id")
        if not lid or st.query("portal_leads", "portal_lead_id = ?", (lid,)):
            continue
        st.insert_row("portal_leads", _full_row("portal_leads", lead))
        task = _first_reply_task(lead)
        if not st.query("tasks", "task_id = ?", (task["task_id"],)):
            st.insert_row("tasks", _full_row("tasks", task))
        manual += 1
    res["manual"] = manual
    return res


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="hub_core.inbox", description="反響取込（.eml→台帳）")
    ap.add_argument("command", choices=["scan", "ingest"])
    ap.add_argument("--data-dir", default="out")
    args = ap.parse_args(argv)
    if args.command == "scan":
        rows = scan_inbox(args.data_dir)
        for r in rows:
            print(f"{r['portal_lead_id']}  {r['platform_id']:8s} {r['customer_name'] or '(名前なし)'}  {r['raw_ref']}")
        print(f"計 {len(rows)} 通")
        return 0
    res = ingest_inbox(args.data_dir)
    print(f"取込完了: 新規{res['new']}件・既存スキップ{res['seen']}件・一次返信タスク{res['tasks']}件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
