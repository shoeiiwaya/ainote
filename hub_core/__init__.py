"""ri-hub core: 正本データストア(オンライン対応の土台)。

ONLINE_ARCHITECTURE.md の hub-core 層。Stage0 S0-1 では SqliteStore を導入し、
hub.py が CSV/jsonl を併産しつつ SQLite 正本へ同期する(E2E非破壊)。
"""
