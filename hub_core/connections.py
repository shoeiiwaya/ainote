"""hub_core/connections.py — 外部連携の接続テスト（非技術者が「本当に繋がった」を確認できる）。

あいのては中核がローカルで動くが、AI（ローカルOllama/クラウドAPI）・メール送信（SMTP）・災害リスク（PRS）は
利用者自身のアカウント/接続が前提（BYO）。このモジュールは各連携の**接続テスト**を提供する。
テストは利用者がボタンで起動する（自動送信しない）。SMTPは認証確認のみ＝メールは送らない。

stdlib のみ（urllib.request / smtplib / ssl）。失敗は例外でなく {ok, detail} で返す（graceful）。
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _is_loopback_url(url: str) -> bool:
    """URL のホストが loopback に解決されるか（SSRF対策）。Ollama はローカル前提なので
    リクエスト由来の base は loopback のみ許可し、内部/外部への任意fetchを防ぐ。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, u.port or 80, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_loopback:
                return False
        except ValueError:
            return False
    return True


def test_ollama(base: str = "") -> dict:
    """ローカルAI（Ollama）の接続テスト。/api/tags を叩いてモデル一覧が取れるか。
    base 例: http://localhost:11434 。SSRF対策で loopback のみ許可。失敗時 ok=False＋理由。"""
    base = (base or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434").rstrip("/")
    # /v1 が付いていたら剥がす（tagsは /api/tags）
    if base.endswith("/v1"):
        base = base[:-3]
    if not _is_loopback_url(base):
        return {"ok": False, "detail": "ローカル（localhost）のOllamaのみテストできます。"
                "別マシンのOllamaは環境変数 OLLAMA_HOST で設定してください。"}
    url = base + "/api/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ri-hub/conn-test"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m.get("name", "") for m in (data.get("models") or [])]
        if models:
            return {"ok": True, "detail": f"接続OK。利用可能なモデル: {', '.join(models[:5])}"}
        return {"ok": True, "detail": "接続OK。ただしモデルが未取得です（例: ollama pull qwen3:8b）。"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return {"ok": False, "detail": f"接続できません（{base}）。Ollamaが起動しているか確認してください。"}
    except (ValueError, json.JSONDecodeError):
        return {"ok": False, "detail": "応答を解釈できませんでした。URLを確認してください。"}


def test_smtp(host: str, port, user: str, password: str, *, use_tls: bool = True) -> dict:
    """メール送信（SMTP）の接続テスト＝**認証のみ・メールは送らない**。
    失敗時 ok=False＋理由（非技術者向けに平易化）。"""
    if not host or not user or not password:
        return {"ok": False, "detail": "サーバ・ユーザー名・パスワードを入力してください。"}
    try:
        p = int(port or (587 if use_tls else 25))
    except (TypeError, ValueError):
        return {"ok": False, "detail": "ポート番号が数値ではありません。"}
    try:
        ctx = ssl.create_default_context()
        if p == 465:
            srv = smtplib.SMTP_SSL(host, p, timeout=8, context=ctx)
        else:
            srv = smtplib.SMTP(host, p, timeout=8)
            if use_tls:
                srv.starttls(context=ctx)
        try:
            srv.login(user, password)
        finally:
            srv.quit()
        return {"ok": True, "detail": "接続・認証OK。送信できる状態です（テストではメールを送っていません）。"}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "detail": "ユーザー名かパスワードが違います。Gmailはアプリパスワードが必要です。"}
    except (smtplib.SMTPException, OSError, ssl.SSLError):
        return {"ok": False, "detail": "サーバに接続できません。サーバ名・ポート・TLS設定を確認してください。"}


def test_prs(address: str = "東京都千代田区丸の内1-1-1", *, data_dir=None) -> dict:
    """juusetsu-hazard-v1接続を1件確認する。住所を送る明示操作で、スコアAPIは使わない。"""
    from hub_core import prs_juusetsu
    if not prs_juusetsu.configured():
        return {"ok": False, "detail": "PRS重説調査の接続URLが未設定です。"}
    if data_dir is None:
        return {"ok": False, "detail": "取得receiptの保存先を確認できません。"}
    receipt = prs_juusetsu.fetch_for_draft(data_dir, address)
    if receipt.get("connected"):
        count = sum(row.get("status") != "NEEDS_PRIMARY_SOURCE"
                    for row in receipt.get("items") or [])
        return {"ok": True,
                "detail": f"接続できました。6項目中{count}項目に一次原典があります。"
                          f" receipt: {receipt.get('receipt_id') or 'IDなし'}"}
    return {"ok": False, "detail": "接続できませんでした。URL・キー・契約版を確認してください。"}


