"""M-search — 横断全文検索（Wave4 G1・SQLite FTS5・完全ローカル・追加依存なし）。

Vault資産・業務台帳・抽出値・書類ドラフトを1つのFTS5索引に束ね、案件/顧客/物件/書類を
横断検索する。索引は派生（hub.db 内の search_index 仮想テーブル）＝reindexで再構築。
- 日本語対応=FTS5 trigram トークナイザ（3文字以上で中間一致）。2文字以下は LIKE フォールバック。
- 索引するのは氏名/物件/状態等の**業務検索キー**のみ。連絡先・LINE ID・数値等の直値PIIは索引しない。
  /search は内部ログイン必須。**公開検索へ流用する場合は customer_name も必ず除外**すること。
- 全ローカル・ネットワーク非接触・stdlib sqlite3 のみ。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# 索引対象の業務台帳と、そのうち検索に載せる列（PII直値=連絡先/LINE ID等は載せない）。
_TABLE_FIELDS = {
    "customers": ("customer_id", ["customer_name", "status", "source_tool"]),
    "cases": ("case_id", ["property_name", "customer_name", "deal_type", "status"]),
    "portal_leads": ("portal_lead_id", ["customer_name", "property_ref", "inquiry_type"]),
    "tasks": ("task_id", ["title", "assignee", "queue", "priority"]),
    "hold_queue": ("hold_id", ["customer_name", "property_ref", "reason"]),
    "approval_queue": ("approval_id", ["customer_name", "reason", "approval_role"]),
}
# kind → 詳細リンクの組み立て
_LINK = {
    "物件": lambda ref: "/case?id=" + ref,
    "案件": lambda ref: "/case?id=" + ref,
    "顧客": lambda ref: "/customers",
    "反響": lambda ref: "/leads",
    "タスク": lambda ref: "/today",
    "保留": lambda ref: "/hold",
    "承認": lambda ref: "/approval",
    "資産": lambda ref: "/properties",
    "抽出": lambda ref: "/case?id=" + ref,
}


def _con(data_dir):
    return sqlite3.connect(Path(data_dir) / "hub.db", timeout=30.0)


def build_search_index(data_dir) -> int:
    """FTS5索引を再構築（派生）。**原子的**＝CREATE IF NOT EXISTS ＋ 単一トランザクション内の
    DELETE+INSERT。並行アクセス下でも索引が空になる瞬間を作らない（DROP+CREATEの非原子は不可）。"""
    con = _con(data_dir)
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
                    "kind, ref, title, body, tokenize='trigram')")
        rows = []
        # 業務台帳
        existing = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for tbl, (idcol, fields) in _TABLE_FIELDS.items():
            if tbl not in existing:
                continue
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
            sel = [c for c in ([idcol] + fields) if c in cols]
            if idcol not in sel:
                continue
            kind = {"customers": "顧客", "cases": "案件", "portal_leads": "反響",
                    "tasks": "タスク", "hold_queue": "保留", "approval_queue": "承認"}[tbl]
            for row in con.execute(f"SELECT {','.join(sel)} FROM {tbl}"):
                d = dict(zip(sel, row))
                ref = str(d.get(idcol) or "")
                title = str(d.get(fields[0]) or ref)
                body = " ".join(str(d.get(f) or "") for f in fields)
                rows.append((kind, ref, title, body))
        # Vault資産（ファイル名・物件・種別）
        if "vault_assets" in existing:
            for r in con.execute("SELECT property, category, kind, filename FROM vault_assets"):
                prop, cat, k, fn = (str(x or "") for x in r)
                rows.append(("資産", prop, fn, f"{prop} {cat} {k} {fn}"))
        # 原子的入替: 単一トランザクションで全消去→全挿入（読み手は旧全集合 or 新全集合のみ・空を見ない）
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM search_index")
            con.executemany("INSERT INTO search_index(kind, ref, title, body) VALUES (?,?,?,?)", rows)
            # 抽出値（extract.json・Vault走査）
            n_ext = _index_extractions(data_dir, con)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return len(rows) + n_ext
    finally:
        con.close()


def _index_extractions(data_dir, con) -> int:
    from hub_core import extract as _ex
    try:
        recs = _ex.load_extractions(data_dir)
    except Exception:
        return 0
    n = 0
    for rec in recs:
        prop = rec.get("property") or ""
        body = " ".join(f'{f.get("key","")} {f.get("value","")}' for f in rec.get("fields", []))
        if body.strip():
            con.execute("INSERT INTO search_index(kind, ref, title, body) VALUES (?,?,?,?)",
                        ("抽出", prop, Path(rec.get("source", "")).name, prop + " " + body))
            n += 1
    return n


def search(data_dir, query: str, *, limit: int = 40) -> list[dict]:
    """横断検索。3文字以上=FTS5 trigram（ランク順）、2文字以下=LIKE。索引が無ければ空。"""
    q = (query or "").strip()
    if not q:
        return []
    con = _con(data_dir)
    try:
        has = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='search_index'").fetchone()
        if not has:
            return []
        out = []
        if len(q) >= 3:
            # FTS5 は特殊記号でMATCH構文が壊れるため、ダブルクオートで句として渡す
            phrase = '"' + q.replace('"', '""') + '"'
            try:
                cur = con.execute(
                    "SELECT kind, ref, title, snippet(search_index,3,'[',']','…',8) "
                    "FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT ?",
                    (phrase, limit))
                out = list(cur)
            except sqlite3.OperationalError:
                out = []
        if not out:
            like = f"%{q}%"
            cur = con.execute(
                "SELECT kind, ref, title, substr(body,1,80) FROM search_index "
                "WHERE title LIKE ? OR body LIKE ? LIMIT ?", (like, like, limit))
            out = list(cur)
        results = []
        for kind, ref, title, snip in out:
            link = _LINK.get(kind, lambda r: "/")(ref) if ref else "/"
            results.append({"kind": kind, "ref": ref, "title": title,
                            "snippet": snip, "link": link})
        return results
    finally:
        con.close()
