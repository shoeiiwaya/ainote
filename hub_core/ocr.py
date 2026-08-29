"""M-ocr — 資料の読取（Wave4 G2・BYO-LLM vision→出典束縛フレーム）。

登記簿/図面/概要書等の画像・PDFから項目を読み取り、**extract.py の出典束縛フレーム**に載せる。
思想（重要）:
- **OCRはドラフト補助であって真実ではない**。読み取った値は必ず extract.save_extraction を通し、
  page 出典・原本 sha256 束縛・宅建士の承認ゲートに乗る（承認前は台帳に流れない）。
- 読取エンジン= **BYO-LLM vision**（ローカルllava / 外部Claude/GPT vision・ユーザー自身のキー）。
  有料OCR API（Google/Azure/AWS）は不要＝追加課金¥0。**未接続時は正直に「手動入力へ」**。
- サーバ自身は実LLMを呼ばない原則を保つ＝vision呼出は BYO provider（差替え式・モック可）に委譲。
- stdlib only（provider抽象・実HTTP呼出は provider 実装側）。
"""
from __future__ import annotations

import json


class OcrError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class VisionProvider:
    """読取エンジンの抽象。実装は read(image_bytes, prompt)->str（JSON文字列）を返す。connected で接続状態。"""
    name = "abstract"
    connected = False

    def read(self, image_bytes: bytes, prompt: str) -> str:
        raise NotImplementedError


class MockVision(VisionProvider):
    """未接続の既定。**実読取をしない**（BYO vision未設定時）＝空を返し「手動入力へ」を促す。"""
    name = "mock"
    connected = False

    def read(self, image_bytes: bytes, prompt: str) -> str:
        return "[]"


# 読取プロンプト（構造化抽出を要求）。値は自己申告のpage出典つきで返させる。
_OCR_PROMPT = (
    "この不動産関連書類の画像から、重要項目を抽出してください。"
    "出力は JSON 配列のみ。各要素は {\"key\": 項目名, \"value\": 値, \"page\": ページ番号(1始まりの整数)}。"
    "値が読み取れない項目は含めないでください。推測や創作はせず、画像に明記された値だけを返してください。")


class LocalOCR(VisionProvider):
    """無料・ローカルの既定OCR（OS標準を自動選択: macOS Vision / Windows.Media.Ocr / tesseract）。
    BYO不要でそのまま写真を読める。読取全文を1フィールドで返す（出典束縛フレームに載る・宅建士が確認）。
    クロスプラットフォーム: 利用者のOSに合わせて local_ocr が最適な無料エンジンを選ぶ。"""

    @property
    def name(self) -> str:  # type: ignore[override]
        from hub_core import local_ocr
        eng = local_ocr.engine()
        return eng if eng != "none" else "local"

    @property
    def connected(self) -> bool:
        from hub_core import local_ocr
        return local_ocr.available()

    def read(self, image_bytes: bytes, prompt: str) -> str:
        import json as _j
        import os as _os
        import tempfile as _tf
        from hub_core import local_ocr
        is_pdf = image_bytes[:5] == b"%PDF-"
        suf = ".pdf" if is_pdf else ".png"
        f = _tf.NamedTemporaryFile(suffix=suf, delete=False)
        try:
            f.write(image_bytes); f.close()
            text = local_ocr.ocr_any(f.name)
        finally:
            try:
                _os.unlink(f.name)
            except OSError:
                pass
        text = (text or "").strip()
        if not text:
            return "[]"
        return _j.dumps([{"key": "読取全文", "value": text, "page": 1}], ensure_ascii=False)


# 後方互換: 旧名 MacOSVision は LocalOCR のエイリアス（既存の呼出し/テストを壊さない）。
MacOSVision = LocalOCR


def default_provider() -> VisionProvider:
    """無料ローカルOCR（macOS Vision / Windows.Media.Ocr / tesseract）が使えれば既定に。
    無ければMock（手動入力へ促す）。利用者のOSを問わず"箱を開けて写真が読める"を担保する。"""
    m = LocalOCR()
    return m if m.connected else MockVision()


def _parse_fields(raw: str) -> list[dict]:
    """vision応答（JSON文字列）→ extract 用の fields。壊れた応答は空（捏造しない）。"""
    raw = (raw or "").strip()
    # コードフェンス等を剥がす
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        raw = raw[raw.find("["):] if "[" in raw else raw
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        key = str(d.get("key") or "").strip()
        value = str(d.get("value") or "").strip()
        page = d.get("page")
        if key and value and isinstance(page, int) and not isinstance(page, bool) and page >= 1:
            out.append({"key": key, "value": value, "page": page})
    return out


def ocr_document(data_dir, source_rel: str, actor: str, *,
                 provider: VisionProvider | None = None) -> dict:
    """原本をBYO-LLM visionで読み取り→extract出典束縛フレームへdraft保存。
    未接続(Mock/読取ゼロ)なら『手動入力へ』の正直な結果を返す（台帳に勝手に流さない）。"""
    from pathlib import Path
    from hub_core import extract as _ex
    root = Path(data_dir).resolve()
    src = (root / source_rel).resolve()
    if not src.is_relative_to(root) or not src.is_file():
        raise OcrError(404, f"原本が見つかりません: {source_rel}")
    prov = provider or default_provider()
    if not getattr(prov, "connected", False):
        return {"ok": True, "connected": False, "fields": 0,
                "note": "BYO-LLM vision が未接続です。/setup でモデルを設定するか、手動で入力してください"
                        "（OCRはドラフト補助・値は出典束縛と宅建士承認を通ります）。"}
    try:
        raw = prov.read(src.read_bytes(), _OCR_PROMPT)
    except Exception as e:
        raise OcrError(502, f"読取に失敗しました（{type(e).__name__}）。手動入力へ切替えてください。")
    fields = _parse_fields(raw)
    if not fields:
        return {"ok": True, "connected": True, "fields": 0,
                "note": "読み取れる項目がありませんでした（捏造しません）。手動で入力してください。"}
    # extract の出典束縛フレームへ（page必須・sha256束縛・承認ゲートは extract 側）
    res = _ex.save_extraction(data_dir, source_rel, fields, actor,
                              extractor="ocr:" + prov.name)
    return {"ok": True, "connected": True, "fields": res["fields"], "path": res["path"],
            "note": "OCRはドラフトです。値は出典（ページ）に束縛され、宅建士の承認まで台帳に流れません。"}