def harness_config() -> dict:
    """line-harness-oss 連携の設定を env から。実接続値は人間ゲートで投入（未設定ならあいのてはMock送信）。"""
    return {"url": (os.environ.get("LINE_HARNESS_API_URL") or "").rstrip("/"),
            "api_key": os.environ.get("LINE_HARNESS_API_KEY") or "",
            "account_id": os.environ.get("LINE_HARNESS_ACCOUNT_ID") or ""}


def harness_configured() -> bool:
    c = harness_config()
    return bool(c["url"] and c["api_key"])


def harness_send(friend_id: str, text: str, *, msgtype: str = "text",
                 alt_text: str = "", idempotency_key: str = "",
                 timeout: float = 10.0) -> dict:
    """line-harness-oss 経由でLINEを実送信（POST /api/friends/:id/messages・Bearer認証）。
    **実送信＝呼び出し側(line_confirm_send)の送信ゲート＋allow_real_send通過後にのみ呼ぶこと**。
    env(URL/API_KEY)未設定なら ok=False（＝あいのてはMockのまま実送信しない）。graceful（例外でなく{ok,detail}）。"""
    c = harness_config()
    if not (c["url"] and c["api_key"]):
        return {"ok": False, "detail": "line-harness 連携が未設定（LINE_HARNESS_API_URL / LINE_HARNESS_API_KEY）。"}
    if not friend_id or not (text or "").strip():
        return {"ok": False, "detail": "friend_id と本文が必要です。"}
    url = f"{c['url']}/api/friends/{urllib.parse.quote(str(friend_id))}/messages"
    if c["account_id"]:
        url += "?lineAccountId=" + urllib.parse.quote(c["account_id"])
    body = json.dumps({"content": text, "messageType": msgtype,
                       "altText": (alt_text or text)[:400],
                       "idempotencyKey": idempotency_key}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + c["api_key"], "Content-Type": "application/json",
        "User-Agent": "ri-hub/harness"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        mid = (data.get("data") or {}).get("messageId") or data.get("messageId") or ""
        ok = bool(data.get("success", True))
        return {"ok": ok, "message_id": mid, "outcome": "accepted" if ok else "rejected",
                "detail": "送信しました。" if ok else "harness が受け付けませんでした。"}
    except urllib.error.HTTPError as e:
        try:
            status = e.code
        finally:
            e.close()
        return {"ok": False, "outcome": "rejected",
                "detail": f"harness API エラー（HTTP {status}）。鍵・friend_idを確認してください。"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"ok": False, "outcome": "unknown",
                "detail": "harness に接続できません。URL・ネットワークを確認してください。"}


def harness_pull_feed(*, cursor: str = "", direction: str = "",
                      limit: int = 200, max_pages: int = 50, timeout: float = 10.0) -> dict:
    """pull型会話取込の**唯一のfetch境界**。poll_once はこの関数だけを呼び、endpoint選択を持たない。

    正本＝**増分feed（単一endpoint・cursorページング）**（integration-contract.md「増分feed」v2・本番Version 5c684932）:
      GET /api/messages/feed?cursor=<opaqueトークン>&limit=<1..200>[&direction=<incoming|outgoing>]（Bearer認証）。
    **direction は既定で無指定＝送受信両方の単一ストリーム**（feed.ts: direction 省略で both を返す）。
    こうして送信側（自動応答/手動/flex/broadcast）も同じcursorで取り込み、会話台帳を完全化する。各item は
    自分の direction を持つので、呼び側はフィルタでなく item.direction で送受信を区別する。
    cursor無し=先頭から・items空でも nextCursor は維持（冪等）。この関数が cursor から hasMore が false に
    なるまでページ取得し、全 item を集約して返す
    （**N+1 の per-friend messages API は pull では使わない**＝レート内に収めるため）。
    返り items は feed 生item（friend情報 inline: friendId/friendDisplayName/friendLineUserId・direction/messageType）。
    呼び側は next_cursor を永続保存し次回そこから増分取得する（重複除去の最終保証は呼び側のメッセージ id dedupe）。
    暴走ガード: max_pages 上限＋cursor 非前進で打切り。

    ⚠️ **cursor は完全に不透明なトークン**（実体はharness側 rowid・旧 createdAt|id 形式は廃止＝送ると400）。
    あいのて側は**中身を一切パースせず**サーバの nextCursor をそのまま echo する（形式を仮定しない）。
    cursor は同一 filter ストリームに束縛＝**無フィルタ（direction 無指定）で一貫使用**（filter を跨いで使い回さない）。
    レート: 認証済み1000req/分。**429 を受けたら台帳に何も書かず**（fail-closed）status=429＋retry_after を返す
    （呼び側/常駐デーモンが Retry-After に従いバックオフ）。

    失敗（未設定/接続/認証/JSON/429）は ok=False＋items無しで返す（fail-closed・呼び側は台帳に何も書かない）。"""
    c = harness_config()
    if not (c["url"] and c["api_key"]):
        return {"ok": False, "detail": "line-harness 連携が未設定です（URL / API_KEY）。",
                "items": [], "next_cursor": cursor}
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 200
    items: list = []
    cur = str(cursor or "")
    pages = 0
    data: dict = {}
    while pages < max(1, int(max_pages)):
        qs = {"limit": str(lim)}
        if direction:                 # 既定は無指定＝送受信両方（direction を送ると片側だけに絞られる）
            qs["direction"] = direction
        if cur:
            qs["cursor"] = cur
        if c["account_id"]:
            qs["lineAccountId"] = c["account_id"]
        url = f"{c['url']}/api/messages/feed?" + urllib.parse.urlencode(qs)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + c["api_key"],
                                                   "User-Agent": "ri-hub/harness"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                status = e.code
                ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
            finally:
                e.close()
            if status == 429:   # レート上限: Retry-After に従いバックオフ・台帳には何も書かない（fail-closed）
                try:
                    retry_after = int(ra) if ra is not None else None
                except (TypeError, ValueError):
                    retry_after = None
                return {"ok": False, "status": 429, "items": [], "next_cursor": cursor,
                        "retry_after": retry_after,
                        "detail": "harness レート上限（HTTP 429）。Retry-After に従いバックオフします。"}
            return {"ok": False, "status": status, "items": [], "next_cursor": cursor,
                    "detail": f"harness API エラー（HTTP {status}）。API_KEYを確認してください。"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"ok": False, "items": [], "next_cursor": cursor,
                    "detail": "harness に接続できません。URL・ネットワークを確認してください。"}
        except (ValueError, json.JSONDecodeError):
            return {"ok": False, "items": [], "next_cursor": cursor,
                    "detail": "応答を解釈できませんでした（不正なJSON）。"}
        d = data.get("data") if isinstance(data.get("data"), dict) else None
        if d is None:
            return {"ok": False, "items": [], "next_cursor": cursor,
                    "detail": "応答に着信フィードが含まれていません。"}
        page = d.get("items") if isinstance(d.get("items"), list) else []
        items.extend(page)
        pages += 1
        nxt = str(d.get("nextCursor") or "")
        if not bool(d.get("hasMore")):
            cur = nxt or cur   # 終端: nextCursor を維持（空でも前回値・冪等契約）
            break
        if not nxt or nxt == cur:
            cur = nxt or cur   # cursor 非前進（サーバ異常）＝暴走回避で打切り（既取得は返す・重複はdedupeが吸収）
            break
        cur = nxt
    return {"ok": bool(data.get("success", True)), "items": items, "next_cursor": cur,
            "pages": pages, "detail": "着信フィードを取得しました。"}


def harness_calendar_slots(date: str, *, slot_minutes: int = 60, start_hour: int = 9,
                           end_hour: int = 18, timeout: float = 10.0) -> dict:
    """担当のGoogleカレンダー空き枠（FreeBusy）を harness 経由で取得（GET /api/integrations/
    google-calendar/slots・Bearer認証）。あいのての events台帳と重ねる**外部整合の source**。

    **fail-open**（＝harness未設定/接続失敗でも呼び側はあいのて単独で枠を出す）: env(URL/API_KEY/
    CALENDAR_CONNECTION_ID)が無ければ ok=False を返すだけ。あいのてはローカルファースト＝台帳が正本で、
    これは「担当の実予定（Google等）とのダブルブッキング回避」を足すためのベストエフォート。
    返り slots=[{start, end, available}]（harness の startAt/endAt/available を正規化）。"""
    c = harness_config()
    conn_id = os.environ.get("LINE_HARNESS_CALENDAR_CONNECTION_ID") or c.get("account_id") or ""
    if not (c["url"] and c["api_key"] and conn_id and (date or "").strip()):
        return {"ok": False, "slots": [],
                "detail": "harness カレンダー連携が未設定です（URL/API_KEY/CALENDAR_CONNECTION_ID）。"}
    qs = {"connectionId": conn_id, "date": date, "slotMinutes": str(slot_minutes),
          "startHour": str(start_hour), "endHour": str(end_hour)}
    url = f"{c['url']}/api/integrations/google-calendar/slots?" + urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + c["api_key"],
                                               "User-Agent": "ri-hub/harness"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            status = e.code
        finally:
            e.close()
        return {"ok": False, "slots": [], "detail": f"harness カレンダーAPIエラー（HTTP {status}）。"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"ok": False, "slots": [], "detail": "harness カレンダーに接続できません。"}
    raw = data.get("data") if isinstance(data.get("data"), list) else []
    slots = [{"start": str(s.get("startAt") or ""), "end": str(s.get("endAt") or ""),
              "available": bool(s.get("available"))} for s in raw if isinstance(s, dict)]
    return {"ok": bool(data.get("success", True)), "slots": slots,
            "detail": "担当カレンダーの空き枠を取得しました。"}


def harness_test() -> dict:
    """line-harness 連携の疎通確認（友だち一覧を1件取得してみる・Bearer認証）。未設定は ok=False。"""
    c = harness_config()
    if not (c["url"] and c["api_key"]):
        return {"ok": False, "detail": "line-harness 連携が未設定です（URL / API_KEY）。"}
    url = f"{c['url']}/api/friends?limit=1"
    if c["account_id"]:
        url += "&lineAccountId=" + urllib.parse.quote(c["account_id"])
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + c["api_key"],
                                               "User-Agent": "ri-hub/harness"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        return {"ok": True, "detail": "line-harness に接続OK（認証成功）。"}
    except urllib.error.HTTPError as e:
        try:
            status = e.code
        finally:
            e.close()
        return {"ok": False, "detail": f"接続エラー（HTTP {status}）。API_KEYを確認してください。"}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "detail": "harness に接続できません。URLを確認してください。"}


