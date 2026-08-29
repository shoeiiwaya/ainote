"""SqliteStore — ri-hub 正本データストア(Stage0 S0-1)。

ONLINE_ARCHITECTURE.md §2.1/§2.3/§6#1。
- stdlib `sqlite3` のみ(追加依存なし・第三者SaaS/RDS不使用・全ローカル)。
- WAL + busy_timeout で「書込中でも read は一貫断面」を担保。
- 書込は BEGIN IMMEDIATE の単一トランザクション(全テーブル full-replace = 原子的)。
  WAL により reader は古い断面 or 新しい断面のどちらかを見る(部分行を見ない)。

将来(S0-2+)で増分UPSERT/行レベル認可フィルタ(viewer)へ拡張する土台。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "schema" / "sql" / "001_init.sql"


def _norm(v):
    """CSV正本と同形のTEXT値へ正規化。None→空文字、list/dict→JSON文字列。"""
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        import json
        return json.dumps(v, ensure_ascii=False)
    return v


class SqliteStore:
    """ローカルSQLite正本。1ファイル=自己ホスト。"""

    # S0-6: append-only テーブル。full-replace(DELETE+INSERT)から除外し、
    # 重複キーを除いた INSERT のみ行う(DB側はトリガで UPDATE/DELETE を物理拒否)。
    APPEND_ONLY = {"audit_log": "entry_hash"}

    def __init__(self, db_path, schema_sql: Path | None = None):
        self.db_path = str(db_path)
        self.schema_sql = Path(schema_sql) if schema_sql else SCHEMA_SQL
        self._init_schema()

    def _connect(self):
        # isolation_level=None: autocommit。トランザクションは明示的に BEGIN/COMMIT 制御する。
        con = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_schema(self):
        # スキーマdir内の *.sql を番号順に全適用(001_init→002_…のマイグレーション方式)。
        # 各ファイルは CREATE TABLE IF NOT EXISTS で冪等。
        sql_dir = self.schema_sql.parent
        files = sorted(sql_dir.glob("*.sql")) or [self.schema_sql]
        con = self._connect()
        try:
            for f in files:
                con.executescript(f.read_text(encoding="utf-8"))
            # 既存DBだけに不足する担当列を、入力由来の識別子を使わない固定migrationで補う。
            case_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(cases)")}
            if "assignee" not in case_columns:
                con.execute("ALTER TABLE cases ADD COLUMN assignee TEXT")
        finally:
            con.close()

    @staticmethod
    def _qident(name: str) -> str:
        # 識別子を二重引用符でクォート(current 等の予約語的列名に耐える)
        return '"' + name.replace('"', '""') + '"'

    def _replace_table(self, con, table, cols, rows):
        con.execute(f"DELETE FROM {self._qident(table)}")
        if not rows:
            return
        collist = ",".join(self._qident(c) for c in cols)
        placeholders = ",".join("?" for _ in cols)
        con.executemany(
            f"INSERT INTO {self._qident(table)} ({collist}) VALUES ({placeholders})",
            [[_norm(r.get(c)) for c in cols] for r in rows],
        )

    def _append_rows(self, con, table, cols, rows, key):
        """append-only テーブルへ key 未存在の行のみ INSERT(過去行を消さない=DELETE しない)。"""
        if not rows:
            return
        existing = {r[0] for r in con.execute(
            f"SELECT {self._qident(key)} FROM {self._qident(table)}")}
        new = [r for r in rows if _norm(r.get(key)) not in existing]
        if not new:
            return
        collist = ",".join(self._qident(c) for c in cols)
        placeholders = ",".join("?" for _ in cols)
        con.executemany(
            f"INSERT INTO {self._qident(table)} ({collist}) VALUES ({placeholders})",
            [[_norm(r.get(c)) for c in cols] for r in new],
        )

    def sync(self, table_data: dict):
        """table_data: {table_name: (cols:list[str], rows:list[dict])}。
        全テーブルを単一 BEGIN IMMEDIATE トランザクションで同期(原子的)。
        通常テーブルは full-replace、APPEND_ONLY テーブルは追記のみ(S0-6)。"""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                for table, (cols, rows) in table_data.items():
                    if table in self.APPEND_ONLY:
                        self._append_rows(con, table, cols, rows, self.APPEND_ONLY[table])
                    else:
                        self._replace_table(con, table, cols, rows)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def update_row(self, table, key_col, key_val, changes: dict) -> int:
        """単一行の指定列を更新する。操作OS(状態遷移)の基本プリミティブ。更新行数を返す。
        append-only テーブル(audit_log 等)は不可(改ざん防止・DBトリガでも物理拒否)。"""
        if table in self.APPEND_ONLY:
            raise ValueError(f"{table} は append-only。update 不可(監査完全性)")
        if not changes:
            return 0
        set_clause = ", ".join(f"{self._qident(c)} = ?" for c in changes)
        params = [_norm(v) for v in changes.values()] + [_norm(key_val)]
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    f"UPDATE {self._qident(table)} SET {set_clause} "
                    f"WHERE {self._qident(key_col)} = ?", params)
                con.execute("COMMIT")
                return cur.rowcount
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def insert_row(self, table, row: dict) -> None:
        """単一行を追加する(新規案件/タスク作成等)。append-only テーブルは sync 経由で。"""
        if table in self.APPEND_ONLY:
            raise ValueError(f"{table} は append-only。sync(追記)経由で")
        cols = list(row.keys())
        collist = ",".join(self._qident(c) for c in cols)
        placeholders = ",".join("?" for _ in cols)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    f"INSERT INTO {self._qident(table)} ({collist}) VALUES ({placeholders})",
                    [_norm(row[c]) for c in cols])
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def delete_row(self, table, key_col, key_val) -> int:
        """単一行を削除する(スヌーズ解除等の可逆状態の撤回)。append-only テーブルは不可。"""
        if table in self.APPEND_ONLY:
            raise ValueError(f"{table} は append-only。削除不可")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    f"DELETE FROM {self._qident(table)} WHERE {self._qident(key_col)} = ?",
                    (key_val,))
                con.execute("COMMIT")
                return cur.rowcount
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def query(self, table, where: str | None = None, params=(), limit: int | None = None):
        con = self._connect()
        try:
            q = f"SELECT * FROM {self._qident(table)}"
            if where:
                q += f" WHERE {where}"
            if limit is not None:
                q += f" LIMIT {int(limit)}"
            return [dict(r) for r in con.execute(q, params).fetchall()]
        finally:
            con.close()

    def count(self, table) -> int:
        con = self._connect()
        try:
            return con.execute(f"SELECT COUNT(*) FROM {self._qident(table)}").fetchone()[0]
        finally:
            con.close()

    def tables(self):
        con = self._connect()
        try:
            return [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        finally:
            con.close()
