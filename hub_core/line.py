"""hub_core/line.py — LINEハーネス（送信=outbox＋送信ゲート＋Mock / 受信は line_webhook に委譲）。

FAX（fax.py）と同じ型でLINEを扱う＝LINE⇄FAX両対応ハーネス。受信の署名検証・イベント正規化は
既存の line_webhook.py（X-Line-Signature 検証・event正規化）が担う。本モジュールは**送信側**を足す:
- LineProvider 抽象＋MockLineProvider（reply/push を実送信しない・未接続時の既定）。
- new_line_message（outbox積み）＋transition（queued→gated[人間確認]→sent）。
- **実発火の機械ゲート**: provider.connected（実チャネル）は allow_real_send 明示が必須（esign/fax と同一規律）。
- 実LINEチャネルトークン/シークレットの接続・実送信は人間ゲート（無料枠だが公式アカウント作成＝承認）。
- stdlib only・ネットワーク非接触（送信はProvider実装側）。捏造しない。
"""
from __future__ import annotations

import datetime
import re

JST = datetime.timezone(datetime.timedelta(hours=9))


def _now() -> str:
    return datetime.datetime.now(JST).replace(microsecond=0).isoformat()


class LineError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# 受信は line_webhook に委譲（重複実装しない）。
def verify_webhook(channel_secret: str, body: bytes, signature: str) -> bool:
    from hub_core import line_webhook
    return line_webhook.verify_signature(channel_secret, body, signature)


def normalize_inbound(payload) -> list:
    """LINEのWebhookイベント → ri-hub の接触素材（差出user/種別/テキスト/時刻）に正規化。捏造しない。"""
    from hub_core import line_webhook
    events = line_webhook.parse_events(payload)
    return line_webhook.normalize_events(events)


# ============================ 送信（outbound）＝ fax/esign 型 ============================

class LineProvider:
    """LINE送信プロバイダの抽象。実装は send（reply/push）と connected（実接続か）を提供。"""
    name = "abstract"
    connected = False

    def send(self, message: dict) -> dict:
        raise NotImplementedError


class MockLineProvider(LineProvider):
    """ローカルモック（実LINE送信しない）。チャネルトークン未設定時の既定。"""
    name = "mock"
    connected = False

    def send(self, message: dict) -> dict:
        return {"provider": self.name, "external_id": "MOCK-LINE-" + str(message.get("msg_id", "")),
                "sent": False, "outcome": "mock",
                "note": "モック: 実際のLINE送信は行っていません（未接続）。"}


class HarnessLineProvider(LineProvider):
    """line-harness-oss 経由でLINEを実送信。connected=True＝transition の allow_real_send ゲートを発火させる
    （実チャネル送信は明示承認が必須）。実送信の HTTP は connections.harness_send（許可モジュール）に委譲し、
    env(URL/API_KEY)未設定なら失敗＝あいのてはMockのまま。to_user は harness の friend UUID（生 line_user_id でない）。"""
    name = "harness"
    connected = True

    def send(self, message: dict) -> dict:
        from hub_core import connections
        idempotency_key = str(message.get("idempotency_key") or "")
        if message.get("message_type") == "flex":
            import json
            flex = message.get("flex_content") or {}
            # 余分な報告用キー（truncated 等）は wire に載せない＝Flex コンテナだけを送る。
            container = {k: v for k, v in flex.items()
                         if k in ("type", "contents", "size", "header", "hero",
                                  "body", "footer", "styles", "direction")}
            alt = message.get("alt_text") or message.get("text") or "物件のご紹介"
            kwargs = {"msgtype": "flex", "alt_text": alt}
            if idempotency_key:
                kwargs["idempotency_key"] = idempotency_key
            r = connections.harness_send(
                message.get("to_user", ""), json.dumps(container, ensure_ascii=False),
                **kwargs)
        else:   # text（従来経路・呼び出し形は不変）
            if idempotency_key:
                r = connections.harness_send(
                    message.get("to_user", ""), message.get("text", ""),
                    idempotency_key=idempotency_key)
            else:
                r = connections.harness_send(
                    message.get("to_user", ""), message.get("text", ""))
        return {"provider": self.name, "external_id": r.get("message_id", ""),
                "sent": bool(r.get("ok")), "outcome": r.get("outcome", "unknown"),
                "note": r.get("detail", "")}


