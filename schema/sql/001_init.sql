-- ri-hub 正本データモデル v1 (Stage0 S0-1)
-- ONLINE_ARCHITECTURE.md §2.1/§6#1。全列TEXT(現CSV正本と同形)。
-- 当面は full-replace(DELETE+INSERT in BEGIN IMMEDIATE)で同期するためPK uniqueには依存しない。
-- CHECK制約は「計算で保証される値」=監査ハッシュにのみ付与(入力依存値には付けない=破損回避)。
-- UNIQUE/UPSERTによる増分同期は後続(S0-2+)で導入。

CREATE TABLE IF NOT EXISTS portal_leads (
  portal_lead_id TEXT, platform_id TEXT, source_method TEXT, received_at TEXT,
  customer_name TEXT, customer_contact TEXT, property_ref TEXT, inquiry_type TEXT,
  consent_status TEXT, reply_gate TEXT, hold_reason TEXT, raw_ref TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT, queue TEXT, status TEXT, priority TEXT, title TEXT, platform_id TEXT,
  customer_name TEXT, property_id TEXT, assignee TEXT, gate TEXT, hold_reason TEXT,
  approval_role TEXT, portal_lead_id TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS hold_queue (
  hold_id TEXT, task_id TEXT, portal_lead_id TEXT, platform TEXT, customer_name TEXT,
  property_ref TEXT, hold_type TEXT, reason TEXT, clear_condition TEXT, owner_role TEXT, gate TEXT
);

CREATE TABLE IF NOT EXISTS approval_queue (
  approval_id TEXT, task_id TEXT, portal_lead_id TEXT, platform TEXT, customer_name TEXT,
  approval_role TEXT, reason TEXT, decision TEXT
);

CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT, customer_name TEXT, contact TEXT, line_user_id TEXT, status TEXT,
  gate_status TEXT, hold_type TEXT, source_ref TEXT, source_tool TEXT
);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT, customer_id TEXT, customer_name TEXT, property_id TEXT, property_name TEXT,
  deal_type TEXT, status TEXT, gate_status TEXT, hold_type TEXT, assignee TEXT,
  source_ref TEXT, source_tool TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT, source_tool TEXT, event_type TEXT, event_at TEXT, customer_id TEXT,
  case_id TEXT, property_id TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS billing_register (
  billing_id TEXT, case_id TEXT, customer_name TEXT, kind TEXT, amount TEXT,
  status TEXT, gate_status TEXT, created_at TEXT, source_ref TEXT, source_tool TEXT
);

CREATE TABLE IF NOT EXISTS contract_register (
  contract_id TEXT, case_id TEXT, property_name TEXT, contract_type TEXT, counterparty TEXT,
  start_date TEXT, end_date TEXT, auto_renewal TEXT, status TEXT,
  created_at TEXT, source_ref TEXT, source_tool TEXT
);

CREATE TABLE IF NOT EXISTS governance_register (
  register_id TEXT, category TEXT, name TEXT, status TEXT, detail TEXT, owner_role TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS claims_register (
  claim_id TEXT, received_at TEXT, case_id TEXT, property_id TEXT, customer_name TEXT, kind TEXT,
  severity TEXT, severity_reason TEXT, escalation_risk TEXT, neighbor TEXT, agency_contact TEXT,
  closure_condition TEXT, closure_state TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS recurrence_checklist (
  claim_id TEXT, kind TEXT, step TEXT, status TEXT, owner TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS filename_standardization (
  document_id TEXT, "current" TEXT, proposed TEXT, case_id TEXT, needs_rename TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS contract_version_register (
  document_id TEXT, case_id TEXT, kind TEXT, version TEXT, latest_version TEXT, is_latest TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS original_disposal_register (
  document_id TEXT, case_id TEXT, kind TEXT, original_state TEXT, action_pending TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS id_crosswalk (
  hub_key TEXT, source_tool TEXT, case_id TEXT, customer_id TEXT, property_id TEXT,
  hub_aliases TEXT, source_ref TEXT
);

CREATE TABLE IF NOT EXISTS approval_ledger (
  approval_id TEXT, task_id TEXT, gate TEXT, confirmer_role TEXT, confirmed_target TEXT,
  reason TEXT, decision TEXT, source_ref TEXT, recorded_at TEXT
);

-- 監査台帳: ハッシュチェーンの整合性をDB制約でも担保(sha256 hex=64桁・計算で保証=破損しない)
CREATE TABLE IF NOT EXISTS audit_log (
  event_id TEXT, actor TEXT, action TEXT, timestamp TEXT,
  prev_hash TEXT, entry_hash TEXT, payload_json TEXT,
  CHECK (length(entry_hash) = 64),
  CHECK (length(prev_hash) = 64)
);

-- S0-6: audit_log は append-only。UPDATE/DELETE をトリガで拒否する(defense-in-depth)。
-- 注意(正直化): SQLite のトリガは `DROP TRIGGER` で外せるため、これは権限境界ではなく
-- 「正規経路の事故/単純改ざんを防ぐ二次防御」。真の権限剥奪は Stage2 の自己ホスト
-- PostgreSQL 権限剥奪(§5.3)。Stage0 の一次的な改ざん検知は audit_log.jsonl の HMAC チェーン
-- (別保管鍵・hub_core/audit.py)が担う。同期は full-replace から除外し append 経路のみで INSERT。
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only (Stage0 S0-6): UPDATE禁止');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only (Stage0 S0-6): DELETE禁止');
END;

CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue);
CREATE INDEX IF NOT EXISTS idx_tasks_gate ON tasks(gate);
CREATE INDEX IF NOT EXISTS idx_hold_gate ON hold_queue(gate);
CREATE INDEX IF NOT EXISTS idx_cases_customer ON cases(customer_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
