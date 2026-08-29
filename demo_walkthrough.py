#!/usr/bin/env python3
"""別のPCで初めて触る人の動きを、最初から最後まで実際に通す。

想定: まっさらな端末。会社情報も、ログインも、データも無い状態から始める。
    1. はじめての設定（会社のこと → ログインを作る → AIをどうするか）
    2. ログインされた状態でホームへ
    3. fixture無しの空台帳へ物件を登録する
    4. 既存顧客CSVを下見→取り込みし、登録済み物件へ接続する
    5. LINE連携をread-only loopbackで接続テストする（実送信0件）
    6. 窓口の札からマイソクを作る（4段）
    7. 重要事項説明書を作る（5段）
    8. 監査ログと出力を確認する
    9. データを暗号化して書き出し、別の復旧キーで内容を検証できる

各段は**実際のHTTP**で叩く。画面の中身も確かめる（200が返っただけでは通ったことにしない）。
実行: python3 demo_walkthrough.py   （通れば exit 0・どこかで転べば理由を出して exit 1）
"""
from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
STEPS: list[tuple[str, bool, str]] = []


def note(title: str, ok: bool, detail: str = "") -> None:
    STEPS.append((title, ok, detail))
    print(f"{'  OK ' if ok else '  NG '} {title}" + (f"  — {detail}" if detail else ""))


