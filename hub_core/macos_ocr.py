"""hub_core/macos_ocr.py — 無料・ローカルの画像OCR（macOS Vision framework）。

なぜ必要か: あいのてのOCRは BYO-LLM vision 前提（利用者がモデルを繋ぐ・既定はモック=空）で、
**箱を開けてすぐ無料で写真を読める道が無かった**。macOS には OS標準の Vision（VNRecognizeTextRequest）が
入っており、**無料・端末内・インストール不要・日本語対応**。これを既定の無料ローカルOCRにする。

- 配布app: 事前コンパイル済みarm64 helperを同梱し、画像/PDFをVision+PDFKitで読む。
- local Web/source: ZIPに同梱したSwift helper sourceをmacOS標準の`/usr/bin/swift`
  で実行し、PDFもPDFKitで読む。Homebrew/Popplerは要求しない。
- OCRは**下書き補助であって真実でない**。読み取り値は extract の出典束縛フレームを通し宅建士が確認。
- すべてローカル・ネットワークなし。配布appはhelper欠落時にfail-closedし、開発時のみswiftを許可する。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Vision framework を叩く Swift スクリプト（日本語＋英語・高精度）。
_SWIFT = r'''
import Vision
import AppKit
import Foundation

let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot load image\n".data(using: .utf8)!); exit(1)
}
let sem = DispatchSemaphore(value: 0)
let req = VNRecognizeTextRequest { req, _ in
    if let obs = req.results as? [VNRecognizedTextObservation] {
        for o in obs { if let t = o.topCandidates(1).first { print(t.string) } }
    }
    sem.signal()
}
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP", "en-US"]
req.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
} catch {
    FileHandle.standardError.write("perform failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
sem.wait()
'''


# bounding box つきで読む Swift スクリプト（表組みの grid 復元に使う・座標は正規化 [0,1]・原点は左下）。
_SWIFT_BOXES = r'''
import Vision
import AppKit
import Foundation

let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot load image\n".data(using: .utf8)!); exit(1)
}
let sem = DispatchSemaphore(value: 0)
let req = VNRecognizeTextRequest { req, _ in
    var arr: [[String: Any]] = []
    if let obs = req.results as? [VNRecognizedTextObservation] {
        for o in obs {
            if let t = o.topCandidates(1).first {
                let b = o.boundingBox
                arr.append(["text": t.string, "x": b.origin.x, "y": b.origin.y,
                            "w": b.size.width, "h": b.size.height])
            }
        }
    }
    if let data = try? JSONSerialization.data(withJSONObject: arr),
       let s = String(data: data, encoding: .utf8) { print(s) }
    sem.signal()
}
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP", "en-US"]
req.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
} catch {
    FileHandle.standardError.write("perform failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
sem.wait()
'''


_HELPER_NAME = "ainote-vision-ocr"


def _bundled_helper() -> Path | None:
    """PyInstallerが同梱した署名対象helperだけを返す。開発repo上の任意実行物は拾わない。"""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    helper = Path(bundle_root) / _HELPER_NAME
    if helper.is_file() and os.access(helper, os.X_OK):
        return helper
    return None


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _source_helper_script() -> Path | None:
    """Return the source-ZIP-owned PDFKit/Vision helper outside frozen apps."""
    if _frozen():
        return None
    script = Path(__file__).resolve().parents[1] / "packaging" / "vision_ocr.swift"
    return script if script.is_file() else None


def _helper_command_prefix() -> list[str] | None:
    helper = _bundled_helper()
    if helper is not None:
        return [str(helper)]
    script = _source_helper_script()
    swift = shutil.which("swift")
    if script is not None and swift is not None:
        return [swift, str(script)]
    return None


def available() -> bool:
    """配布appは同梱binary、source ZIPは同梱source + Swiftを必須にする。"""
    if platform.system() != "Darwin":
        return False
    if _frozen():
        return _bundled_helper() is not None
    return _helper_command_prefix() is not None


def _run_helper(path: str, *, boxes: bool = False, dpi: int = 150,
                page: int = 1, max_pages: int = 5, timeout: float = 90.0):
    prefix = _helper_command_prefix()
    if prefix is None or not Path(path).is_file():
        return [] if boxes else ""
    argv = list(prefix)
    if boxes:
        argv.extend(["--boxes", "--page", str(page)])
    else:
        argv.extend(["--max-pages", str(max_pages)])
    argv.extend(["--dpi", str(dpi), str(path)])
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return [] if boxes else ""
        if not boxes:
            return out.stdout
        import json as _json
        data = _json.loads(out.stdout or "[]")
        return data if isinstance(data, list) else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return [] if boxes else ""


def _script_path(name: str = "vision_ocr.swift", body: str = _SWIFT) -> Path:
    """Swiftスクリプトを固定パスに一度だけ書き出す（毎回書かない）。"""
    d = Path(os.environ.get("RI_OS_CACHE", str(Path.home() / ".ri-os" / "cache")))
    d.mkdir(parents=True, exist_ok=True)
    sp = d / name
    if not sp.is_file() or sp.read_text(encoding="utf-8") != body:
        sp.write_text(body, encoding="utf-8")
    return sp


def ocr_image_boxes(image_path: str, *, timeout: float = 60.0) -> list:
    """画像 → [{text,x,y,w,h}]（正規化座標・原点左下）。表組みの grid 復元用。失敗時は空list。"""
    import json as _j
    if not available() or not Path(image_path).is_file():
        return []
    if _helper_command_prefix() is not None:
        return _run_helper(image_path, boxes=True, timeout=timeout)
    try:
        out = subprocess.run(
            ["swift", str(_script_path("vision_ocr_boxes.swift", _SWIFT_BOXES)), str(image_path)],
            capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return []
        data = _j.loads(out.stdout or "[]")
        return data if isinstance(data, list) else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def ocr_pdf_boxes(pdf_path: str, *, dpi: int = 150, page: int = 1, timeout: float = 90.0) -> list:
    """画像PDFの指定ページ → boxes。配布appは同梱helperのPDFKit経路を使う。"""
    if not available() or not Path(pdf_path).is_file():
        return []
    if _helper_command_prefix() is not None:
        return _run_helper(pdf_path, boxes=True, dpi=dpi, page=page, timeout=timeout)
    if not shutil.which("pdftoppm"):
        return []
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "page")
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
                            pdf_path, prefix], capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        pngs = sorted(Path(td).glob("page*.png"))
        if not pngs:
            return []
        return ocr_image_boxes(str(pngs[0]), timeout=timeout)


def ocr_image(image_path: str, *, timeout: float = 60.0) -> str:
    """画像ファイル → 抽出テキスト（macOS Vision・ローカル）。失敗時は空文字。"""
    if not available() or not Path(image_path).is_file():
        return ""
    if _helper_command_prefix() is not None:
        return _run_helper(image_path, timeout=timeout)
    try:
        out = subprocess.run(
            ["swift", str(_script_path()), str(image_path)],
            capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def ocr_pdf(pdf_path: str, *, dpi: int = 150, max_pages: int = 5, timeout: float = 90.0) -> str:
    """画像PDF → 抽出テキスト。配布appは同梱helperのPDFKit経路で外部コマンド不要。"""
    if not available() or not Path(pdf_path).is_file():
        return ""
    if _helper_command_prefix() is not None:
        return _run_helper(pdf_path, dpi=dpi, max_pages=max_pages, timeout=timeout)
    if not shutil.which("pdftoppm"):
        return ""
    texts = []
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "page")
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), pdf_path, prefix],
                           capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return ""
        pages = sorted(Path(td).glob("page*.png"))[:max_pages]
        for pg in pages:
            texts.append(ocr_image(str(pg), timeout=timeout))
    return "\n".join(t for t in texts if t)


def ocr_any(path: str, **kw) -> str:
    """拡張子で画像/PDFを振り分けてOCR。無料ローカルの唯一の入口。"""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return ocr_pdf(path, **{k: v for k, v in kw.items() if k in ("dpi", "max_pages", "timeout")})
    return ocr_image(path, **{k: v for k, v in kw.items() if k in ("timeout",)})
