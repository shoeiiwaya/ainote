"""hub_core/local_ocr.py — 無料・ローカルOCRのクロスプラットフォーム入口。

OS標準の無料OCRを自動選択する（BYO不要・箱を開けて写真が読める）:
- macOS → Vision framework（macos_ocr）
- Windows → Windows.Media.Ocr（windows_ocr）
- どちらも不可 → tesseract があれば tesseract（brew/apt で入る・任意）

クラウドに送らない＝主権を保つ。どれも無ければ空を返す（捏造しない・UIは手動入力へ）。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from hub_core import macos_ocr, windows_ocr


def _tesseract_available() -> bool:
    return bool(shutil.which("tesseract"))


def _tesseract_image(image_path: str, *, timeout: float = 60.0) -> str:
    try:
        # 日本語+英語（jpn 学習データがあれば）。無ければ eng のみでも動く。
        langs = "jpn+eng"
        chk = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10)
        if "jpn" not in (chk.stdout or ""):
            langs = "eng"
        out = subprocess.run(["tesseract", str(image_path), "stdout", "-l", langs],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _tesseract_pdf(pdf_path: str, *, dpi: int = 150, max_pages: int = 5, timeout: float = 90.0) -> str:
    if not shutil.which("pdftoppm"):
        return ""
    texts = []
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), pdf_path, str(Path(td) / "page")],
                           capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return ""
        for pg in sorted(Path(td).glob("page*.png"))[:max_pages]:
            texts.append(_tesseract_image(str(pg), timeout=timeout))
    return "\n".join(t for t in texts if t)


def engine() -> str:
    """今使える無料ローカルOCRエンジン名（'macos-vision'/'windows-ocr'/'tesseract'/'none'）。"""
    if macos_ocr.available():
        return "macos-vision"
    if windows_ocr.available():
        return "windows-ocr"
    if _tesseract_available():
        return "tesseract"
    return "none"


def available() -> bool:
    return engine() != "none"


def ocr_boxes(path: str, *, page: int = 1, **kw) -> list:
    """画像/PDF → [{text,x,y,w,h}]（正規化座標・グリッド復元用）。macOS/Windowsのみ。
    tesseract は本実装では boxes 非対応（hOCR未パース）＝空を返しテキスト経路にフォールバック。"""
    eng = engine()
    p = Path(path)
    is_pdf = p.suffix.lower() == ".pdf"
    if eng == "macos-vision":
        return macos_ocr.ocr_pdf_boxes(path, page=page, **kw) if is_pdf else macos_ocr.ocr_image_boxes(path, **kw)
    if eng == "windows-ocr":
        return windows_ocr.ocr_pdf_boxes(path, page=page, **kw) if is_pdf else windows_ocr.ocr_image_boxes(path, **kw)
    return []


def ocr_any(path: str, **kw) -> str:
    """画像/PDF → テキスト（OS標準の無料ローカルOCRを自動選択）。無ければ空。"""
    eng = engine()
    if eng == "macos-vision":
        return macos_ocr.ocr_any(path, **kw)
    if eng == "windows-ocr":
        return windows_ocr.ocr_any(path, **kw)
    if eng == "tesseract":
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            return _tesseract_pdf(path, **{k: v for k, v in kw.items() if k in ("dpi", "max_pages", "timeout")})
        return _tesseract_image(path, **{k: v for k, v in kw.items() if k in ("timeout",)})
    return ""