def _customer_facing_brand_ok(html: str, expected_company: str) -> bool:
    """Return whether the rendered customer document carries the configured company."""
    expected = str(expected_company or "").strip()
    return bool(expected and expected in html and "株式会社理" not in html)


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie = ""

    def _conn(self):
        return http.client.HTTPConnection(HOST, self.port, timeout=15)

    def get(self, path: str) -> tuple[int, str, dict]:
        c = self._conn()
        h = {"Cookie": self.cookie} if self.cookie else {}
        c.request("GET", path, headers=h)
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, body, hdrs

    def post(self, path: str, form: dict) -> tuple[int, str, dict]:
        c = self._conn()
        data = urllib.parse.urlencode(form, doseq=True)
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.cookie:
            h["Cookie"] = self.cookie
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        sc = hdrs.get("set-cookie", "")
        if "rihub_session" in sc:
            self.cookie = sc.split(";")[0]
        return r.status, body, hdrs

    def post_json(self, path: str, payload: dict) -> tuple[int, str, dict]:
        c = self._conn()
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://{HOST}:{self.port}",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        c.request("POST", path, body=raw, headers=headers)
        response = c.getresponse()
        body = response.read().decode("utf-8", "replace")
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        c.close()
        return response.status, body, response_headers

    def post_multipart(self, path: str, fields: dict[str, str], *,
                       filename: str, payload: bytes) -> tuple[int, str, dict]:
        boundary = "----ainote-fresh-walkthrough-boundary"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend([
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"), b"\r\n",
            ])
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             'Content-Type: text/csv; charset=utf-8\r\n\r\n').encode("utf-8"),
            payload, b"\r\n", f"--{boundary}--\r\n".encode("ascii"),
        ])
        raw = b"".join(chunks)
        c = self._conn()
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(raw)),
            "Origin": f"http://{HOST}:{self.port}",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        c.request("POST", path, body=raw, headers=headers)
        response = c.getresponse()
        body = response.read().decode("utf-8", "replace")
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        c.close()
        return response.status, body, response_headers

    def get_bytes(self, path: str) -> tuple[int, bytes]:
        c = self._conn()
        h = {"Cookie": self.cookie} if self.cookie else {}
        c.request("GET", path, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def post_bytes(self, path: str, form: dict) -> tuple[int, bytes, dict]:
        c = self._conn()
        data = urllib.parse.urlencode(form, doseq=True)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://{HOST}:{self.port}",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        c.request("POST", path, body=data, headers=headers)
        response = c.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        c.close()
        return response.status, raw, response_headers


def wait_up(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection(HOST, port, timeout=2)
            c.request("GET", "/")
            c.getresponse().read()
            c.close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def free_port() -> int:
    sock = socket.socket()
    sock.bind((HOST, 0))
    try:
        return sock.getsockname()[1]
    finally:
        sock.close()


class _ReadOnlyLineHandler(BaseHTTPRequestHandler):
    """Fresh walkthrough専用。友だち一覧GETだけを許可し、送信POSTは数えて拒否する。"""

    reads = 0
    sends = 0

    def do_GET(self):
        if (self.path.startswith("/api/friends?limit=1")
                and self.headers.get("Authorization") == "Bearer walkthrough-read-only"):
            type(self).reads += 1
            payload = b'{"success":true,"data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self):
        type(self).sends += 1
        self.send_error(405)

    def log_message(self, _format, *_args):
        return


def run(port: int, data_dir: Path) -> bool:
    cl = Client(port)
    company_profile = {
        "company_name": "株式会社みなと不動産",
        "license_no": "東京都知事（2）第12345号",
        "address": "東京都港区芝浦3丁目12番8号",
        "tel": "03-1234-5678",
        "association": "（公社）全国宅地建物取引業保証協会",
        "fair_trade": "首都圏不動産公正取引協議会",
    }

    # 1. まっさらな状態では、はじめての設定へ案内される
    st, body, hd = cl.get("/")
    ok = st == 303 and "/setup" in hd.get("location", "")
    note("まっさらな端末で開くと、はじめての設定へ案内される", ok, f"status={st}")

    st, body, _ = cl.get("/setup")
    ok = st == 200 and "まず、会社のことを" in body and "あなたの会社の名前で出ます" in body
    note("1段目で会社のことだけを聞き、書類が誰の名前で出るかを伝える", ok)
    note("1段目でパスワードまで聞いていない", 'name="owner_pw"' not in body)

    # 2. 段を進む（POST＝パスワードをURLに載せない）
    st, body, _ = cl.post("/setup/step", {
        "step": "account", **company_profile, "business": ["賃貸仲介", "売買仲介"]})
    ok = st == 200 and 'name="owner_user"' in body and "株式会社みなと不動産" in body
    note("2段目でログインを作る（1段目の答えは保持されている）", ok)

    st, body, _ = cl.post("/setup/step", {
        "step": "ai", **company_profile, "business": ["賃貸仲介", "売買仲介"],
        "owner_user": "daihyo", "owner_pw": "password123"})
    ok = st == 200 and 'name="llm_mode"' in body
    note("3段目でAIの扱いを選ぶ（既定は使わない）", ok and 'value="none" checked' in body)

    st, body, hd = cl.post("/setup", {
        **company_profile, "business": ["賃貸仲介", "売買仲介"], "owner_user": "daihyo",
        "owner_pw": "password123", "llm_mode": "none"})
    ok = st == 303 and "/home" in hd.get("location", "") and cl.cookie
    note("設定を終えるとログインされてホームへ", ok, f"status={st}")

    # 3. ホームに窓口の札が並ぶ
    st, body, _ = cl.get("/home")
    fudas = ["マイソクを作る", "重要事項説明書を作る", "お客様の問い合わせを見る",
             "お金を計算する", "物件を調べる", "書類を印刷する"]
    ok = st == 200 and all(f in body for f in fudas)
    note("ホームに「やりたいこと」の札が6枚並ぶ", ok)
    note("ホームに旧称や製品の符牒が出ていない", "理OS" not in body and "株式会社理" not in body)

    # 4. fixture無しの空台帳へ、実務量のある物件を画面と同じPOSTで登録する。
    st, body, _ = cl.get("/properties")
    note("初回設定の直後は、見本物件でなく空の物件台帳が開く",
         st == 200 and "まだ物件がありません" in body
         and "サンプル" not in body and "みなと台" not in body)
    property_form = {
        "op": "property_register",
        "property_name": "芝浦リバーサイドレジデンス 503",
        "address": "東京都港区芝浦3丁目12番8号",
        "deal_type": "sale",
        "rent_or_price": "6,980万円",
        "layout": "2LDK",
        "area": "58.42㎡",
        "built_year": "2016年3月",
        "structure": "鉄筋コンクリート造",
        "station": "JR山手線 田町駅",
        "walk_min": "8分",
        "source": "売主ヒアリング・登記事項確認待ち",
    }
    st, _body, headers = cl.post("/op", property_form)
    note("法人登録後、最初の物件を登録できる",
         st == 303 and "/properties" in headers.get("location", ""), f"status={st}")
    st, properties_body, _ = cl.get("/properties")
    note("登録した実務物件が物件画面に出る",
         st == 200 and "芝浦リバーサイドレジデンス 503" in properties_body)

    # 5. 既存顧客名簿を下見してから取り込み、登録済み物件へ接続する。
    customers_csv = (
        "氏名,メールアドレス,LINEユーザーID,備考\n"
        "佐藤 美咲,misaki.sato@example.com,LINE-DEMO-MISAKI,共働き・田町駅徒歩圏を希望\n"
        "高橋 健一,kenichi.takahashi@example.com,LINE-DEMO-KENICHI,自己資金2,000万円・入居時期相談\n"
        "伊藤 直子,naoko.ito@example.com,,売却査定からの住み替え相談\n"
    ).encode("utf-8-sig")
    st, preview, _ = cl.post_multipart(
        "/migrate/preview", {"source_tool": "既存顧客台帳（ExcelからCSV書き出し）"},
        filename="港南店_既存顧客台帳.csv", payload=customers_csv)
    token_match = __import__("re").search(r'name="token" value="([a-f0-9]+)"', preview)
    preview_ok = (st == 200 and token_match is not None
                  and all(name in preview for name in ("佐藤 美咲", "高橋 健一", "伊藤 直子"))
                  and "入る方 3 名" in preview)
    note("既存顧客3名を、書き込む前に下見できる", preview_ok, f"status={st}")
    token = token_match.group(1) if token_match else ""
    st, _body, headers = cl.post("/migrate/apply", {
        "token": token,
        "source_tool": "既存顧客台帳（ExcelからCSV書き出し）",
    })
    note("確認後に既存顧客3名を取り込める",
         st == 303 and "/customers?msg=" in headers.get("location", ""), f"status={st}")
    st, customers_body, _ = cl.get("/customers")
    import re as _re
    customer_match = _re.search(
        r"佐藤 美咲.*?name=\"customer_id\" value=\"([^\"]+)\"",
        customers_body, flags=_re.S)
    property_match = _re.search(
        r'<option value="(PROP-[^"]+)">芝浦リバーサイドレジデンス 503</option>',
        customers_body)
    note("顧客画面で、取り込んだ顧客と登録済み物件を選べる",
         st == 200 and customer_match is not None and property_match is not None)
    customer_id = customer_match.group(1) if customer_match else ""
    property_id = property_match.group(1) if property_match else ""
    st, _body, headers = cl.post("/op", {
        "op": "customer_case_create", "customer_id": customer_id,
        "property_id": property_id, "deal_type": "sale_buyer",
    })
    note("既存顧客と登録済み物件を同じ案件へ接続できる",
         st == 303 and "/case?id=" in headers.get("location", ""), f"status={st}")
    connected_location = headers.get("location", "")
    case_id = (urllib.parse.parse_qs(urllib.parse.urlparse(connected_location).query)
               .get("id") or [""])[0]
    st, connected_body, _ = cl.get("/customers")
    note("接続後は顧客の取引履歴に物件名が出る",
         st == 200 and "芝浦リバーサイドレジデンス 503" in connected_body
         and "購入する" in connected_body)

    # 6. LINE接続テストはloopbackの一覧GETだけ。送信POSTは0件であることを実測する。
    st, connection_body, _ = cl.get("/connections")
    note("接続設定に、送信しないLINE接続テストがある",
         st == 200 and "LINEの接続テスト" in connection_body
         and "メッセージは送りません" in connection_body)
    before_reads, before_sends = _ReadOnlyLineHandler.reads, _ReadOnlyLineHandler.sends
    st, result_body, _ = cl.post_json("/api/conn-test", {"kind": "harness", "params": {}})
    result = json.loads(result_body) if result_body.startswith("{") else {}
    line_ok = (st == 200 and result.get("ok") is True
               and _ReadOnlyLineHandler.reads == before_reads + 1
               and _ReadOnlyLineHandler.sends == before_sends == 0)
    note("LINE接続テストは一覧を1回読み、実送信0件で終わる", line_ok,
         f"GET={_ReadOnlyLineHandler.reads}, POST={_ReadOnlyLineHandler.sends}")

    # 7. 登録した同じ物件でマイソクを窓口型で作る（4段）
    q = {"case": case_id}
    for i, (step, fields) in enumerate([
            ("basic", {"property_type": "中古マンション", "property_name": "芝浦リバーサイドレジデンス 503",
                       "price": "6,980万円"}),
            ("place", {"address": "東京都港区芝浦3丁目12番8号", "nearest_station": "JR山手線 田町駅",
                       "access": "徒歩8分", "walk_distance_m": "640"}),
            ("spec", {"land_area": "58.42㎡", "building_area": "58.42㎡", "floor_plan": "2LDK",
                      "built": "2016年3月", "structure": "鉄筋コンクリート造"}),
            ("deal", {"torihiki_taiyo": "媒介", "title_copy": "運河を望む南東角住戸・田町駅徒歩8分"})]):
        q.update(fields)
        path = "/maisoku/new-form?" + urllib.parse.urlencode({"step": step, **q})
        st, body, _ = cl.get(path)
        n_inputs = body.count('class="ms-i"')
        # その段が聞く項目数は画面側の定義が正（こちらが埋めた数ではない）
        import serve as _srv
        expect = len(next(f for k, _t, f, _h in _srv.MAISOKU_STEPS if k == step))
        ok = st == 200 and n_inputs == expect and f"{i + 1} / 4" in body and n_inputs <= 6
        note(f"マイソク {i + 1}/4 段目（1画面{n_inputs}項目・6以下）", ok,
             f"入力欄={n_inputs}/想定{expect}")

    st, body, hd = cl.post("/maisoku/new-create", q)
    ok = st in (200, 303)
    loc = hd.get("location", "")
    doc_id = (urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("doc") or [""])[0]
    note("マイソクの下書きができる", ok, f"status={st} → {loc[:60]}")

    st, body, _ = cl.get("/maisoku")
    ok = st == 200 and "芝浦リバーサイドレジデンス 503" in body
    note("作ったマイソクが一覧に出る", ok)
    maisoku_body = body
    preview_path = loc if loc.startswith("/doc/preview?") else (
        "/doc/preview?" + urllib.parse.urlencode({"doc": doc_id}))
    preview_status, preview_body, _ = cl.get(preview_path)
    brand_ok = preview_status == 200 and _customer_facing_brand_ok(
        preview_body, "株式会社みなと不動産")
    note("作ったマイソクの帯に自社名が出る", brand_ok,
         f"preview status={preview_status}")
    note("マイソクの帯に他社名（理）が出ていない", "株式会社理" not in preview_body)

    # 画面に出る実際の書き出し導線を踏む。Excelは内部の差し込み値まで開き直す。
    import importlib.util as _importlib_util
    excel_ready = bool(_importlib_util.find_spec("openpyxl"))
    xlsx_ok = bool(doc_id)
    xlsx_detail = f"doc={doc_id or '(取得失敗)'}"
    if doc_id and excel_ready:
        xst, xraw = cl.get_bytes(
            "/case/doc/file?" + urllib.parse.urlencode({
                "doc": doc_id, "v": "1", "case": case_id,
                "customer": customer_id, "as": "xlsx"}))
        try:
            if xst != 200 or xraw[:4] != b"PK\x03\x04":
                raise ValueError(xraw.decode("utf-8", "replace")[:160])
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(xraw), data_only=False, keep_links=False)

            def named_value(name: str):
                destinations = list(wb.defined_names[name].destinations)
                sheet, coordinate = destinations[0]
                return wb[sheet][coordinate.replace("$", "")].value

            xlsx_ok = (xst == 200 and xraw[:4] == b"PK\x03\x04"
                       and named_value("ms_property_name") == "芝浦リバーサイドレジデンス 503"
                       and named_value("ms_company_name") == "株式会社みなと不動産")
            xlsx_detail = f"status={xst}, {len(xraw)} bytes"
        except Exception as exc:
            xlsx_ok = False
            xlsx_detail = f"status={xst}, {type(exc).__name__}: {exc}"
    elif doc_id:
        xlsx_ok = ("as=xlsx" not in maisoku_body
                   and ("PDFで保存（印刷）" in maisoku_body
                        or "印刷用に開く（PDF保存）" in maisoku_body))
        xlsx_detail = "openpyxlなし: Excelリンク非表示・ブラウザ印刷へfallback"
    note("端末で利用可能なExcel導線が表示され、エラーにならない", xlsx_ok, xlsx_detail)

    # LibreOfficeがある端末は直接PDF。無い端末は壊れたリンクを出さず、印刷画面を必ず出す。
    from hub_core import docgen as _docgen
    pdf_href = ("/doc/file?doc=" + urllib.parse.quote(doc_id)
                + "&amp;as=pdf") if doc_id else ""
    if doc_id and excel_ready and _docgen._find_soffice():
        pst, praw = cl.get_bytes(
            "/doc/file?" + urllib.parse.urlencode({"doc": doc_id, "as": "pdf"}))
        pdf_ok = pst == 200 and praw[:4] == b"%PDF" and pdf_href in maisoku_body
        pdf_detail = f"直接PDF status={pst}, {len(praw)} bytes"
    else:
        pdf_ok = bool(doc_id and pdf_href not in maisoku_body
                      and ("PDFで保存（印刷）" in maisoku_body
                           or "印刷用に開く（PDF保存）" in maisoku_body))
        pdf_detail = "ブラウザ印刷へfallback"
    note("端末で利用可能なPDF導線が表示され、エラーにならない", pdf_ok, pdf_detail)

    # 8. 重説を窓口型で作る（5段）
    jq = {"deal_type": "売買", "case": case_id}
    for i, (step, fields) in enumerate([
            ("torihiki", {"torihiki_keitai": "媒介"}),
            ("bukken", {"property_name": "芝浦リバーサイドレジデンス 503",
                        "address": "東京都港区芝浦3丁目12番8号", "structure": "鉄筋コンクリート造",
                        "area": "58.42㎡"}),
            ("okane", {"rent": "—"}),
            ("setsubi", {"water": "公営", "electric": "東京電力", "gas": "都市ガス",
                         "drainage": "公共下水"}),
            ("houki", {"youto": "第一種住居地域", "kenpei_yoseki": "60/200",
                       "flood": "浸水想定0.5m未満", "landslide": "指定なし"})]):
        jq.update(fields)
        path = "/juusetsu/new?" + urllib.parse.urlencode({"step": step, **jq})
        st, body, _ = cl.get(path)
        ok = st == 200 and f"{i + 1} / 5" in body
        note(f"重説 {i + 1}/5 段目", ok)

    st, body, _ = cl.get("/juusetsu/new?" + urllib.parse.urlencode({"step": "houki", **jq}))
    ok = "株式会社みなと不動産" in body and "記名" in body
    note("最終段に「誰の名義で出るか」が書いてある", ok)

    st, body, hd = cl.post("/juusetsu/new/create", jq)
    ok = st in (200, 303)
    note("重要事項説明書の下書きができる", ok, f"status={st}")

    # 9. 業者情報 → 履歴 → 戻す
    st, body, _ = cl.get("/profile")
    note("業者情報の画面が開く", st == 200 and "株式会社みなと不動産" in body)

    st, _b, _h = cl.post("/profile/save", {
        "name": "株式会社みなと不動産", "license_no": "東京都知事（2）第12345号",
        "address": "東京都港区芝浦3丁目12番8号", "tel": "03-1234-5678", "brand_color": "#2e5a87",
        "display_font": "rounded"})
    note("業者情報を保存できる", st in (200, 303), f"status={st}")

    # もう一度変えてから、実際に「戻す」を押して元に戻ることを確かめる
    st, _b, _h = cl.post("/profile/save", {
        "name": "株式会社みなと不動産", "license_no": "東京都知事（2）第12345号",
        "address": "東京都港区海岸3丁目21番35号", "tel": "03-9876-5432", "brand_color": "#8a2e4d",
        "display_font": "gothic"})
    st, body, _ = cl.get("/brand/history")
    ok = st == 200 and body.count("版 ") >= 2 and "この状態に戻す" in body
    note("変更の履歴が2件以上残り、戻すボタンが出る", ok)

    import re as _re
    m = _re.search(r'name="version" value="(\d+)"', body)
    if m:
        st, _b, hdr = cl.post("/brand/restore", {"version": m.group(1)})
        note("「この状態に戻す」を押せる", st in (200, 303), f"status={st}")
        st, prof, _ = cl.get("/profile")
        note("戻したあと、前の住所に戻っている", "東京都港区芝浦3丁目12番8号" in prof)
        st, hist, _ = cl.get("/brand/history")
        note("戻した操作も履歴に残る（履歴が消えない）",
             "に戻した" in hist and hist.count("版 ") >= 3)
    else:
        note("「この状態に戻す」を押せる", False, "ボタンが見つからない")

    # 10. 一覧から仕事が始められる
    st, body, _ = cl.get("/properties")
    ok = st == 200 and "マイソクを作る" in body and "重要事項説明書を作る" in body
    note("物件の一覧から、その物件の仕事を始められる", ok)

    st, body, _ = cl.get("/leads")
    ok = st == 200 and ("お返事の下書きを作る" in body or "まとめて取り込む" in body)
    note("お客様の問い合わせの画面から次の一手に進める", ok)

    # 11. お金の計算
    st, body, _ = cl.get("/keisan?price=69800000")
    ok = st == 200 and "69,800,000" in body
    note("自分の金額でお金の計算ができる", ok)

    # 12. 監査画面と出力。暗号化した本体と復旧キーも別々に取得して実際に検証する。
    st, audit_body, _ = cl.get("/audit")
    note("監査画面で法人・物件・顧客・書類の操作記録を確認できる",
         st == 200 and "監査ログは正常です" in audit_body
         and all(label in audit_body for label in
                 ("物件を登録", "顧客名簿を取り込み", "顧客と物件を接続", "LINE接続を確認")))
    from hub_core import backup as _backup
    files: dict[str, bytes] = {}
    if _backup.portable_crypto_available():
        st, encrypted, backup_headers = cl.post_bytes("/api/backup", {})
        kst, key_document, key_headers = cl.post_bytes("/api/backup/recovery-key", {})
        backup_ok = (st == 200 and encrypted.startswith(_backup.PORTABLE_MAGIC)
                     and not encrypted.startswith(_backup.MAGIC)
                     and backup_headers.get("cache-control") == "no-store"
                     and kst == 200 and key_headers.get("cache-control") == "no-store")
        detail = f"AES-GCM backup={st}, key={kst}, encrypted={len(encrypted)} bytes"
        if backup_ok:
            try:
                recovery_key = _backup.parse_recovery_key_document(key_document)
                files = _backup.read_portable_backup(encrypted, recovery_key)
                backup_ok = ("株式会社みなと不動産".encode("utf-8") not in encrypted
                             and recovery_key not in files.values())
            except _backup.BackupError as exc:
                backup_ok = False
                detail = f"AES-GCM復号検証失敗: {exc}"
        note("AES-256-GCMで暗号化し、復旧キーを別ファイルで保存できる", backup_ok, detail)
        note("別取得した復旧キーで会社情報と監査鍵を復元できる",
             "auth/company.json" in files and "keys/audit_chain.key" in files,
             f"検証済み={len(files)}ファイル")
    else:
        st, body, _ = cl.get("/connections")
        hidden = (st == 200 and "標準暗号を利用できないため、バックアップ機能は停止中です" in body
                  and 'action="/api/backup"' not in body
                  and 'action="/api/backup/recovery-key"' not in body)
        note("標準暗号が無い端末では弱い方式へ落とさず導線を隠す", hidden)
        bst, _payload, _headers = cl.post_bytes("/api/backup", {})
        kst, _key, _headers = cl.post_bytes("/api/backup/recovery-key", {})
        note("標準暗号が無い端末ではバックアップ本体も復旧キーも取得できない",
             bst == 503 and kst == 503, f"backup={bst}, key={kst}")

    # 13. 接続設定にFAXの口がある
    st, body, _ = cl.get("/connections")
    note("接続設定にFAX送信の口がある", st == 200 and "FAX送信" in body)

    return all(ok for _t, ok, _d in STEPS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="あいのて fresh一周（外部送信なし）")
    parser.add_argument(
        "--data-dir", type=Path,
        help="検証後も残す空の保存先。存在する非空ディレクトリは上書きせず拒否します。",
    )
    args = parser.parse_args(argv)
    owned_root = args.data_dir is None
    if owned_root:
        root = Path(tempfile.mkdtemp(prefix="ainote_demo_"))
        data = root / "out"
        data.mkdir(parents=True)
    else:
        data = args.data_dir.expanduser().resolve()
        if data.exists() and (not data.is_dir() or any(data.iterdir())):
            print(f"保存先は空のディレクトリを指定してください: {data}")
            return 2
        data.mkdir(parents=True, exist_ok=True)
        root = data.parent
    port = free_port()
    line_port = free_port()
    _ReadOnlyLineHandler.reads = 0
    _ReadOnlyLineHandler.sends = 0
    line_server = ThreadingHTTPServer((HOST, line_port), _ReadOnlyLineHandler)
    line_thread = threading.Thread(target=line_server.serve_forever, daemon=True)
    line_thread.start()
    here = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        server_command = [sys.executable, "--serve-for-test",
                          "--data-dir", str(data), "--port", str(port), "--no-browser"]
    else:
        python_cmd = [sys.executable] + (["-S"] if sys.flags.no_site else [])
        server_command = python_cmd + [str(here / "serve.py"),
                                       "--data-dir", str(data), "--port", str(port)]
    proc = subprocess.Popen(
        server_command,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "RI_HUB_ONBOARD": "1",
             "RI_HUB_DISABLE_SOFFICE": "1",
             "RI_HUB_KEYS_DIR": str(root / "keys"),
             "RI_HUB_AUDIT_KEY_PATH": str(root / "keys" / "audit_chain.key"),
             "LINE_HARNESS_API_URL": f"http://{HOST}:{line_port}",
             "LINE_HARNESS_API_KEY": "walkthrough-read-only"})
    try:
        if not wait_up(port):
            print("サーバが起動しませんでした")
            return 1
        print(f"\n=== あいのて fresh一周（fixture 0件・外部送信 0件）===\n保存先: {data}\n")
        ok = run(port, data)
        bad = [t for t, o, _d in STEPS if not o]
        print(f"\n{len(STEPS) - len(bad)} / {len(STEPS)} 通過")
        if bad:
            print("転んだところ:")
            for t in bad:
                print("  -", t)
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        line_server.shutdown()
        line_server.server_close()
        line_thread.join(timeout=3)
        if owned_root:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
