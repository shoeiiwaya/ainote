-- 003_vault.sql — Vault 派生インデックス（Wave 0 / M-vault）
-- 正本は物件フォルダのファイル群（VAULT_STRUCTURE.md）。本テーブルは reindex で全再構築可能。
CREATE TABLE IF NOT EXISTS vault_assets (
  asset_key TEXT PRIMARY KEY,   -- data_dir 相対パス（NFC正規化）
  property  TEXT NOT NULL,      -- 物件フォルダ名（NFC）
  category  TEXT NOT NULL,      -- 素材/調査/許諾/（直下）
  kind      TEXT NOT NULL,      -- 間取り図/写真/案内図/登記/許諾/資料/その他…
  filename  TEXT NOT NULL,
  sha256    TEXT NOT NULL,
  bytes     INTEGER NOT NULL,
  mtime     TEXT NOT NULL,
  indexed   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_assets_property ON vault_assets(property);
CREATE INDEX IF NOT EXISTS idx_vault_assets_kind ON vault_assets(kind);