def run_test(kind: str, params: dict, *, data_dir=None) -> dict:
    """種別を受けて接続テストを振り分ける。UIの /api/conn-test から呼ぶ。"""
    if kind == "harness":
        return harness_test()
    if kind == "ollama":
        return test_ollama(str(params.get("base") or ""))
    if kind == "smtp":
        return test_smtp(str(params.get("host") or ""), params.get("port"),
                         str(params.get("user") or ""), str(params.get("password") or ""),
                         use_tls=str(params.get("tls", "1")) not in ("0", "false", "no"))
    if kind == "fax":
        return test_fax(str(params.get("endpoint") or ""), str(params.get("token") or ""))
    if kind == "prs":
        return test_prs(str(params.get("address") or "東京都千代田区丸の内1-1-1"),
                        data_dir=data_dir)
    return {"ok": False, "detail": f"未知のテスト種別: {kind}"}


# --- SMTP設定の保存/読込（非秘密=data_dir/smtp_config.json・秘密=~/.ri-hub/keys/smtp_password 0600）---
def _smtp_config_path(data_dir):
    return Path(data_dir) / "smtp_config.json"


def _smtp_key_path():
    return Path(os.environ.get("RI_HUB_KEYS_DIR", str(Path.home() / ".ri-hub" / "keys"))) / "smtp_password"


