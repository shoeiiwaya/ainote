-- 004_ledger.sql — Wave1 M-ledger: タスクスヌーズ（CAN-09型）
-- 状態の正本は audit_log.jsonl のイベント（task_snoozed/task_unsnoozed）＝このテーブルは
-- 派生インデックス。vault.rebuild_business_tables が監査イベントのリプレイで再構築する。
CREATE TABLE IF NOT EXISTS task_snooze (
    task_id TEXT PRIMARY KEY,
    snooze_until TEXT NOT NULL,  -- YYYY-MM-DD。この日付**まで**一覧から隠す（当日に再浮上）
    reason TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_snooze_until ON task_snooze(snooze_until);
