"""hub_core/windows_ocr.py — 無料・ローカルの画像OCR（Windows.Media.Ocr / Windows Runtime OCR）。

macOS の Vision と対になる Windows 版。Windows 10/11 に**標準搭載**の OCR エンジン
（Windows.Media.Ocr.OcrEngine・無料・端末内・追加インストール不要・日本語言語パックがあれば日本語）を
PowerShell 経由で呼ぶ。クラウドに送らない＝主権を保つ。

注意（正直に）: この実装は Windows 実機での検証が未（開発環境が macOS のため）。PowerShell の WinRT
async 呼出は環境差があるため、Windows 実機での動作確認が必要。日本語は「日本語 OCR 言語パック」
（設定→時刻と言語→言語→日本語→オプション→OCR）が要る場合がある。無ければ英数字のみ。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

# Windows.Media.Ocr を PowerShell から叩くスクリプト（WinRT の await を .GetAwaiter().GetResult() で同期化）。
_PS = r'''
param([string]$ImagePath)
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
  # WinRT async を同期で待つヘルパ
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $t) { $m = $asTask.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(); $task.Result }

  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime] | Out-Null
  [Windows.Media.Ocr.OcrEngine, Windows.Media, ContentType=WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("ja"))
  }
  if ($null -eq $engine) { [Console]::Error.WriteLine("no OCR engine (language pack?)"); exit 2 }

  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  foreach ($line in $result.Lines) { [Console]::Out.WriteLine($line.Text) }
} catch {
  [Console]::Error.WriteLine($_.Exception.Message); exit 1
}
'''


# bounding box つきで読む版（表組みの grid 復元用・座標は正規化 [0,1]・原点は左上=Windows流儀）。
# ocr_geom は範囲重なりで行判定するため y の向き（左上/左下）に依存しない＝同じ幾何ロジックで動く。
_PS_BOXES = r'''
param([string]$ImagePath)
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $t) { $m = $asTask.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(); $task.Result }

  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime] | Out-Null
  [Windows.Media.Ocr.OcrEngine, Windows.Media, ContentType=WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $W = [double]$bitmap.PixelWidth; $H = [double]$bitmap.PixelHeight
  if ($W -le 0 -or $H -le 0) { exit 3 }

  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("ja")) }
  if ($null -eq $engine) { [Console]::Error.WriteLine("no OCR engine"); exit 2 }

  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  foreach ($line in $result.Lines) {
    $minX=1e18; $minY=1e18; $maxX=0.0; $maxY=0.0
    foreach ($word in $line.Words) {
      $r = $word.BoundingRect
      if ($r.X -lt $minX) { $minX = $r.X }
      if ($r.Y -lt $minY) { $minY = $r.Y }
      if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
      if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
    }
    if ($maxX -le 0) { continue }
    $nx = $minX / $W; $ny = $minY / $H; $nw = ($maxX - $minX) / $W; $nh = ($maxY - $minY) / $H
    [Console]::Out.WriteLine(("{0}`t{1}`t{2}`t{3}`t{4}" -f $nx, $ny, $nw, $nh, $line.Text))
  }
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
'''


def available() -> bool:
    """Windows かつ PowerShell が使えるか。"""
    if platform.system() != "Windows":
        return False
    return bool(shutil.which("powershell") or shutil.which("pwsh"))


def _ps_exe() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def _script_path(name: str = "windows_ocr.ps1", body: str = _PS) -> Path:
    d = Path(os.environ.get("RI_OS_CACHE", str(Path.home() / ".ri-os" / "cache")))
    d.mkdir(parents=True, exist_ok=True)
    sp = d / name
    if not sp.is_file() or sp.read_text(encoding="utf-8") != body:
        sp.write_text(body, encoding="utf-8")
    return sp


def ocr_image(image_path: str, *, timeout: float = 60.0) -> str:
    """画像ファイル → 抽出テキスト（Windows.Media.Ocr・ローカル）。失敗時は空文字。"""
    if not available() or not Path(image_path).is_file():
        return ""
    try:
        out = subprocess.run(
            [_ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_script_path()), "-ImagePath", str(Path(image_path).resolve())],
            capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def ocr_pdf(pdf_path: str, *, dpi: int = 150, max_pages: int = 5, timeout: float = 90.0) -> str:
    """画像PDF → テキスト（pdftoppm で画像化して各ページ OCR）。pdftoppm が要る（poppler）。"""
    if not available() or not Path(pdf_path).is_file() or not shutil.which("pdftoppm"):
        return ""
    texts = []
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "page")
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), pdf_path, prefix],
                           capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return ""
        for pg in sorted(Path(td).glob("page*.png"))[:max_pages]:
            texts.append(ocr_image(str(pg), timeout=timeout))
    return "\n".join(t for t in texts if t)


def ocr_any(path: str, **kw) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return ocr_pdf(path, **{k: v for k, v in kw.items() if k in ("dpi", "max_pages", "timeout")})
    return ocr_image(path, **{k: v for k, v in kw.items() if k in ("timeout",)})


def _parse_boxes_tsv(tsv: str) -> list:
    """PowerShell の TSV 出力（nx\\tny\\tnw\\tnh\\ttext）→ [{text,x,y,w,h}]。"""
    out = []
    for line in (tsv or "").splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        try:
            x, y, w, h = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        text = parts[4].strip()
        if text:
            out.append({"text": text, "x": x, "y": y, "w": w, "h": h})
    return out


def ocr_image_boxes(image_path: str, *, timeout: float = 60.0) -> list:
    """画像 → [{text,x,y,w,h}]（正規化座標・原点左上）。表組みの grid 復元用。失敗時は空list。"""
    if not available() or not Path(image_path).is_file():
        return []
    try:
        out = subprocess.run(
            [_ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_script_path("windows_ocr_boxes.ps1", _PS_BOXES)),
             "-ImagePath", str(Path(image_path).resolve())],
            capture_output=True, text=True, timeout=timeout)
        return _parse_boxes_tsv(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def ocr_pdf_boxes(pdf_path: str, *, dpi: int = 150, page: int = 1, timeout: float = 90.0) -> list:
    """画像PDFの指定ページ → boxes（pdftoppm で当該ページ画像化）。"""
    if not available() or not Path(pdf_path).is_file() or not shutil.which("pdftoppm"):
        return []
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "page")
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
                            pdf_path, prefix], capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        pngs = sorted(Path(td).glob("page*.png"))
        return ocr_image_boxes(str(pngs[0]), timeout=timeout) if pngs else []
