"""状態enum→日本語ラベルの単一正本（開発語彙を製品面に漏らさない=GATE-PV W1-PV deficit#1）。
参照ID（SUUMO-B-001等）は実IDなので翻訳しない。未知値は素通し（隠さない・正直）。"""
from __future__ import annotations

JP_ENUM = {
    # 在庫・反響・対応系
    "stock_unknown": "在庫未確認", "stop_requested": "停止依頼", "contact_unknown": "連絡先不明",
    "professional_review": "専門確認", "review_request_hold": "口コミ依頼の保留",
    "stealth_marketing_hold": "ステマ表記の確認", "publish_stock_hold": "公開前の在庫確認",
    "privacy_hold": "個人情報の確認", "ad_expression_hold": "広告表現の確認",
    "media_gate_hold": "媒体ゲート確認", "missing_documents": "書類不足",
    "missing_document": "書類不足", "unknown_tool_warning": "未知ツールの確認",
    # ゲート・状態
    "pending": "未対応", "cleared": "済", "ready": "送信可", "hold": "保留", "open": "対応中",
    "done": "完了", "sent": "送信済", "approved": "承認済", "rejected": "却下",
    "opt_in": "同意済", "opt_out": "同意なし", "unknown": "不明",
    # キュー・種別
    "Today": "今日", "Hold": "保留", "Approval": "承認", "Inbox": "新着",
    "Outbox": "送信待ち", "send": "送信", "money": "金銭",
    "privacy": "個人情報", "contract": "契約", "professional": "専門確認",
    "document_request": "資料請求", "viewing": "内見希望", "loan": "ローン相談",
    "price": "価格相談", "purchase_offer": "購入申出",
    # ポータルID
    "suumo": "SUUMO", "lifull_homes": "HOME'S", "athome_atbb": "athome",
    "rakumachi": "楽待", "kenbiya": "健美家",
    # 広告・公開ゲート
    "ad_expression_block": "広告表現の確認", "ad_permission_not_clear": "広告許諾の未確認",
    "ad_permission_unknown": "広告許諾が不明", "ad_review_pending": "広告審査待ち",
    "go_live": "公開", "go_live_not_approved": "公開未承認",
    # 書類・契約・審査
    "document_review_required": "書類確認が必要", "retention_overdue": "保管期限超過",
    "shinsa_gate_hold": "審査ゲートの保留", "contract_gate_hold": "契約ゲートの保留",
    "doc_version_conflict": "書類の版が競合", "electronic_consent_missing": "電子交付の同意なし",
    "takkenshi_assignment_missing": "宅建士の割当なし", "chousa_source_required": "原典資料が必要",
    "identity_not_verified": "本人確認未了", "kanri_gate_hold": "管理ゲートの保留",
    # 個人情報・ガバナンス
    "pii_access_role_insufficient": "個人情報の権限不足",
    "pii_external_send_signal": "個人情報の外部送信検知",
    "external_destination_unapproved": "未承認の外部送信先",
    "external_destination": "外部送信先", "deletion_requested": "削除依頼",
    "role_permission": "権限設定",
    # 金銭
    "amount_mismatch": "金額の不一致", "deposit_not_confirmed": "入金未確認",
    "money_gate_hold": "金銭ゲートの保留", "money_not_confirmed": "金銭未確認",
    "money_overdue": "支払期限超過", "refund_approval_required": "返金の承認が必要",
    # クレーム・運用
    "claim_emergency": "緊急クレーム", "claim_not_closed": "クレーム未解決",
    "seller_hope_not_fixed": "売主希望が未確定", "filename_nonstandard": "ファイル名が不統一",
    "training_overdue": "研修期限超過",
    # 掲載・精算・共有
    "inventory_not_available": "在庫なし", "listing_stop_or_review": "掲載停止・要確認",
    "publish_gate_hold": "公開ゲートの保留", "setup_gate_hold": "設定ゲートの保留",
    "privacy_risk": "個人情報リスク", "professional_review_required": "専門確認が必要",
    "report_price_review_required": "報告価格の確認", "shared_link_expired": "共有リンクの期限切れ",
    "shared_link_pii_review": "共有リンクの個人情報確認", "urgent_vendor_missing": "緊急対応業者が未設定",
    "settlement_basis_missing": "精算根拠なし", "settlement_gate_hold": "精算ゲートの保留",
    # 課金プロダクト（従量メータリング）
    "prs_assess": "PRSリスク評価", "prs_assess_full": "PRS全種別評価",
    "maisoku_generate": "マイソク生成", "juusetsu_finalize": "重説確定",
}


def jp(v) -> str:
    """enum値の日本語化（未知値は素通し）。"""
    s = str(v or "").strip()
    return JP_ENUM.get(s, s)
