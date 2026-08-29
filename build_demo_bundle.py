#!/usr/bin/env python3
"""Build the canonical local-Web source ZIP from an explicit runtime allowlist."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

from distribution_manifest import DEMO_FILES


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "dist" / "ainote-local-web-source.zip"
BUNDLE_ROOT = "ainote-local-web"
PRODUCT_LICENSE_FILE = "LICENSE"
BUNDLE_FILES = tuple(dict.fromkeys((*DEMO_FILES, PRODUCT_LICENSE_FILE)))
_LICENSE_PLACEHOLDER_RE = re.compile(
    rb"(?i)\b(?:TODO|TBD|REPLACE_ME|CHOOSE A LICENSE)\b|<copyright holders?>")


def bundle_files(root: Path = ROOT) -> list[Path]:
    """Return regular runtime files only; repository state never influences the list."""
    root = root.resolve()
    paths = [root / rel for rel in BUNDLE_FILES]
    missing = [rel for rel, path in zip(BUNDLE_FILES, paths) if not path.exists()]
    if missing:
        raise FileNotFoundError("配布必須ファイルがありません: " + ", ".join(missing))
    for rel, path in zip(BUNDLE_FILES, paths):
        if path.is_symlink():
            raise ValueError(f"配布ファイルにsymlinkは使えません: {rel}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"配布ファイルがプロジェクト外を参照しています: {rel}")
        if not path.is_file():
            raise ValueError(f"配布対象が通常ファイルではありません: {rel}")
    license_payload = (root / PRODUCT_LICENSE_FILE).read_bytes()
    if (len(license_payload.strip()) < 100
            or _LICENSE_PLACEHOLDER_RE.search(license_payload)):
        raise ValueError(
            "確定した製品LICENSEが必要です（未選択・未完成のまま配布できません）")
    return paths


def _zip_info(name: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (0o100000 | mode) << 16
    return info


def build_bundle(output: Path, root: Path = ROOT) -> Path:
    root = root.resolve()
    files = bundle_files(root)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []

    fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp",
                                    dir=output.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for path in files:
                rel = path.relative_to(root).as_posix()
                payload = path.read_bytes()
                manifest.append(f"{hashlib.sha256(payload).hexdigest()}  {rel}")
                archive.writestr(
                    _zip_info(f"{BUNDLE_ROOT}/{rel}", executable=rel.endswith(".sh")),
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
            manifest_body = ("\n".join(manifest) + "\n").encode("utf-8")
            archive.writestr(
                _zip_info(f"{BUNDLE_ROOT}/MANIFEST.sha256"),
                manifest_body,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
    return output


def matches_current_bundle(path: Path, root: Path = ROOT) -> bool:
    if not path.is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="ainote_bundle_check_") as td:
        expected = build_bundle(Path(td) / path.name, root)
        return path.read_bytes() == expected.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="検査済みファイルだけを含む、あいのてlocal Web source ZIPを作ります。")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true",
                        help="既存ZIPが現在のソースと一致するかだけ確認します")
    args = parser.parse_args()
    if args.check:
        try:
            matches = matches_current_bundle(args.output)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 1
        if matches:
            print(f"bundle matches allowlist: {args.output}")
            return 0
        print(f"bundle missing or stale: {args.output}")
        return 1
    try:
        output = build_bundle(args.output)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(f"built {output} ({len(bundle_files())} files + MANIFEST.sha256)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