# ---- harness → あいのて の着信（outgoing webhook・LINEネイティブとは別の署名/形） ----
def verify_harness_webhook(secret: str, body: bytes, signature: str) -> bool:
    """harness の outgoing webhook 署名検証（X-Webhook-Signature = hex(HMAC-SHA256(secret, body))・定数時間比較）。
    LINE ネイティブの X-Line-Signature(base64) とはヘッダ名・エンコードが別。secret 未設定は False（拒否）。"""
    import hashlib
    import hmac
    if not secret or not signature or not isinstance(signature, str):
        return False
    body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def normalize_harness_inbound(payload) -> list:
    """harness outgoing webhook payload → line_receive 用 events 形（friendId→line_user_id）。捏造しない。"""
    import json
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload if not isinstance(payload, (bytes, bytearray))
                                 else payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
    if not isinstance(payload, dict):
        return []
    ev = str(payload.get("event") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    edata = data.get("eventData") if isinstance(data.get("eventData"), dict) else {}
    delivery_id = str(payload.get("delivery_id") or payload.get("id")
                      or data.get("delivery_id") or data.get("messageId")
                      or edata.get("id") or "")
    if not delivery_id:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), default=str)
        import hashlib
        delivery_id = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    kind = {"message_received": "message", "friend_add": "follow",
            "friend_remove": "unfollow"}.get(ev, ev or "event")
    return [{"line_user_id": str(data.get("friendId") or ""),
             "reply_token": str(data.get("replyToken") or ""), "kind": kind,
             "text": str(edata.get("text") or edata.get("displayName") or ""),
             "source": "harness", "delivery_source": "line-harness",
             "delivery_id": delivery_id, "event": ev}]


# 非text の messageType → 会話台帳に載せる faithful なプレースホルダ（本文JSONを垂れ流さない）。
# 送信側の Flex カード等はこれで「Flexカード」と表示する（altText 相当・捏造しない＝型の事実だけ）。
_MTYPE_LABEL = {"image": "画像", "video": "動画", "audio": "音声", "file": "ファイル",
                "sticker": "スタンプ", "location": "位置情報", "flex": "Flexカード",
                "template": "テンプレート", "imagemap": "画像マップ"}


def normalize_feed_items(items) -> list:
    """pull型取込: harness の**増分feed** item（GET /api/messages/feed・friend情報 inline）→ line_receive 用 events 形。
    **送受信 両方向**を拾い、各 event に direction（incoming/outgoing）を付す（会話台帳の完全化）。
    - text は content をそのまま。**非text（画像/スタンプ/Flex等）は本文を捏造せず型プレースホルダ**（例「[Flexカード]」）。
      これで送信側 Flex は altText 相当の短い表記で載り、JSON全文を会話に垂れ流さない。
    - dedupe 用に各 event へ harness_msg_id（メッセージUUID）を持たせる。id 無しはスキップ（安全に重複除去できない）。
    - 空 text（本文なしのtext）はスキップ（載せる中身が無い）。
    reply 宛先の一貫性のため line_user_id は friend の UUID（送信 to_user と同じ・生 lineUserId でない）。"""
    if not isinstance(items, list):
        return []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        direction = str(m.get("direction") or "incoming")
        if direction not in ("incoming", "outgoing"):
            continue   # 未知の direction は取り込まない（捏造しない）
        mid = str(m.get("id") or "")
        if not mid:
            continue   # UUID無し＝安全にdedupeできないので取り込まない
        mtype = str(m.get("messageType") or "")
        content = str(m.get("content") or "").strip()
        if mtype in ("", "text"):
            text = content
            if not text:
                continue   # 本文なしのtextは載せる中身が無い
        else:
            text = "[" + _MTYPE_LABEL.get(mtype, mtype or "メッセージ") + "]"   # 型プレースホルダ
        fid = str(m.get("friendId") or "")
        out.append({"line_user_id": fid or str(m.get("friendLineUserId") or ""),
                    "harness_friend_id": fid, "harness_msg_id": mid, "direction": direction,
                    "delivery_id": mid, "delivery_source": "line-harness-pull",
                    "message_type": mtype or "text",
                    "line_display_name": str(m.get("friendDisplayName") or ""),
                    "kind": "message", "text": text, "source": "harness-pull",
                    "created_at": str(m.get("createdAt") or "")})
    return out


# ---- 内見希望の検出（着信テキスト→内見予約ドラフトの補助・捏造しない） ----
_VIEWING_WORDS = ("内見", "内覧", "見学", "下見", "お部屋を見", "物件を見")


def viewing_intent(text: str) -> dict:
    """着信メッセージが内見希望かを判定し、**明確な**日時があれば候補として返す。
    返り値 {"is_viewing": bool, "candidate_at": "YYYY-MM-DDTHH:MM"|""}。
    捏造しない＝曖昧・相対表現（明日/来週）や年欠落は候補を出さず担当入力に委ねる。日時は担当が必ず確認する前提。"""
    t = str(text or "")
    is_v = any(w in t for w in _VIEWING_WORDS)
    return {"is_viewing": is_v, "candidate_at": _extract_datetime(t) if is_v else ""}


