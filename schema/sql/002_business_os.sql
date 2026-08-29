-- ri-hub 業務OS化: タイムライン / CRM / 物件マッチング の正本テーブル(v2)
-- 設計前提は実装側注釈/テストで管理し、全列は既存001と同形の全replace同期に乗る設計。
-- 既存 cases/customers を太らせず、属性・履歴・物件・マッチ・書類充足を分離テーブルに持つ。

-- §4 顧客属性(CRM)。1顧客×複数属性(field_key)。PII区分とconsentで個情法対応。
CREATE TABLE IF NOT EXISTS customer_attributes (
  attr_id TEXT, customer_id TEXT, field_key TEXT, field_value TEXT,
  category TEXT, pii_flag TEXT, consent_scope TEXT, updated_at TEXT
);

-- §3 顧客ジャーニー(ステージ履歴・滞留検知の駆動)。case単位・deal_track(buyer/seller)で売買は2レーン。
CREATE TABLE IF NOT EXISTS customer_journey (
  journey_id TEXT, case_id TEXT, deal_track TEXT, stage TEXT,
  entered_at TEXT, due_at TEXT, completed_at TEXT, last_contact_at TEXT
);

-- 接触履歴(架電/メール/LINE/来店/内見)。last_contact_at の源泉=滞留検知(a)系。
CREATE TABLE IF NOT EXISTS contact_log (
  contact_id TEXT, customer_id TEXT, case_id TEXT, channel TEXT,
  occurred_at TEXT, summary TEXT, reaction TEXT, actor TEXT
);

-- §5 物件マスタ(現状 property_id 文字列のみ→実体化)。risk_scores=PRSキャッシュ。
CREATE TABLE IF NOT EXISTS properties (
  property_id TEXT, address TEXT, deal_type TEXT, rent_or_price TEXT, layout TEXT,
  area TEXT, built_year TEXT, structure TEXT, station TEXT, walk_min TEXT,
  pet TEXT, source TEXT, ad_fee TEXT, lat TEXT, lon TEXT, risk_scores TEXT, status TEXT
);

-- §5 顧客×希望条件と優先順位ウェイト(マッチングの重み=must/nice)。
CREATE TABLE IF NOT EXISTS match_factors (
  factor_id TEXT, customer_id TEXT, factor_key TEXT, want_value TEXT,
  priority TEXT, weight TEXT
);

-- §6 ステージ×書類の充足チェック(必須未充足を朱表示=滞留検知(b)系)。
CREATE TABLE IF NOT EXISTS document_requirements (
  req_id TEXT, case_id TEXT, doc_kind TEXT, required TEXT, present TEXT,
  gate TEXT, source_doc_id TEXT
);
