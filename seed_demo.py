#!/usr/bin/env python3
"""seed_demo.py — デモ用サンプル書類(重説の全文＋マイソク販売図面)を documents に投入する。

目的: 重説/マイソク ページが「空」でなく**実物**を表示できるようにする。
冪等: 既に juusetsu/maisoku 書類があれば何もしない(版を無駄に増やさない)。
使い方: python3 seed_demo.py --data-dir <dir>   (既定: out)
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from hub_core import documents, maisoku

JUUSETSU_NAKANO = """# 重要事項説明書（35条）— 中野坂上 賃貸1LDK ＜ドラフト＞

> 本書は理OSが生成した**下書き**です。記名確定は宅地建物取引士が行います（未確定）。

## 1. 物件の表示
- 所在地：東京都中野区中央二丁目（住居表示：未確定／公開前に確定）
- 種類・構造：共同住宅・鉄筋コンクリート造（RC）地上8階建のうち5階部分
- 間取り／専有面積：1LDK／42.10㎡（壁芯）
- 取引の種別：賃貸借（媒介）

## 2. 登記記録に記録された事項
| 区分 | 内容 |
|---|---|
| 所有権 | 所有者：（登記簿のとおり・要確認） |
| 所有権以外の権利 | 抵当権の登記の有無：要確認（登記事項証明書で確認） |

## 3. 法令に基づく制限の概要
- 用途地域：商業地域
- 建ぺい率／容積率：80％／500％
- その他：日影規制・高度地区の有無は都市計画図で要確認

## 4. 私道に関する負担
- 私道負担：なし（前面道路は区道・幅員 約6m）※要確認

## 5. 飲用水・電気・ガス・排水の供給施設
- 飲用水：公営水道／電気：東京電力管内／ガス：都市ガス／排水：公共下水道

## 6. 未完成物件等の場合（完了時の形状・構造）
- 該当なし（既存建物）

## 7. 代金・交換差金以外に授受される金銭
- 敷金：1ヶ月／礼金：1ヶ月／仲介手数料：賃料1ヶ月＋消費税

## 8. 契約の解除に関する事項
- 借主からの解約：解約日の30日前までに書面で通知

## 9. 損害賠償額の予定・違約金に関する事項
- 短期解約違約金：契約から1年未満の解約時に賃料1ヶ月分（特約・要確認）

## 10. 支払金・預り金の保全措置／13. ローンあっせん
- 保全措置：講じない（少額のため）／ローンあっせん：なし

## 14. 契約不適合責任の履行に関する措置
- 賃貸のため売買の契約不適合責任は適用なし。設備の修繕区分は賃貸借契約書による。

## 15. 災害リスク（ハザード）
- 洪水・内水・高潮：各ハザードマップで要確認（PRSスコア連携：未配線）
- 土砂災害警戒区域：該当の有無を要確認

