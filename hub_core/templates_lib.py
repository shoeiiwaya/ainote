"""M-templates — 文例ライブラリ（Wave4 Tier2・再利用テンプレ・¥0）。

追客・内見案内・申込お礼・重説補足など、業務で繰り返し使う文面を分類して持つ。
message_draft から呼び出して差し込む。AI臭を避けるため定型は簡潔・人間の校正前提。
- 変数は {name}/{property}/{date} 等の単純置換のみ（LLM不使用・決定的・stdlib）。
- 顧客向け文面に価格断定・精度バッジ・誇大表現を入れない（公開規律）。
"""
from __future__ import annotations

TEMPLATES = {
    "追客": [
        ("初回フォロー", "{name}様　お問い合わせありがとうございます。ご希望の条件を伺えれば、"
         "条件に合うお物件をご提案します。ご都合のよいご連絡方法をお知らせください。"),
        ("新着案内", "{name}様　その後ご状況いかがでしょうか。ご希望に近い新着のお物件が出ましたので"
         "ご案内できればと思います。"),
    ],
    "内見": [
        ("内見のご案内", "{name}様　内見のご予約ありがとうございます。当日は{property}の前で"
         "お待ちしております。お気をつけてお越しください。"),
        ("内見後フォロー", "{name}様　本日はご内見ありがとうございました。ご不明点やご検討状況が"
         "ございましたら、お気軽にお知らせください。"),
    ],
    "申込": [
        ("申込お礼", "{name}様　このたびはお申込をいただきありがとうございます。"
         "審査の進捗が分かり次第、あらためてご連絡いたします。"),
    ],
    "重説補足": [
        ("重説日程調整", "{name}様　ご契約に先立ち、重要事項のご説明のお時間を頂戴したく存じます。"
         "ご都合のよい日時をいくつかお知らせいただけますでしょうか。"),
    ],
}


def categories() -> list[str]:
    return list(TEMPLATES.keys())


def list_templates(category: str | None = None) -> list[dict]:
    """テンプレ一覧。category 指定でその分類のみ。"""
    out = []
    for cat, items in TEMPLATES.items():
        if category and cat != category:
            continue
        for i, (title, body) in enumerate(items):
            out.append({"category": cat, "index": i, "title": title, "body": body})
    return out


def render_template(category: str, index: int, variables: dict | None = None) -> str:
    """テンプレを変数で埋めて返す。未知の変数は原文のまま（KeyErrorにしない）。"""
    items = TEMPLATES.get(category)
    if not items or not (0 <= int(index) < len(items)):
        raise ValueError(f"未知のテンプレ: {category}[{index}]")
    body = items[int(index)][1]
    v = variables or {}
    for k, val in v.items():
        body = body.replace("{" + str(k) + "}", str(val or ""))
    return body