def _extract_datetime(t: str) -> str:
    """ごく明確な日時パターンのみ抽出（YYYY[-/年]MM[-/月]DD ＋ 任意で HH[:時]MM）。
    年が無い/範囲外は空（年跨ぎ・誤読を避ける＝捏造しない）。相対表現は解釈しない。"""
    import re
    m = re.search(r"(20\d{2})[/\-年](\d{1,2})[/\-月](\d{1,2})", t)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    hh, mm = 0, 0
    tm = re.search(r"(\d{1,2})[:時](\d{1,2})?", t)
    if tm:
        h2 = int(tm.group(1))
        m2 = int(tm.group(2)) if tm.group(2) else 0
        if 0 <= h2 <= 23 and 0 <= m2 <= 59:
            hh, mm = h2, m2
    try:
        return datetime.datetime(y, mo, d, hh, mm).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


# ---- 案内可否の問い合わせ検出（他社ポータルURL＋案内可否語彙・捏造しない） ----
# 顧客が SUUMO/athome/HOMES 等の物件URLを貼って「案内できますか」と聞く反響パターン。担当は元付へ物確する。
# URLは本文から**そのまま**抽出し、物件名・条件はテキストから推測して構造化しない（＝URLと生テキストだけ運ぶ）。
_INQUIRY_WORDS = ("案内可能", "案内できま", "案内して", "ご案内", "案内お願い",
                  "紹介可能", "紹介できま", "紹介して", "ご紹介",
                  "空いてま", "空いていま", "空室ですか", "空きはあ", "空きあり",
                  "まだ空いて", "まだあります", "まだ募集", "取扱できま", "取り扱えま",
                  "この物件", "内見できま")
# hostname → ポータル名（末尾一致・sp/www サブドメインも拾う）。列挙外は「その他」（捏造しない）。
_PORTAL_HOSTS = (("suumo.jp", "SUUMO"), ("athome.co.jp", "athome"),
                 ("homes.co.jp", "HOME'S"), ("chintai.net", "CHINTAI"))
# http/https の URL（空白・山括弧・引用符・全角括弧/空白で終端）。短縮URLもそのまま拾う。
_URL_RE = re.compile(r"https?://[^\s<>\"'（）()【】「」『』　]+")


def _portal_of(url: str) -> str:
    """URL の hostname からポータル名を推定（列挙外は「その他」・捏造しない）。"""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    for dom, label in _PORTAL_HOSTS:
        if host == dom or host.endswith("." + dom):
            return label
    return "その他"


def _extract_urls(text: str) -> list:
    """本文から http/https URL を最大3件抽出（重複除去・末尾の句読点は落とす）。各 URL にポータル名を付す。
    URL は本文の**部分文字列そのまま**（正規化・補完しない＝捏造しない）。"""
    out, seen = [], set()
    for m in _URL_RE.finditer(str(text or "")):
        u = m.group(0).rstrip("。、,.）)】」』>　")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "portal": _portal_of(u)})
        if len(out) >= 3:
            break
    return out


def inquiry_intent(text: str) -> dict:
    """着信が「案内可否の問い合わせ（物確対象）」かを判定。他社ポータル等の URL（最大3件）を抽出し、
    URL が無くても案内可否の語彙（案内できますか/空いてますか/紹介できますか等）があれば is_inquiry=True。
    返り値 {"is_inquiry": bool, "urls": [{"url": str, "portal": str}], "has_url": bool, "keyword": bool}。
    物件名・条件はテキストから推測して構造化しない（捏造しない＝URLと生テキストだけを運ぶ）。"""
    t = str(text or "")
    urls = _extract_urls(t)
    kw = any(w in t for w in _INQUIRY_WORDS)
    return {"is_inquiry": bool(urls) or kw, "urls": urls, "has_url": bool(urls), "keyword": kw}


# ---- 希望条件ヒアリングの検出（LIFF条件フォーム→トーク送信の構造化テキストをパース） ----
# LIFFの条件オートヒアリング（賃貸/購入→エリア・予算・間取り/種別・時期・こだわり）は
# 「物件さがしの希望です。」ヘッダ＋ラベル行として、顧客本人のメッセージでトークに届く
# （sendMessages・実証済みの LINE→harness→pull 経路に相乗り）。ここではヘッダを持つ着信を
# 検出し、**既知ラベルの行だけ**を辞書化する。ラベルに無い行（お名前/電話/自由文の続き等）は
# 無視し、値の推測補完はしない（＝捏造しない）。種別=賃貸/購入、物件種別=購入のマンション/戸建/土地。
_HEARING_HEADER = "物件さがしの希望です"
_HEARING_LABELS = ("種別", "物件種別", "エリア", "賃料上限", "予算上限",
                   "間取り", "入居時期", "時期", "こだわり", "受付番号")
_HEARING_LINE_RE = re.compile(r"^([^:：]+)[:：]\s*(.*)$")


