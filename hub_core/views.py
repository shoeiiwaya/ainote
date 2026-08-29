"""読み取りビュー(Stage0 S0-2)。

serve.py の read_csv を置き換える DB-backed の query_page を提供する。
DB(英語キー)から読み、schema_cols.COLS で「日本語ラベルキー」へ再キー化して
(headers=日本語ラベル, rows=ラベルキー dict) を返す。これは従来の read_csv(先頭行=ラベルを
DictReader したもの)と同形なので、serve.py の下流(predicate/絞込/描画)は無改変で動く。

将来 S0-3 で viewer(認可)引数を受け、行/列フィルタを query_page 内に差し込む差込点になる。
"""
from __future__ import annotations

from pathlib import Path

from .auth import authorize_rows
from .schema_cols import COLS, table_of_source
from .store import SqliteStore


def query_page(data_dir, source, viewer=None):
    """source が DB-backed なら (labels, label_keyed_rows) を返す。非対応なら None。

    viewer(S0-3): 行スコープ + 個人情報列マスクの認可を適用する。serve は常に実 viewer を渡す
    (read には viewer 必須)。viewer=None は内部/テスト用途のみ(全件全列)。読み取り専用。
    """
    table = table_of_source(source)
    if table is None:
        return None
    db = Path(data_dir) / "hub.db"
    if not db.exists():
        return None
    cols = COLS.get(table)
    if not cols:
        return None
    store = SqliteStore(db)
    if table not in store.tables():
        return None
    eng_rows = store.query(table)
    labels = [label for label, _ in cols]
    label_rows = [{label: r.get(key, "") for label, key in cols} for r in eng_rows]
    label_rows = authorize_rows(label_rows, viewer)
    return labels, label_rows
