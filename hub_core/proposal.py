"""M-proposal — 物件PR文・提案文の生成（Wave4 Tier2・AI臭ガード付き・¥0）。

物件データから売り出しPR文・お客様向け提案文の下書きを作る。
- **AI臭ガードが核**：—(em-dash)/セミコロン/コロン多用・定型免責・誇大表現を機械的に除去する
  （顧客に届く文面のAI臭は成約を遠ざける＝規律を機械化）。
- 生成は①テンプレベース（LLM不使用・¥0・既定）②BYO-LLM下書き（任意・generator注入）。
  どちらでも最後に必ず ai_smell_guard を通す。下書きは人間が校正して使う前提。
- 価格断定・精度バッジ・GTM語を入れない（公開規律）。stdlib のみ。
"""
from __future__ import annotations

import re

# AI臭・誇大の定型表現（顧客向け文面から除去/警告）。
_HYPE = ("必ず", "確実に", "業界No", "業界ナンバー", "最安", "最高値", "絶対に", "圧倒的",
         "唯一無二", "革命的", "精度", "AUC")
# 定型免責・AIっぽい前置き（除去）。
_BOILERPLATE = (
    "本物件は", "いかがでしょうか。", "ぜひこの機会に", "お見逃しなく",
    "この記事では", "まとめると", "結論から言うと",
)


def ai_smell_guard(text: str) -> str:
    """AI臭を機械的に落とす: 全角/半角ダッシュ多用・セミコロン・コロン多用・定型免責・誇大を除去。
    顧客に届く文面の規律を機械で担保する（[[feedback-avoid-em-dash-semicolon-colon]]）。"""
    t = text or ""
    # 文中連結ダッシュ（AI臭の典型＝em-dash族）を読点へ。対象=figure/en/em/two-em/three-em/
    # horizontal-bar/minus/vertical/罫線。**ハイフン(U+2D/2010/2011)は含めない**＝住所「2-3-1」や
    # 電話「03-1234-5678」を壊さないため。長音符U+30FC『ー』・全角チルダU+FF5E『～』も正当語で除外。
    t = re.sub(r"[\u2012\u2013\u2014\u2015\u2212\u2500\u2501\u2E3A\u2E3B\uFE31\uFE32\uFE58\uFF0D]+", "、", t)
    t = re.sub(r"\s-\s", "、", t)
    # セミコロンを句点へ
    t = t.replace(";", "。").replace("；", "。")
    # 文中コロンを除去（時刻 10:00 等 数字:数字 のみ保持）
    t = re.sub(r"(?<!\d)\s*[：:]\s*|\s*[：:]\s*(?!\d)", " ", t)
    # 定型免責の除去
    for bp in _BOILERPLATE:
        t = t.replace(bp, "")
    # 連続する句読点・空白を整える
    t = re.sub(r"[、。]{2,}", "。", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def hype_flags(text: str) -> list[str]:
    """誇大/断定表現を検出（除去でなく警告＝人間が判断）。"""
    return [w for w in _HYPE if w in (text or "")]


def _template_pr(prop: dict) -> str:
    """テンプレPR文（LLM不使用・¥0）。事実の列挙に徹する（誇大にしない）。"""
    parts = []
    name = prop.get("property_name") or prop.get("address") or "物件"
    parts.append(f"{name}のご紹介です。")
    facts = []
    if prop.get("station") and prop.get("walk_min"):
        facts.append(f"{prop['station']}駅から徒歩{prop['walk_min']}分")
    if prop.get("layout"):
        facts.append(f"間取りは{prop['layout']}")
    if prop.get("area"):
        facts.append(f"面積は{prop['area']}")
    if prop.get("built_year"):
        facts.append(f"{prop['built_year']}築")
    if prop.get("structure"):
        facts.append(f"{prop['structure']}造")
    if facts:
        parts.append("、".join(facts) + "です。")
    if prop.get("pet"):
        parts.append(f"ペットは{prop['pet']}です。")
    parts.append("詳しい条件やご内見のご希望はお気軽にお問い合わせください。")
    return "".join(parts)


def build_proposal(prop: dict, *, purpose: str = "pr", generator=None) -> dict:
    """PR/提案の下書きを作る。generator(BYO-LLM・callable(prompt)->str)があれば使い、無ければテンプレ。
    どちらも最後に ai_smell_guard を通す。誇大表現は flags で警告（除去せず人間判断）。"""
    if generator is not None:
        try:
            raw = generator(_template_pr(prop))
        except Exception:
            raw = _template_pr(prop)
    else:
        raw = _template_pr(prop)
    clean = ai_smell_guard(raw)
    return {"ok": True, "purpose": purpose, "text": clean, "hype_flags": hype_flags(clean),
            "generated_by": "byo-llm" if generator is not None else "template",
            "note": "下書きです。顧客に出す前に必ず校正してください。"}