def hearing_intent(text: str) -> dict:
    """着信が「希望条件ヒアリング」かを判定し、既知ラベル行だけを辞書化する。
    返り値 {"is_hearing": bool, "mode": "賃貸"|"購入"|"", "receipt": str, "fields": {label: value}}。
    - is_hearing はヘッダ「物件さがしの希望です」を含む着信のみ True（強アンカー・誤検出回避）。
    - fields は _HEARING_LABELS にあるラベルの行だけ（ラベルに無い行は無視＝捏造しない）。
    - 値の補完・推測をしない（空欄を「未定」等で埋めない・空値の行は落とす）。"""
    t = str(text or "")
    if _HEARING_HEADER not in t:
        return {"is_hearing": False, "mode": "", "receipt": "", "fields": {}}
    fields: dict = {}
    for raw_line in t.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _HEARING_LINE_RE.match(line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if label in _HEARING_LABELS and value and label not in fields:
            fields[label] = value
    mode = fields.get("種別", "")
    if mode not in ("賃貸", "購入"):
        mode = ""
    return {"is_hearing": True, "mode": mode, "receipt": fields.get("受付番号", ""), "fields": fields}


TRANSITIONS = {
    "queued": {"gated", "canceled"},
    "gated": {"sent", "failed", "canceled"},
    "sent": {"failed"},
    "failed": {"gated"},
    "canceled": set(),
}


def new_line_message(msg_id: str, to_user: str, text: str, *, kind: str = "push") -> dict:
    """送信LINEメッセージを outbox に積む（status=queued）。実送信はまだ＝送信確認(gated)が要る。
    kind: reply（replyToken必須）/ push（to_userへ）。捏造しない・空textは拒否。"""
    if not msg_id or not (text or "").strip():
        raise LineError(400, "msg_id と text は必須です。")
    if kind not in ("reply", "push"):
        raise LineError(400, "kind は reply/push のいずれかです。")
    return {
        "msg_id": msg_id, "to_user": to_user or "", "text": text.strip(), "kind": kind,
        "message_type": "text", "direction": "outbound", "status": "queued", "created_at": _now(),
        "history": [("queued", _now())], "provider": "", "external_id": "", "sent": False,
    }


def new_line_flex_message(msg_id: str, to_user: str, flex: dict, alt_text: str,
                          *, kind: str = "push") -> dict:
    """Flex（bubble/carousel）メッセージを outbox に積む（status=queued）。text と後方互換の同一スキーマに
    message_type="flex"・flex_content・alt_text を足す。実送信はまだ＝送信確認(gated)＋実チャネルは承認が要る。
    outbox 一覧は text を表示に使うので altText を text にも入れる。捏造しない・空 flex は拒否。"""
    if not msg_id or not isinstance(flex, dict) or not flex:
        raise LineError(400, "msg_id と flex コンテナ（bubble/carousel）は必須です。")
    if kind not in ("reply", "push"):
        raise LineError(400, "kind は reply/push のいずれかです。")
    alt = (str(alt_text or "").strip() or "物件のご紹介")[:400]
    return {
        "msg_id": msg_id, "to_user": to_user or "", "text": alt, "kind": kind,
        "message_type": "flex", "flex_content": flex, "alt_text": alt,
        "direction": "outbound", "status": "queued", "created_at": _now(),
        "history": [("queued", _now())], "provider": "", "external_id": "", "sent": False,
    }


def transition(message: dict, to: str, *, provider: LineProvider | None = None,
               actor: str = "", allow_real_send: bool = False, note: str = "") -> dict:
    """状態遷移。許可外遷移は拒否。sent には provider が必要（既定Mock）。送信は gated（人間確認）を通った後だけ。
    **実発火ゲート**: provider.connected（実チャネル）で送る場合は allow_real_send=True 必須（無ければ拒否）。"""
    cur = message.get("status")
    if to not in TRANSITIONS.get(cur, set()):
        raise LineError(409, f"許可されない遷移: {cur} → {to}")
    if to == "gated" and not actor:
        raise LineError(403, "送信確認（gated）には確認者（actor）が必要です（人間ゲート）。")
    if to == "sent":
        prov = provider or MockLineProvider()
        if getattr(prov, "connected", False) and not allow_real_send:
            raise LineError(403, "実チャネルでのLINE送信は明示の承認（allow_real_send）が必要です"
                                 "（人間ゲート・未承認の実送信は拒否）。")
        res = prov.send(message)
        message["provider"] = res.get("provider", prov.name)
        message["external_id"] = res.get("external_id", "")
        message["sent"] = bool(res.get("sent", False))
        message["provider_outcome"] = str(
            res.get("outcome") or ("accepted" if message["sent"] else "unknown"))
        message["provider_note"] = str(res.get("note") or "")
    message["status"] = to
    message["history"].append((to, _now(), actor, note) if (actor or note) else (to, _now()))
    return message