def load_smtp_config(data_dir) -> dict:
    """SMTP設定（host/port/user/tls/from）を返す。パスワードは含めない。"""
    p = _smtp_config_path(data_dir)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def smtp_password(data_dir=None) -> str:
    """SMTPパスワード: env > keysファイル。"""
    v = (os.environ.get("RI_HUB_SMTP_PASSWORD") or "").strip()
    if v:
        return v
    kp = _smtp_key_path()
    try:
        return kp.read_text(encoding="utf-8").strip() if kp.is_file() else ""
    except OSError:
        return ""


def save_smtp_config(data_dir, cfg: dict, password: str = "") -> None:
    """非秘密設定を data_dir へ、パスワードを keysファイルへ保存。
    パスワードは owner専用(0600)で**原子的に**書く。権限を強制できなければ保存を拒否（消して例外）。"""
    keep = {k: cfg.get(k) for k in ("host", "port", "user", "tls", "from") if cfg.get(k) is not None}
    p = _smtp_config_path(data_dir)
    p.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    if password:
        import stat
        kp = _smtp_key_path()
        kp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 既存を消してから owner-only で作成（作成時点から0600・レース回避）
        try:
            os.remove(kp)
        except FileNotFoundError:
            pass
        fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, password.encode("utf-8"))
        finally:
            os.close(fd)
        # 権限を強制できたか検証。owner以外に少しでも権限が付いていれば拒否（消して例外）。
        mode = stat.S_IMODE(os.stat(kp).st_mode)
        if mode & 0o077:
            try:
                os.chmod(kp, 0o600)
                mode = stat.S_IMODE(os.stat(kp).st_mode)
            except OSError:
                pass
            if mode & 0o077:
                os.remove(kp)
                raise PermissionError(
                    "SMTPパスワードを安全な権限(0600)で保存できませんでした（保存を中止）。")


