#!/usr/bin/env python3
"""Create the supported AINOTE venv and install the exact release pins."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = (ROOT / "requirements.txt", ROOT / "requirements-office.txt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args(argv)
    target = args.venv.expanduser().resolve()
    if target.exists():
        print("BLOCKED: --venv must name a new path", file=sys.stderr)
        return 2
    if any(not path.is_file() for path in REQUIREMENTS):
        print("BLOCKED: pinned requirement files are missing", file=sys.stderr)
        return 2
    if sys.version_info < (3, 12):
        print("BLOCKED: Python 3.12 or newer is required", file=sys.stderr)
        return 2
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
    python = target / "bin" / "python"
    command = [
        str(python), "-m", "pip", "install", "--isolated", "--no-input",
        "--disable-pip-version-check",
    ]
    if args.wheelhouse:
        wheelhouse = args.wheelhouse.expanduser().resolve()
        if not wheelhouse.is_dir():
            print("BLOCKED: wheelhouse is not a directory", file=sys.stderr)
            shutil.rmtree(target, ignore_errors=True)
            return 2
        command.extend(("--no-index", "--find-links", str(wheelhouse)))
    for requirement in REQUIREMENTS:
        command.extend(("--requirement", str(requirement)))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        return completed.returncode
    payload = {
        "schema": "ainote.install-result/v1",
        "python": str(python),
        "requirements": [path.name for path in REQUIREMENTS],
        "isolated_pip": True,
        "pythonpath_unset": True,
    }
    print("AINOTE_INSTALL_OK " + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
