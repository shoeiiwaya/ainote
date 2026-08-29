"""hub_core/fax.py — FAXブリッジ（受信=Webhook署名検証＋正規化→OCR / 送信=outbox＋送信ゲート＋Mock）。

なぜ: 不動産のFAX利用率は全業種トップ（CIAJ 2024=63.6%）。物確（物件確認）・マイソク送受信はまだFAXが主。
本モジュールは line_webhook（受信）＋esign（送信ゲート）と同じ型でFAXを扱う（LINE⇄FAX両対応ハーネス）:
- 受信: クラウドFAXプロバイダのWebhook署名を検証（HMAC-SHA256・定数時間比較）＋着信FAXを正規化。
  着信画像（PDF/TIFF）は無料ローカルOCR（macOS Vision/Windows）で読取→物確回答等を構造化（呼出側）。
- 送信: マイソク/物確文書をoutboxに積み、**送信ゲート（人間確認）**を通してからプロバイダへ。既定=
  MockFaxProvider（実送信しない・未接続）。**実クラウドFAXプロバイダの接続・実送信は人間ゲート**
  （APIキー/課金/実発火）。esignと同じく provider.connected の実送信は allow_real_send 明示が必須。
- stdlib only（hmac/hashlib/base64/json）・ネットワーク非接触（送信はProvider実装側）。捏造しない。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import re

JST = datetime.timezone(datetime.timedelta(hours=9))


def _now() -> str:
    return datetime.datetime.now(JST).replace(microsecond=0).isoformat()


class FaxError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ============================ 受信（inbound）＝ line_webhook 型 ============================

def verify_webhook(secret: str, body: bytes, signature: str) -> bool:
    """クラウドFAXプロバイダのWebhook署名を検証（HMAC-SHA256 = Base64・定数時間比較）。
    secret 未設定時は False（未接続＝受け付けない）。line_webhook と同じ規律。"""
    if not secret or not signature or not isinstance(signature, str):
        return False
    body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("ascii")
    return hmac.compare_digest(expected, signature)


def normalize_inbound(payload) -> dict:
    """プロバイダのWebhookペイロード（着信FAX）→ ri-hub の素材へ正規化。
    実登録は operation 経由・着信画像のOCRは呼出側（無料ローカルOCR）。捏造しない（無い項目は空）。"""
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload if not isinstance(payload, (bytes, bytearray))
                                 else payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise FaxError(400, "Webhookペイロードが不正なJSONです。")
    if not isinstance(payload, dict):
        raise FaxError(400, "Webhookペイロードはオブジェクトである必要があります。")

    def g(*keys):
        for k in keys:
            v = payload.get(k)
            if v not in (None, ""):
                return str(v)
        return ""
    fax_id = g("fax_id", "id", "message_id")
    return {
        "fax_id": fax_id,
        "delivery_id": g("delivery_id", "event_id", "webhook_event_id") or fax_id,
        "direction": "inbound",
        "from_number": g("from", "from_number", "caller", "src"),
        "to_number": g("to", "to_number", "dst"),
        "pages": g("pages", "num_pages", "page_count"),
        "media_ref": g("media_url", "file", "document_url", "url"),  # 取得は人間ゲート/呼出側
        "received_at": g("received_at", "timestamp", "created_at") or _now(),
        "status": "received",
    }


# ============================ 送信（outbound）＝ esign 型 ============================

class FaxProvider:
    """FAXプロバイダの抽象。実装は send（実送信）と connected（実接続か）を提供。"""
    name = "abstract"
    connected = False

    def send(self, job: dict) -> dict:
        raise NotImplementedError


class MockFaxProvider(FaxProvider):
    """ローカルモック（実FAX送信しない）。プロバイダ未接続時の既定。設計/フロー検証用。"""
    name = "mock"
    connected = False   # 実接続でない＝実送信していないことを表す

    def send(self, job: dict) -> dict:
        return {"provider": self.name, "external_id": "MOCK-FAX-" + str(job.get("job_id", "")),
                "sent": False, "outcome": "mock",
                "note": "モック: 実際のFAX送信は行っていません（未接続）。"}


# FAXジョブの状態遷移（queued=outbox積み → gated=人間が送信確認 → sent=送信実行）。
TRANSITIONS = {
    "queued": {"gated", "canceled"},
    "gated": {"sent", "failed", "canceled"},
    "sent": {"failed"},
    "failed": {"gated"},
    "canceled": set(),
}

_TEL_RE = re.compile(r"^[0-9+\-() ]{6,20}$")


def new_fax_job(job_id: str, to_number: str, doc_id: str, title: str, *, pages: int = 1) -> dict:
    """送信FAXを outbox に積む（status=queued）。実送信はまだ行わない＝人間の送信確認(gated)が要る。"""
    if not job_id or not title:
        raise FaxError(400, "job_id と title は必須です。")
    if not to_number or not _TEL_RE.match(str(to_number).strip()):
        raise FaxError(400, "送信先FAX番号が不正です。")
    return {
        "job_id": job_id, "to_number": str(to_number).strip(), "doc_id": doc_id or "",
        "title": title, "pages": int(pages or 1), "direction": "outbound",
        "status": "queued", "created_at": _now(), "history": [("queued", _now())],
        "provider": "", "external_id": "", "sent": False,
    }


def transition(job: dict, to: str, *, provider: FaxProvider | None = None,
               actor: str = "", allow_real_send: bool = False, note: str = "") -> dict:
    """状態遷移。許可外遷移は拒否。sent には provider が必要（既定Mock）。送信は gated（人間確認）を通った後だけ。
    **実送信の機械ゲート**: provider.connected（実プロバイダ）で送る場合は allow_real_send=True 必須。
    無ければ FaxError（＝人間ゲートを通さない実FAX送信をモジュールが拒否）。esign と同じ規律。"""
    cur = job.get("status")
    if to not in TRANSITIONS.get(cur, set()):
        raise FaxError(409, f"許可されない遷移: {cur} → {to}")
    if to == "gated" and not actor:
        raise FaxError(403, "送信確認（gated）には確認者（actor）が必要です（人間ゲート）。")
    if to == "sent":
        prov = provider or MockFaxProvider()
        if getattr(prov, "connected", False) and not allow_real_send:
            raise FaxError(403, "実プロバイダでのFAX送信は明示の承認（allow_real_send）が必要です"
                                "（人間ゲート・未承認の実送信は拒否）。")
        res = prov.send(job)
        job["provider"] = res.get("provider", prov.name)
        job["external_id"] = res.get("external_id", "")
        job["sent"] = bool(res.get("sent", False))
        job["provider_outcome"] = str(
            res.get("outcome") or ("accepted" if job["sent"] else "unknown"))
        job["provider_note"] = str(res.get("note") or "")
    job["status"] = to
    job["history"].append((to, _now(), actor, note) if (actor or note) else (to, _now()))
    return job


# ============================ 物確（FAXの本丸）＝ 決定論生成/緩パース ============================

def build_bukkaku_fax(fields: dict, *, sender: dict | None = None, today: str = "") -> str:
    """物確（物件確認）FAXの本文を決定論的に生成（相手業者に「この物件は現在も取扱可能か」を確認）。
    弱いLLMに書かせない・空欄は記入欄で残す。返信欄（空室/成約/変更）つき。捏造しない。"""
    f = fields or {}
    s = sender or {}
    name = str(f.get("property_name") or "").strip() or "（物件名）"
    lines = [
        "＝＝＝ 物 件 確 認 の お 願 い （FAX） ＝＝＝", "",
        f"送信日: {today or '　年　月　日'}",
        f"差出: {s.get('company_name') or '（貴社名）'}　担当: {s.get('staff') or '＿＿＿＿'}"
        f"　TEL {s.get('tel') or '＿＿＿'} / FAX {s.get('fax') or '＿＿＿'}", "",
        "下記物件について、現在の取扱状況をご確認のうえ、本紙にご記入し返信FAXをお願いいたします。", "",
        f"■ 物件名: {name}",
        f"■ 所在地: {f.get('address') or '＿＿＿＿＿＿＿＿'}",
        f"■ 価格/賃料: {f.get('price') or '＿＿＿'}　■ 間取り: {f.get('layout') or '＿'}"
        f"　■ 面積: {f.get('area') or '＿'}", "",
        "───────── 以下、貴社ご記入欄（返信） ─────────",
        "◯ 現在の状況:　□ 取扱中（空室/売出中）　□ 商談中　□ 成約済　□ 取扱終了",
        "◯ 変更点:　□ なし　□ 価格変更（　　　　　）　□ 条件変更（　　　　　）",
        "◯ 内見/紹介:　□ 可　□ 要連絡　□ 不可",
        "◯ ご記入者:　＿＿＿＿＿＿　ご記入日: 　　年　月　日",
        "",
        "※本FAXは物件確認の依頼です。ご記入のうえ返信いただけますようお願いいたします。",
    ]
    return "\n".join(lines)


# チェック印としてOCRされうる字（空box「□」は除外）。返信は送信フォームのコピーで全選択肢ラベルが印字
# されるため、部分一致でなく「ラベル直前の印」で判定する（＝チェックの無い選択肢を採らない・捏造防止）。
_MARKS = "☑✓✔■●▪◾◼✗✘×レ乄ﾚ"
_STATUS_OPTS = [("成約済", "成約済"), ("成約", "成約済"), ("契約済", "成約済"),
                ("商談中", "商談中"), ("申込", "商談中"),
                ("取扱終了", "取扱終了"),
                ("取扱中", "取扱中"), ("空室", "取扱中"), ("売出", "取扱中"), ("募集中", "取扱中")]
# viewing のラベルは build_bukkaku_fax のフォーム表記（□ 可 / □ 要連絡 / □ 不可）に一致させる。
# 「不可」は「可」を含むが、_marked_values はラベル直前の印で判定するので取り違えない。
_VIEW_OPTS = [("要連絡", "要連絡"), ("不可", "不可"), ("可", "可")]


def _marked_values(text: str, options) -> set:
    """各選択肢ラベルの直前（小窓3字）にチェック印があり、空box「□」でないものの値集合。
    より長い別ラベルの末尾に重なるマッチは飛ばす（「可」が「不可」の一部＝不可の■を可と誤認しない）。"""
    labels = [lab for lab, _ in options]
    hits = set()
    for label, val in options:
        for m in re.finditer(re.escape(label), text):
            if any(o != label and len(o) > len(label)
                   and text[max(0, m.end() - len(o)):m.end()] == o for o in labels):
                continue          # このマッチはより長いラベルの末尾（例: 可 が 不可 の一部）
            pre = text[max(0, m.start() - 3):m.start()]
            if "□" in pre:
                continue          # 空boxのラベルはマークでない
            if any(c in pre for c in _MARKS):
                hits.add(val)
    return hits


def parse_bukkaku_reply(text: str) -> dict:
    """返信FAXのOCRテキスト → 物確回答。**チェック印が付いた選択肢だけ**を採る。返信は送信フォームのコピーで
    全選択肢ラベルが常に印字されるため、部分一致は使わない（毎回「成約済」を返す捏造を防ぐ）。一意にマーク
    された時のみ確定・記入欄は数値/記述がある時のみ。読み取れなければ入れない（捏造しない・担当が現物照合）。"""
    t = text or ""
    out: dict = {}
    st = _marked_values(t, _STATUS_OPTS)
    if len(st) == 1:                       # 一意にマークされた時だけ（曖昧/複数/無印は入れない）
        out["status"] = next(iter(st))
    m = re.search(r"価格変更[（(]\s*([0-9][0-9,]*\s*万?円)", t)   # 記入（数値）がある時だけ
    if m:
        out["price_change"] = m.group(1).strip()
    m2 = re.search(r"条件変更[（(]\s*([^\n　（）()]{1,30})", t)     # 記入（非空白）がある時だけ
    if m2 and m2.group(1).strip():
        out["condition_change"] = m2.group(1).strip()
    vw = _marked_values(t, _VIEW_OPTS)
    if len(vw) == 1:
        out["viewing"] = next(iter(vw))
    return out

class HttpFaxProvider(FaxProvider):
    """設定したHTTP送信APIへFAXを投げる汎用アダプタ。

    国内クラウドFAXの多くは「宛先番号＋PDF（またはそのURL）をPOST」の形なので、
    特定業者に固定せず設定で差し替えられるようにする。
    - 送信先URL・認証方式・発信番号は「接続設定」画面から入れる（非技術者向けガイドつき）
    - トークンは data_dir でなく keys ファイル（0600）に置く
    - **ここに来る時点で人間の送信確認（gated）を通っている**。勝手には送らない
    """

    name = "http"

    def __init__(self, *, endpoint: str, token: str, method: str = "POST",
                 auth_style: str = "bearer", from_number: str = "",
                 service_name: str = "", timeout: int = 20):
        self.endpoint = endpoint
        self.token = token
        self.method = (method or "POST").upper()
        self.auth_style = (auth_style or "bearer").lower()
        self.from_number = from_number
        self.service_name = service_name or "設定したFAXサービス"
        self.timeout = timeout
        self.connected = bool(endpoint and token)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth_style == "bearer":
            h["Authorization"] = f"Bearer {self.token}"
        elif self.auth_style == "token":
            h["Authorization"] = f"Token {self.token}"
        elif self.auth_style == "x-api-key":
            h["X-API-Key"] = self.token
        return h

    def send(self, job: dict) -> dict:
        """実送信。到達不能・拒否は例外にせず結果で返す（送信台帳に理由を残すため）。"""
        import json as _json
        import urllib.error
        import urllib.request
        if not self.connected:
            raise FaxError(409, "FAX送信サービスが未設定です。接続設定から登録してください。")
        payload = {
            "to": job.get("to_number"),
            "from": self.from_number,
            "document_id": job.get("doc_id"),
            "title": job.get("title"),
            "pages": job.get("pages"),
            "idempotency_key": job.get("idempotency_key"),
        }
        headers = self._headers()
        if job.get("idempotency_key"):
            headers["Idempotency-Key"] = str(job["idempotency_key"])
        req = urllib.request.Request(
            self.endpoint, method=self.method,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(65536).decode("utf-8", "replace")
                code = resp.getcode()
        except urllib.error.HTTPError as exc:
            try:
                status = exc.code
            finally:
                exc.close()
            return {"provider": self.name, "external_id": "", "sent": False,
                    "outcome": "rejected",
                    "note": f"{self.service_name} が受け付けませんでした（{status}）。"}
        except (urllib.error.URLError, OSError, TimeoutError):
            return {"provider": self.name, "external_id": "", "sent": False,
                    "outcome": "unknown",
                    "note": f"{self.service_name} につながりませんでした。"}
        ext = ""
        try:
            body = _json.loads(raw)
            if isinstance(body, dict):
                for k in ("id", "job_id", "fax_id", "message_id"):
                    if body.get(k):
                        ext = str(body[k])
                        break
        except ValueError:
            pass
        ok = 200 <= int(code) < 300
        return {"provider": self.name, "external_id": ext, "sent": ok,
                "outcome": "accepted" if ok else "rejected",
                "note": (f"{self.service_name} へ送信しました。" if ok else
                         f"{self.service_name} が受け付けませんでした（{code}）。")}


def build_fax_provider(data_dir) -> FaxProvider:
    """設定済みなら実プロバイダ、未設定ならモック（実送信しない）を返す。

    未設定を異常にしない＝FAXを使わない人はそのまま使える。設定した瞬間から実送信経路になる。
    """
    try:
        from hub_core import connections as _conn
        cfg = _conn.load_fax_config(data_dir) or {}
        token = _conn.fax_token(data_dir)
    except Exception:      # noqa: BLE001
        return MockFaxProvider()
    if cfg.get("endpoint") and token:
        return HttpFaxProvider(
            endpoint=str(cfg.get("endpoint") or ""), token=token,
            method=str(cfg.get("method") or "POST"),
            auth_style=str(cfg.get("auth_style") or "bearer"),
            from_number=str(cfg.get("from_number") or ""),
            service_name=str(cfg.get("service_name") or ""))
    return MockFaxProvider()
