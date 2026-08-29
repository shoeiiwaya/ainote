-- 005: 失注の一級市民化(CRMギャップp1・2026-07-30)。
-- 002と同じ設計思想: cases を太らせず、失注の事実・理由を分離テーブルに持つ。
-- 失注理由の蓄積が仕入れ・掲載改善の源泉(VtigerCRM設計案記事×理OS実査ギャップ)。

CREATE TABLE IF NOT EXISTS lost_records (
  lost_id TEXT, case_id TEXT, customer_id TEXT, deal_type TEXT,
  lost_stage TEXT, lost_reason TEXT, note TEXT, lost_at TEXT, actor TEXT
);
CREATE INDEX IF NOT EXISTS idx_lost_case ON lost_records(case_id);
