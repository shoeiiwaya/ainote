"""あいのて Vault — フォルダが正本・アプリは眺め方。

- 物件/顧客/案件のフォルダとファイルが正本。取込＝フォルダに置くこと（アップロードUI廃止）。
- hub.db は再構築可能な派生インデックス。`reindex` でゼロから再生成できる。
- 書類は documents.py（1書類1dir/1版1file・分岐5A）が既に正本＝本モジュールは
  **素材/調査/許諾ファイル**（PDF/画像等の非構造ファイル）の走査・分類・索引と、
  `確定.sig`（content_hash 束縛）の検証を担う。
- stdlib のみ。監視は watch デーモンでなく走査（scan）＝ serve 起動時/操作時/CLI で呼ぶ
  （watchdog 等の依存追加は tool-scout ゲート対象のため v0 はポーリング走査）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JST = timezone(timedelta(hours=9))

# Vault 直下の第一級フォルダ（存在するものだけ走査。旧 out/ 直下構成とも共存＝後方互換）
TOP_DIRS = ("物件", "顧客", "案件", "金銭", "テンプレ")
# 物件配下のカテゴリ（親フォルダ名で分類）
CATEGORY_DIRS = ("素材", "調査", "許諾", "書類")
# 索引対象拡張子（非構造ファイル。書類正本 documents/ は documents.py 管轄なので除外）
ASSET_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".xlsx", ".docx", ".csv", ".json", ".md", ".txt"}
# 種別分類（ファイル名の語彙→kind。第一防衛層＝後段でBYO-LLM分類に置換可能な純関数）
_KIND_RULES = (
    ("間取り図", ("間取", "madori", "floorplan")),
    ("案内図", ("案内図", "地図", "map")),
    ("写真", ("写真", "photo", "外観", "内装", "img_", "dsc")),
    ("登記", ("登記", "全部事項", "touki")),
    ("公図", ("公図",)),
    ("重説資料", ("重説", "重要事項")),
    ("マイソク", ("マイソク", "販売図面", "maisoku")),
    ("ハザード", ("ハザード", "hazard", "浸水")),
    ("許諾", ("承諾", "許諾", "広告可", "帯替え")),
)

SIG_NAME = "確定.sig"


def _now() -> str:
    return datetime.now(_JST).replace(microsecond=0).isoformat()


def nfc(name: str) -> str:
    """日本語ファイル名の正規化キー。macOS/iCloud は NFD で保存し得る（W7）。
    索引キーは常に NFC。ファイル自体は改名しない（非破壊・同期衝突を作らない）。"""
    return unicodedata.normalize("NFC", name)


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def classify(filename: str, parent: str = "") -> str:
    """ファイル名（＋親フォルダ名）から素材種別を推定。未知は拡張子ベース。"""
    base = nfc(filename).lower()
    par = nfc(parent).lower()
    for kind, words in _KIND_RULES:
        for w in words:
            if w in base or w in par:
                return kind
    ext = Path(base).suffix
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".heic"):
        return "写真"
    if ext == ".pdf":
        return "資料"
    return "その他"


def scan(data_dir) -> list[dict]:
    """Vault を走査して素材/調査/許諾ファイルの索引行を返す（正本は触らない・読み取りのみ）。

    対象: <data_dir>/物件/<物件名>/(素材|調査|許諾)/**、および物件フォルダ直下のファイル。
    documents/（書類正本）と .riho/・隠しファイルは除外。"""
    root = Path(data_dir)
    rows: list[dict] = []
    bukken_root = root / "物件"
    if not bukken_root.exists():
        return rows
    for prop_dir in sorted(p for p in bukken_root.iterdir() if p.is_dir()):
        prop = nfc(prop_dir.name)
        for f in sorted(prop_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.name.startswith(".") or f.name == SIG_NAME:
                continue
            rel_parent = f.parent.relative_to(prop_dir).parts
            category = nfc(rel_parent[0]) if rel_parent else ""
            if category == "書類":
                # 書類正本は documents.py 管轄（版管理・確定）。索引の二重化を避ける。
                continue
            if f.suffix.lower() not in ASSET_EXTS:
                continue
            rows.append({
                "asset_key": nfc(str(f.relative_to(root))),
                "property": prop,
                "category": category or "（直下）",
                "kind": classify(f.name, category),
                "filename": nfc(f.name),
                "sha256": _sha256_file(f),
                "bytes": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, _JST).replace(microsecond=0).isoformat(),
                "indexed": _now(),
            })
    return rows


VAULT_ASSET_COLS = ["asset_key", "property", "category", "kind", "filename",
                    "sha256", "bytes", "mtime", "indexed"]


def sync_assets(data_dir) -> int:
    """scan 結果を hub.db の vault_assets へ full-replace 同期（派生インデックス）。件数を返す。"""
    from hub_core.store import SqliteStore
    rows = scan(data_dir)
    store = SqliteStore(Path(data_dir) / "hub.db")
    store.sync({"vault_assets": (VAULT_ASSET_COLS, rows)})
    return len(rows)


# 業務台帳: CSV正本ファイル → hub.db テーブル（ファイル名=テーブル名・ヘッダ=列名）
BUSINESS_TABLES = (
    "portal_leads", "customers", "cases", "events", "tasks", "hold_queue",
    "approval_queue", "governance_register", "claims_register", "recurrence_checklist",
    "filename_standardization", "contract_version_register", "original_disposal_register",
    "id_crosswalk", "approval_ledger", "contact_log", "customer_journey", "billing_register", "contract_register",
)
AUDIT_DB_COLS = ["event_id", "actor", "action", "timestamp", "prev_hash", "entry_hash", "payload_json"]


def _read_csv_rows(path: Path):
    import csv
    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        return headers, [dict(r) for r in reader]


def rebuild_business_tables(data_dir) -> dict:
    """業務台帳を **ファイル正本（out/*.csv + audit_log.jsonl）** から hub.db へ全再構築する。
    CSVヘッダ（日本語ラベル）→ DB列（英語キー）の対訳は schema_cols.COLS（単一正本）を使う。
    hub.py の write_outputs→sync と同じ full-replace 機構（audit は append-only）を、
    パイプラインを回さずに正本ファイルから直接行う＝「DBは派生インデックス」の実行体。"""
    import json as _json
    from hub_core.store import SqliteStore
    from hub_core.schema_cols import COLS
    root = Path(data_dir)
    table_data = {}
    counts = {}
    for tbl in BUSINESS_TABLES:
        headers, rows = _read_csv_rows(root / f"{tbl}.csv")
        if headers:
            mapping = dict(COLS.get(tbl, []))  # 日本語ラベル -> 英語キー
            cols = [mapping.get(h, h) for h in headers]
            rows = [{mapping.get(h, h): r.get(h, "") for h in headers} for r in rows]
            table_data[tbl] = (cols, rows)
            counts[tbl] = len(rows)
    audit_rows = []
    ap = root / "audit_log.jsonl"
    if ap.exists():
        for line in ap.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = _json.loads(line)
            audit_rows.append({
                "event_id": d.get("event_id", ""), "actor": d.get("actor", ""),
                "action": d.get("action", ""), "timestamp": d.get("timestamp", ""),
                "prev_hash": d.get("prev_hash", ""), "entry_hash": d.get("entry_hash", ""),
                "payload_json": _json.dumps(d, ensure_ascii=False),
            })
    if audit_rows:
        table_data["audit_log"] = (AUDIT_DB_COLS, audit_rows)
        counts["audit_log"] = len(audit_rows)
    if table_data:
        SqliteStore(root / "hub.db").sync(table_data)
    counts["task_snooze"] = _replay_snooze(root, audit_rows)
    # 反響の復元（.eml=ファイル正本の再取込＋手動登録の監査リプレイ）
    from hub_core import inbox as _inbox
    inbox_res = _inbox.replay(root, audit_rows)
    counts["inbox_replayed"] = inbox_res.get("new", 0) + inbox_res.get("manual", 0)
    # 横断全文検索索引（派生・G1）を再構築
    try:
        from hub_core import search as _search
        counts["search_index"] = _search.build_search_index(root)
    except Exception:
        pass
    return counts


def _replay_snooze(root: Path, audit_rows: list) -> int:
    """スヌーズ状態を監査イベント（task_snoozed/task_unsnoozed）のリプレイで再構築する。
    task_snooze テーブルは派生インデックス＝正本は audit_log.jsonl（イベントソーシング）。"""
    import json as _json
    from hub_core.store import SqliteStore
    state = {}
    for r in audit_rows:
        act = r.get("action", "")
        if act not in ("task_snoozed", "task_unsnoozed"):
            continue
        d = _json.loads(r.get("payload_json") or "{}")
        tid = d.get("target", "")
        if not tid:
            continue
        if act == "task_snoozed":
            state[tid] = {"task_id": tid, "snooze_until": d.get("until", ""),
                          "reason": d.get("reason", ""), "actor": d.get("actor", ""),
                          "created_at": d.get("timestamp", "")}
        else:
            state.pop(tid, None)
    st = SqliteStore(root / "hub.db")
    cols = ["task_id", "snooze_until", "reason", "actor", "created_at"]
    st.sync({"task_snooze": (cols, list(state.values()))})
    return len(state)


def reindex(data_dir) -> dict:
    """hub.db（派生インデックス）をファイル正本からゼロ再構築する（W1）。
    ①業務台帳= out/*.csv + audit_log.jsonl → full-replace（rebuild_business_tables）
    ②vault_assets= 物件フォルダ走査 → full-replace
    ③documents 索引= documents.list_documents が常にファイル走査で答えるためDB非依存（件数報告のみ）。"""
    from hub_core import documents
    biz = rebuild_business_tables(data_dir)
    n_assets = sync_assets(data_dir)
    n_docs = len(documents.list_documents(data_dir))
    return {"vault_assets": n_assets, "documents": n_docs,
            "business_tables": biz, "reindexed": _now()}


# ---- 確定.sig（content_hash 束縛の改竄検知・RIH-14 の型をファイルへ） ----

def write_sig(target: Path, *, signer: str = "", extra: dict | None = None) -> Path:
    """対象ファイルの sha256 に束縛した 確定.sig を同じフォルダへ発行する。
    HMAC 鍵署名は chat_bridge.finalize（RIH-14）が担う＝本関数は hash 束縛のみ（第一層）。"""
    target = Path(target)
    sig = {
        "target": nfc(target.name),
        "content_sha256": _sha256_file(target),
        "signer": signer,
        "signed": _now(),
    }
    if extra:
        sig.update(extra)
    p = target.parent / SIG_NAME
    p.write_text(json.dumps(sig, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def verify_sigs(data_dir) -> list[dict]:
    """Vault 内の全 確定.sig を検証。改竄（hash不一致）・対象欠落を報告（W3）。
    返り値: [{sig, target, status: ok|tampered|missing}]"""
    root = Path(data_dir)
    results = []
    for sig_path in sorted(root.rglob(SIG_NAME)):
        try:
            sig = json.loads(sig_path.read_text(encoding="utf-8"))
        except Exception:
            results.append({"sig": str(sig_path), "target": "", "status": "unreadable"})
            continue
        target = sig_path.parent / sig.get("target", "")
        if not target.is_file():
            results.append({"sig": str(sig_path), "target": sig.get("target", ""), "status": "missing"})
            continue
        actual = _sha256_file(target)
        ok = actual == sig.get("content_sha256", "")
        results.append({"sig": str(sig_path), "target": sig.get("target", ""),
                        "status": "ok" if ok else "tampered"})
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hub_core.vault", description="あいのて Vault（フォルダ正本の走査・索引・検証）")
    ap.add_argument("command", choices=["scan", "reindex", "verify-sigs"])
    ap.add_argument("--data-dir", default="out")
    args = ap.parse_args(argv)
    if args.command == "scan":
        rows = scan(args.data_dir)
        for r in rows:
            print(f"{r['property']} / {r['category']} / {r['kind']} / {r['filename']} ({r['bytes']}B)")
        print(f"計 {len(rows)} 件")
        return 0
    if args.command == "reindex":
        res = reindex(args.data_dir)
        biz = res.get("business_tables", {})
        print(f"reindex 完了: vault_assets={res['vault_assets']} documents={res['documents']} "
              f"業務台帳={sum(biz.values())}行/{len(biz)}表")
        return 0
    if args.command == "verify-sigs":
        results = verify_sigs(args.data_dir)
        bad = [r for r in results if r["status"] != "ok"]
        for r in results:
            print(f"[{r['status']}] {r['sig']} -> {r['target']}")
        print(f"計 {len(results)} 件・異常 {len(bad)} 件")
        return 1 if bad else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
