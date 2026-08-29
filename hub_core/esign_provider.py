"""M-esign provider factory — BYO電子契約プロバイダの差替え口（Wave4 G6）。

esign.py の Provider 抽象に対し、**実プロバイダ（クラウドサイン/GMOサイン/DocuSign等）を env 設定で
差し込む**ファクトリ。未設定なら MockProvider（実送信しない）。実接続・キー投入は人間ゲート。
- 接続状態はサーバ側の配備事実（env）から決める＝リクエスト自己申告で実送信に化けない（screening と同型）。
- 実プロバイダの HTTP 実装は chat_llm と同じ BYO 経路（キーは同期フォルダ外）。ここでは接続可否と
  アダプタの選択のみ。実際の送信呼出は各 Provider 実装（本ファイルは Mock と接続判定のスケルトン）。
"""
from __future__ import annotations

import os

from hub_core.esign import MockProvider, Provider

# 対応予定の実プロバイダ（BYO）。実装は接続時に差し込む（キー/エンドポイントは env）。
KNOWN_PROVIDERS = ("cloudsign", "gmosign", "docusign", "freesign")


def esign_connected() -> bool:
    """実電子契約プロバイダが接続されているか＝サーバ側の配備事実（env）。既定False。
    RI_HUB_ESIGN_PROVIDER と対応キーが設定されて初めてTrue（人間ゲート）。"""
    prov = (os.environ.get("RI_HUB_ESIGN_PROVIDER") or "").strip().lower()
    key = (os.environ.get("RI_HUB_ESIGN_KEY") or "").strip()
    return bool(prov in KNOWN_PROVIDERS and key)


class ConfiguredProvider(Provider):
    """env設定済みの実プロバイダ枠。実HTTP送信は各社SDK/API実装を差し込む（tool-scout）。
    ここでは connected=True を表明するのみ＝実装未差込のうちは送信で NotImplementedError。"""
    def __init__(self, name: str):
        self.name = name
        self.connected = True

    def send(self, envelope: dict) -> dict:
        # 実プロバイダのAPI呼出をここに実装（BYOキー・PAdES/認定TSA）。未実装のうちは明示エラー。
        raise NotImplementedError(
            f"実プロバイダ({self.name})のsend未実装。API実装を差し込んでください（実接続は人間ゲート）。")


def build_esign_provider(data_dir=None) -> Provider:
    """設定に応じて Provider を返す。未接続なら MockProvider（実送信しない）。"""
    if esign_connected():
        return ConfiguredProvider((os.environ.get("RI_HUB_ESIGN_PROVIDER") or "").strip().lower())
    return MockProvider()
