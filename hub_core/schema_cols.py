"""ラベル↔キー正本(Stage0 S0-2)。

hub.py write_outputs が CSV に書く (日本語ラベル, 英語キー) と、SQLite正本の英語キー列を
橋渡しする単一の対応表。serve.py が DB から読んだ英語キー行を「日本語ラベルキー」へ再キー化し、
従来 CSV(先頭行=日本語ラベル)を DictReader した時と同形の (headers, rows) を返すために使う。

注意: これは hub.py の *_cols と同内容(正本)。drift は test で検出する
(test_views.py が hub 実行後の out/*.csv ヘッダと COLS を突合)。
"""
from __future__ import annotations

COLS = {
    "portal_leads": [
        ("反響ID", "portal_lead_id"), ("ポータル", "platform_id"), ("取込方法", "source_method"),
        ("受信日時", "received_at"), ("顧客名", "customer_name"), ("連絡先", "customer_contact"),
        ("物件参照", "property_ref"), ("問い合わせ種別", "inquiry_type"), ("同意状態", "consent_status"),
        ("返信ゲート", "reply_gate"), ("保留理由", "hold_reason"), ("原文参照", "raw_ref"),
    ],
    "tasks": [
        ("タスクID", "task_id"), ("キュー", "queue"), ("状態", "status"), ("優先度", "priority"),
        ("タイトル", "title"), ("ポータル", "platform_id"), ("顧客名", "customer_name"),
        ("物件参照", "property_id"), ("担当", "assignee"), ("ゲート", "gate"),
        ("保留理由", "hold_reason"), ("承認役割", "approval_role"), ("元反響ID", "portal_lead_id"),
        ("作成日時", "created_at"),
    ],
    "hold_queue": [
        ("保留ID", "hold_id"), ("タスクID", "task_id"), ("反響ID", "portal_lead_id"), ("ポータル", "platform"),
        ("顧客名", "customer_name"), ("物件参照", "property_ref"), ("保留種別", "hold_type"),
        ("理由", "reason"), ("解除条件", "clear_condition"), ("解除役割", "owner_role"), ("ゲート", "gate"),
    ],
    "approval_queue": [
        ("承認ID", "approval_id"), ("タスクID", "task_id"), ("反響ID", "portal_lead_id"), ("ポータル", "platform"),
        ("顧客名", "customer_name"), ("承認役割", "approval_role"), ("理由", "reason"), ("判断", "decision"),
    ],
    "customers": [
        ("顧客ID", "customer_id"), ("顧客名", "customer_name"), ("連絡先", "contact"),
        ("LINEユーザーID", "line_user_id"), ("状態", "status"), ("ゲート状態", "gate_status"),
        ("保留種別", "hold_type"), ("元データ", "source_ref"), ("元ツール", "source_tool"),
    ],
    "cases": [
        ("案件ID", "case_id"), ("顧客ID", "customer_id"), ("顧客名", "customer_name"),
        ("物件ID", "property_id"), ("物件名", "property_name"), ("取引種別", "deal_type"),
        ("状態", "status"), ("ゲート状態", "gate_status"), ("保留種別", "hold_type"),
        ("担当", "assignee"), ("元データ", "source_ref"), ("元ツール", "source_tool"),
    ],
    "events": [
        ("イベントID", "event_id"), ("元ツール", "source_tool"), ("イベント種別", "event_type"),
        ("イベント日時", "event_at"), ("顧客ID", "customer_id"), ("案件ID", "case_id"),
        ("物件ID", "property_id"), ("元データ", "source_ref"),
    ],
    "contract_register": [
        ("契約ID", "contract_id"), ("案件ID", "case_id"), ("物件名", "property_name"),
        ("契約種別", "contract_type"), ("相手方", "counterparty"), ("開始日", "start_date"),
        ("終了日", "end_date"), ("自動更新", "auto_renewal"), ("状態", "status"),
        ("作成日時", "created_at"), ("元データ", "source_ref"), ("元ツール", "source_tool"),
    ],
    "billing_register": [
        ("請求ID", "billing_id"), ("案件ID", "case_id"), ("顧客名", "customer_name"),
        ("種別", "kind"), ("金額", "amount"), ("状態", "status"), ("ゲート状態", "gate_status"),
        ("作成日時", "created_at"), ("元データ", "source_ref"), ("元ツール", "source_tool"),
    ],
    "governance_register": [
        ("台帳ID", "register_id"), ("カテゴリ", "category"), ("名称", "name"),
        ("状態", "status"), ("詳細", "detail"), ("解除/管理役割", "owner_role"), ("元データ", "source_ref"),
    ],
    "claims_register": [
        ("受付ID", "claim_id"), ("受付日時", "received_at"), ("案件ID", "case_id"),
        ("物件ID", "property_id"), ("顧客名", "customer_name"), ("種別", "kind"),
        ("緊急度", "severity"), ("緊急度理由", "severity_reason"), ("クレーム化リスク", "escalation_risk"),
        ("近隣トラブル", "neighbor"), ("行政連絡", "agency_contact"),
        ("終結条件", "closure_condition"), ("終結状態", "closure_state"), ("元データ", "source_ref"),
    ],
    "recurrence_checklist": [
        ("受付ID", "claim_id"), ("種別", "kind"), ("手順", "step"),
        ("状態", "status"), ("担当", "owner"), ("元データ", "source_ref"),
    ],
    "filename_standardization": [
        ("書類ID", "document_id"), ("現ファイル名", "current"), ("標準提案名", "proposed"),
        ("案件ID", "case_id"), ("リネーム要否", "needs_rename"), ("元データ", "source_ref"),
    ],
    "contract_version_register": [
        ("書類ID", "document_id"), ("案件ID", "case_id"), ("書類種別", "kind"),
        ("版", "version"), ("最新版", "latest_version"), ("最新判定", "is_latest"), ("元データ", "source_ref"),
    ],
    "original_disposal_register": [
        ("書類ID", "document_id"), ("案件ID", "case_id"), ("書類種別", "kind"),
        ("原本状態", "original_state"), ("処理要否", "action_pending"), ("元データ", "source_ref"),
    ],
    "id_crosswalk": [
        ("Hubキー", "hub_key"), ("元ツール", "source_tool"), ("案件ID", "case_id"),
        ("顧客ID", "customer_id"), ("物件ID", "property_id"),
        ("名寄せ別名", "hub_aliases"), ("元データ", "source_ref"),
    ],
    "approval_ledger": [
        ("承認ID", "approval_id"), ("タスクID", "task_id"), ("ゲート", "gate"),
        ("確認役割", "confirmer_role"), ("確認対象", "confirmed_target"), ("理由", "reason"),
        ("判断", "decision"), ("元データ", "source_ref"), ("記録日時", "recorded_at"),
    ],
    # --- Phase3 業務OS化(タイムライン/CRM/マッチング) ---
    "customer_attributes": [
        ("属性ID", "attr_id"), ("顧客ID", "customer_id"), ("項目", "field_key"), ("値", "field_value"),
        ("カテゴリ", "category"), ("個人情報区分", "pii_flag"), ("同意範囲", "consent_scope"), ("更新日時", "updated_at"),
    ],
    "customer_journey": [
        ("ジャーニーID", "journey_id"), ("案件ID", "case_id"), ("トラック", "deal_track"), ("ステージ", "stage"),
        ("着手日時", "entered_at"), ("期限", "due_at"), ("完了日時", "completed_at"), ("最終接触", "last_contact_at"),
    ],
    "contact_log": [
        ("接触ID", "contact_id"), ("顧客ID", "customer_id"), ("案件ID", "case_id"), ("チャネル", "channel"),
        ("発生日時", "occurred_at"), ("要約", "summary"), ("反応", "reaction"), ("担当", "actor"),
    ],
    "properties": [
        ("物件ID", "property_id"), ("所在地", "address"), ("取引種別", "deal_type"), ("賃料/価格", "rent_or_price"),
        ("間取り", "layout"), ("面積", "area"), ("築年", "built_year"), ("構造", "structure"),
        ("最寄駅", "station"), ("徒歩分", "walk_min"), ("ペット", "pet"), ("ソース", "source"),
        ("AD", "ad_fee"), ("緯度", "lat"), ("経度", "lon"), ("リスクスコア", "risk_scores"), ("状態", "status"),
    ],
    "match_factors": [
        ("因子ID", "factor_id"), ("顧客ID", "customer_id"), ("因子", "factor_key"), ("希望値", "want_value"),
        ("優先度", "priority"), ("重み", "weight"),
    ],
    "document_requirements": [
        ("要件ID", "req_id"), ("案件ID", "case_id"), ("書類種別", "doc_kind"), ("必須", "required"),
        ("充足", "present"), ("ゲート", "gate"), ("元書類", "source_doc_id"),
    ],
    # --- 005 失注の一級市民化(CRMギャップp1) ---
    "lost_records": [
        ("失注ID", "lost_id"), ("案件ID", "case_id"), ("顧客ID", "customer_id"), ("取引種別", "deal_type"),
        ("失注時ステージ", "lost_stage"), ("失注理由", "lost_reason"), ("補足", "note"),
        ("失注日時", "lost_at"), ("記録者", "actor"),
    ],
}


def table_of_source(source: str):
    """serve.py の source 文字列 → DB テーブル名。DB非対応(jsonl等)は None。"""
    if source == "tasks":
        return "tasks"
    if source.startswith("csv:"):
        table = source[len("csv:"):]
        if table.endswith(".csv"):
            table = table[:-len(".csv")]
        return table if table in COLS else None
    return None