---
出典：ri-chousa 集約ドラフト ／ 本説明の最終責任は記名した宅地建物取引士に帰属します。
"""

# 帯(取扱業者欄=必要表示事項)はサンプル雛形。公開前に宅建士が確定する。
_COMPANY = {
    "company_name": "株式会社理（宅地建物取引業者）",
    "license": "国土交通大臣（1）第00000号（サンプル・公開前に確定）",
    "association": "（公社）全国宅地建物取引業保証協会加盟（サンプル）",
    "fair_trade_association": "（公社）首都圏不動産公正取引協議会加盟（サンプル）",
    "company_address": "東京都新宿区西新宿○-○-○",
    "company_tel": "03-0000-0000",
}
# 業者間流通(物確で確認する事項)。広告不可の無断掲載はレインズ規程・宅建業法違反。
_RYUTSU = {
    "reins_no": "（レインズ登録後に記載）",
    "ad_permission": "広告可（要連絡）",
    "obi_swap": "可（広告は要連絡）",
}

MAISOKU_OGIKUBO = {
    "property_type": "中古マンション", "property_name": "荻窪二丁目 中古マンション（サンプル）",
    "price": "5,480万円", "price_label": "価格",
    "address": "東京都杉並区荻窪二丁目（地番）", "access": "JR中央線「荻窪」駅 徒歩9分",
    "land_area": "—（敷地権）", "building_area": "68.40㎡（壁芯）", "balcony_area": "9.80㎡",
    "floor_plan": "3LDK", "built": "2009年3月（平成21年）", "floor": "7階／11階建",
    "direction": "南", "structure": "RC造", "youto": "第一種住居地域", "kenpei_yoseki": "60%／200%",
    "road": "—（区分所有）", "shidou": "なし", "genkyo": "空室（即入居可）", "hikiwatashi": "相談",
    "management_fee": "18,200円", "repair_fund": "12,600円", "parking": "空き要確認（月額別）",
    "setsubi": "オートロック・宅配ボックス・浴室乾燥・床暖房・システムキッチン・角部屋",
    "torihiki_taiyo": "専属専任媒介",
    "bikou": "2024年内装フルリフォーム済。ペット飼育は管理規約により可（細則あり）。",
    "yuko_kigen": "2026年7月31日", "published": "2026年6月24日", "next_update": "2026年7月8日",
    "staging_note": "掲載画像の一部はイメージです。家具・調度品は販売対象に含まれません。",
    **_RYUTSU, **_COMPANY,
}

MAISOKU_ATAMI = {
    "property_type": "中古戸建", "property_name": "熱海市桜木町 中古戸建（サンプル）",
    "price": "1,980万円", "price_label": "価格",
    "address": "静岡県熱海市桜木町1-2-3（サンプル表記）",
    "access": "JR東海道線「熱海」駅 バス12分「桜木町」停 徒歩4分（約300m）",
    "land_area": "182.45㎡（55.19坪・実測）", "building_area": "98.54㎡（延床・壁芯）",
    "balcony_area": "—", "floor_plan": "4K＋縁側", "built": "1981年6月（昭和56年）",
    "floor": "—（2階建）", "direction": "南東", "structure": "木造瓦葺2階建",
    "youto": "第一種低層住居専用地域", "kenpei_yoseki": "50%／100%",
    "road": "南東側 幅員4.2m 公道に接道（間口9.5m）", "shidou": "なし",
    "genkyo": "空家", "hikiwatashi": "相談",
    "management_fee": "—", "repair_fund": "—", "parking": "敷地内2台",
    "setsubi": "公営水道・本下水・都市ガス（プロパン併用・要確認）", "torihiki_taiyo": "媒介",
    "bikou": "新耐震基準（1981年6月以降確認申請）。雨樋・外壁に経年劣化あり、現況有姿での引渡し。リフォーム参考見積り取得済（1,200万円〜）。",
    "yuko_kigen": "2026年7月31日", "published": "2026年6月24日", "next_update": "2026年7月8日",
    "staging_note": "掲載画像の一部はイメージです。家具・調度品は販売対象に含まれません。",
    **_RYUTSU, **_COMPANY,
}


# 収益物件サンプル（データはすべてデモ用）
MAISOKU_SHUUEKI = {
    "property_type": "新築1棟アパート", "property_name": "サンプル収益アパート（川崎）",
    "owner_change": "新築・満室想定／投資用1棟もの",
    "title_copy": "駅徒歩10分・劣化対策等級3・利回り6.7%想定。",
    "sales_points": "・JR鶴見線「武蔵白石」駅 徒歩10分\n・新築（2025年9月完成予定）\n・全8戸 1Kワンルーム",
    "price": "1億5,310万円", "price_label": "販売価格", "yield_rate": "6.70%（満室想定）",
    "torihiki_taiyo": "媒介",
    "address": "神奈川県川崎市川崎区浅田2-13-12",
    "nearest_station": "JR鶴見線「武蔵白石」駅 徒歩10分",
    "route_lines": "JR鶴見線「武蔵白石」徒歩10分\nJR「川崎」バス便",
    "access": "JR鶴見線「武蔵白石」駅 徒歩10分",
    "surroundings": "コンビニ 徒歩3分／スーパー 徒歩8分／小学校 徒歩10分",
    "land_area": "189.69㎡（57.38坪・実測）", "building_area": "283.56㎡（85.78坪・延床）",
    "balcony_area": "—", "floor_plan": "1K × 8戸（各戸 21.11〜21.73㎡）",
    "floors_total": "2階建", "built": "2025年9月 完成予定", "chikunensu": "新築",
    "structure": "木造ストレート葺2階建", "total_units": "8戸", "direction": "南",
    "genkyo": "建築中", "hikiwatashi": "相談",
    "land_right": "所有権", "chimoku": "宅地", "toshi_keikaku": "市街化区域",
    "youto": "第二種住居地域（一部準住居地域含む）", "kenpei_yoseki": "80%／300%",
    "bouka": "準防火地域", "other_law": "第三種高度地区", "road": "南側公道 約4.5m", "shidou": "なし",
    "management_fee": "—", "repair_fund": "—", "other_fee": "水道加入金264万円（税込）含む",
    "parking": "敷地内（要確認）",
    "monthly_rent": "855,000円（満室想定）", "annual_income": "10,260,000円（満室想定）",
    "lease_period": "—（新築募集）",
    "bunjou_company": "—", "kanri_company": "（管理会社・要確認）", "sekou_company": "（施工会社・要確認）",
    "setsubi": "公営水道・LPガス・公共下水", "torihiki_taiyo": "媒介",
    "bikou": "路線価：21万円\n積算評価：8,673万円（うち土地3,983万円）\n水道加入金264万円（税込）含む",
    "yuko_kigen": "2026年7月31日", "published": "2026年6月24日", "next_update": "2026年7月8日",
    "staging_note": "掲載画像の一部はイメージです。",
    "staff": "（担当・未設定）", "fee": "3%（売買・税別）", "key_handling": "現地対応", "holiday": "水曜",
    **_RYUTSU, **_COMPANY,
}


def seed(data_dir: Path) -> list[str]:
    existing = {d.get("kind") for d in documents.list_documents(data_dir)}
    created = []
    if "juusetsu" not in existing:
        r = documents.save_version(data_dir, "JU-中野坂上1LDK", JUUSETSU_NAKANO,
                                   kind="juusetsu", fmt="md", author="理OS(seed)", sample=True)
        created.append(f'重説 {r["doc_id"]} v{r["version"]}')
    if "maisoku" not in existing:
        # 様式は正本=構造化フィールド(json)。表示時にテンプレ駆動でHTML化(フォーム編集可)。
        for did, prop, variant in (
            ("MS-川崎1棟アパート（収益）", MAISOKU_SHUUEKI, "shuueki"),
            ("MS-荻窪中古マンション", MAISOKU_OGIKUBO, "standard"),
            ("MS-熱海中古戸建", MAISOKU_ATAMI, "standard"),
        ):
            payload = dict(prop)
            payload["_variant"] = variant
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            r = documents.save_version(data_dir, did, body, kind="maisoku", fmt="txt",
                                       author="理OS(seed)", sample=True)
            created.append(f'マイソク {r["doc_id"]} v{r["version"]}({variant})')
    return created


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="out")
    args = ap.parse_args()
    dd = Path(args.data_dir).expanduser().resolve()
    made = seed(dd)
    if made:
        print("seeded:", "; ".join(made))
    else:
        print("既に重説/マイソク書類が存在するため seed をスキップしました。")