def smtp_configured(data_dir) -> bool:
    c = load_smtp_config(data_dir)
    return bool(c.get("host") and c.get("user") and smtp_password(data_dir))

# --- FAX送信サービスの設定（非秘密=data_dir/fax_config.json・秘密=keysファイル 0600）-----
# FAXサービスへ接続するための設定層。
# 特定業者に固定せず、**HTTPで送信APIを叩く汎用アダプタ**にする（国内クラウドFAXの多くはこの形）。
# 実送信そのものは従来どおり人間の送信確認ゲート（fax_confirm_send）を通す。
def _fax_config_path(data_dir):
    return Path(data_dir) / "fax_config.json"


def _fax_key_path():
    return Path(os.environ.get("RI_HUB_KEYS_DIR",
                               str(Path.home() / ".ri-hub" / "keys"))) / "fax_token"


def load_fax_config(data_dir) -> dict:
    """FAX送信サービスの設定（endpoint/method/auth_style/from_number/service_name）。トークンは含めない。"""
    p = _fax_config_path(data_dir)
    if not p.is_file():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def fax_token(data_dir=None) -> str:
    """FAX APIトークン: env > keysファイル。"""
    v = (os.environ.get("RI_HUB_FAX_TOKEN") or "").strip()
    if v:
        return v
    kp = _fax_key_path()
    try:
        return kp.read_text(encoding="utf-8").strip() if kp.is_file() else ""
    except OSError:
        return ""


def save_fax_config(data_dir, cfg: dict, token: str = "") -> None:
    """非秘密設定を data_dir へ、トークンを keysファイル(0600)へ保存する。"""
    keep = {k: cfg.get(k) for k in
            ("service_name", "endpoint", "method", "auth_style", "from_number")
            if cfg.get(k) is not None}
    _fax_config_path(data_dir).write_text(
        json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    if token:
        import stat
        kp = _fax_key_path()
        kp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = kp.with_name(kp.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, kp)
        if stat.S_IMODE(kp.stat().st_mode) != 0o600:
            kp.unlink(missing_ok=True)
            raise OSError("FAXトークンを所有者のみ(0600)で保存できませんでした。")


def test_fax(endpoint: str, token: str) -> dict:
    """FAX送信サービスの疎通確認＝**FAXは送らない**。

    設定した宛先URLへ到達できるか（名前解決とTCP接続）だけを見る。
    送信APIを実際に叩くと課金・実送信が起きるため、ここでは絶対に叩かない。
    """
    from urllib.parse import urlsplit
    if not endpoint:
        return {"ok": False, "detail": "送信先のURLが入っていません。"}
    u = urlsplit(endpoint)
    if u.scheme != "https":
        return {"ok": False, "detail": "https で始まるURLを入れてください（暗号化されない経路は使いません）。"}
    if not u.hostname:
        return {"ok": False, "detail": "URLの形が正しくありません。"}
    if not token:
        return {"ok": False, "detail": "APIトークンが入っていません。"}
    import socket
    try:
        socket.getaddrinfo(u.hostname, u.port or 443)
    except OSError:
        return {"ok": False, "detail": f"{u.hostname} が見つかりませんでした。URLをご確認ください。"}
    try:
        with socket.create_connection((u.hostname, u.port or 443), timeout=6):
            pass
    except OSError:
        return {"ok": False, "detail": f"{u.hostname} につながりませんでした。ネットワークをご確認ください。"}
    return {"ok": True, "detail": f"{u.hostname} につながりました（FAXは送っていません）。"}
