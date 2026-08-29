"""あいのて自身が利用できる外部送信経路の開示（sovereignty）。

あいのては既定で外部送信経路を持たない。だが一部の機能（PRS災害リスク＝住所を
国土地理院/PRSへ送信、クラウドLLM＝物件データをAI提供者へ送信）は外部送信を伴う。
このモジュールは「今どの経路でデータが外に出得るか」を設定とenvから算出し、
UIバッジで**監査可能に開示**する。主権を「言葉」でなく「事実」にするための単一の真実源。

OS、バックアップソフト、利用者が選んだ保存先による同期は検知できない。画面では必ず
「あいのて自身からの送信」と範囲を限定し、端末外へ絶対に出ないとは表示しない。

原則: 状態はリクエストの自己申告でなく、サーバ配備の事実（環境変数）から決める。
"""
from __future__ import annotations

import os


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def geocoder_mode() -> str:
    """住所→座標の経路。'local'=端末内で完結 / 'gsi'=国土地理院へ住所を送信 / 'off'=無効。
    既定は 'gsi'（従来動作）だが、GEOCODER=local でローカル化、PRS_ALLOW_NETWORK 未許可なら
    住所の外部送信を止める（主権優先）。"""
    m = str(os.environ.get("GEOCODER") or "").strip().lower()
    if m in ("local", "off", "gsi"):
        return m
    # 明示指定なし: ネットワーク許可があれば従来のGSI、無ければ off（住所を外に出さない）
    return "gsi" if network_allowed() else "off"


def network_allowed() -> bool:
    """外部送信の明示許可。PRS_ALLOW_NETWORK=1 、または PRS/リスクAPIキーが設定されている場合。
    （キー設定＝利用者が外部PRSを使う明示意思とみなす。）"""
    if _truthy(os.environ.get("PRS_ALLOW_NETWORK")):
        return True
    return bool((os.environ.get("RISK_API_KEY") or os.environ.get("PRS_API_KEY") or "").strip())


def llm_mode(data_dir=None) -> str:
    """AIの経路。'local'=Ollama等ローカル / 'cloud'=外部API(BYO) / 'none'=未接続。
    llm_config.json ではなく env の実配備から読む（LLM_MODE / ANTHROPIC_API_KEY 等）。"""
    if data_dir is not None:
        try:
            from . import chat_llm
            provider = chat_llm.build_provider(data_dir)
            if provider is not None:
                return "cloud" if getattr(provider, "is_external", True) else "local"
        except Exception:
            pass
    mode = str(os.environ.get("RI_HUB_LLM_PROVIDER") or os.environ.get("LLM_MODE") or "").strip().lower()
    if mode in ("local", "ollama"):
        return "local"
    if mode in ("api", "cloud", "subscription", "anthropic"):
        return "cloud"
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return "cloud"
    if (os.environ.get("OLLAMA_HOST") or "").strip():
        return "local"
    return "none"


def data_flows(data_dir=None) -> list[dict]:
    """今、外部にデータが出る経路の一覧。UIバッジ・監査用。
    各要素: {channel, external(bool), sends, to, note}。"""
    flows = []
    gm = geocoder_mode()
    if gm == "gsi":
        flows.append({"channel": "災害リスク（ジオコード）", "external": True,
                      "sends": "物件の住所", "to": "国土地理院（オンライン）",
                      "note": "GEOCODER=local でローカル化、PRS_ALLOW_NETWORK 未設定で送信停止"})
    if network_allowed():
        flows.append({"channel": "災害リスク（PRS評価）", "external": True,
                      "sends": "物件の座標", "to": "PRS災害リスクAPI",
                      "note": "RISK_API_KEY を外すと未接続（送信なし）"})
    lm = llm_mode(data_dir)
    if lm == "cloud":
        flows.append({"channel": "AI（クラウド）", "external": True,
                      "sends": "会話・物件データの一部", "to": "外部AI提供者（BYO）",
                      "note": "ローカルLLM（Ollama）に切り替えるとAI処理は端末内で完結"})
    if data_dir is not None:
        from . import connections
        checks = (
            (
                "メール送信",
                lambda: connections.smtp_configured(data_dir),
                {"sends": "宛先・件名・本文", "to": "設定したSMTPサーバ",
                 "note": "人間が送信を確定した時だけ送信"},
            ),
            (
                "FAX送信",
                lambda: bool(connections.load_fax_config(data_dir).get("endpoint")
                             and connections.fax_token(data_dir)),
                {"sends": "宛先・送信書類", "to": "設定したFAXサービス",
                 "note": "人間が送信を確定した時だけ送信"},
            ),
            (
                "LINE連携",
                connections.harness_configured,
                {"sends": "送受信メッセージ・顧客識別子", "to": "設定したLINE連携",
                 "note": "設定した連携先との送受信"},
            ),
        )
        for channel, configured, details in checks:
            try:
                if configured():
                    flows.append({"channel": channel, "external": True, **details})
            except Exception:
                # 設定を確認できない時に「送信なし」と断定しない。秘密や例外文字列は画面へ出さない。
                flows.append({"channel": f"{channel}（設定確認不能）", "external": True,
                              "sends": "不明", "to": "設定を確認してください",
                              "note": "接続設定を読み取れないため、外部送信なしとは判定できません"})
    return flows


def status(data_dir=None) -> dict:
    """主権バッジ用の要約。{sovereign(bool), label, tone, flows}。
    sovereign=True＝あいのて自身に有効な外部送信経路が無い。"""
    flows = data_flows(data_dir)
    if not flows:
        return {"sovereign": True, "tone": "ok",
                "label": "あいのてからの外部送信なし（保存先の同期設定は別）",
                "flows": []}
    channels = "・".join(f["channel"] for f in flows)
    return {"sovereign": False, "tone": "warn",
            "label": f"外部送信あり: {channels}",
            "flows": flows}
