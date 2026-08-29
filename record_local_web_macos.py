#!/usr/bin/env python3
"""Create artifact-owned clean-room evidence for the local-Web source ZIP.

The recorder never executes the checkout that launched it.  It safely extracts
the supplied ZIP into a private temporary directory, verifies its manifest and
that the recorder bytes match the artifact, then runs every probe from that
extraction.  Raw runtime observations are strict JSONL so an offline verifier
can reject contradictory observations instead of looking for positive words.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
ARTIFACT_NAME = "ainote-local-web-source.zip"
ARTIFACT_ROOT = "ainote-local-web"
MANIFEST_NAME = "MANIFEST.sha256"
SCHEMA = "ainote.release-evidence/v1"
CONTRACT_VERSION = "ainote.local-web-clean-room/v2"
RUNTIME_SCHEMA = "ainote.runtime-probe/v1"
ISOLATION_SCHEMA = "ainote.isolation-probe/v1"
MCP_POSTCONDITION_SCHEMA = "ainote.mcp-postcondition/v1"
MAX_ARCHIVE_FILES = 4096
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
VERIFIED_MACOS_VERSIONS = ("26.4", "26.6.2")
SECRET_ENV_MARKERS = (
    "API_KEY", "TOKEN", "PASSWORD", "SECRET", "SMTP", "FAX", "LINE_",
    "PRS_", "ANTHROPIC", "OPENAI", "PROXY",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _jsonl(items: list[dict]) -> bytes:
    return b"".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for item in items
    )


def _tree_identity(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = 0
    total = 0
    entries = sorted(
        (item.relative_to(root).as_posix(), item)
        for item in root.rglob("*") if item.is_file()
    )
    for relative_text, path in entries:
        if path.is_symlink():
            raise RuntimeError(f"extracted tree contains a symlink: {path.relative_to(root)}")
        relative = relative_text.encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        files += 1
        total += len(payload)
    return digest.hexdigest(), files, total


def _archive_member_path(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError(f"unsafe archive path: {name!r}")
    if path.parts[0] != ARTIFACT_ROOT or len(path.parts) < 2:
        raise RuntimeError(f"archive member is outside {ARTIFACT_ROOT}/: {name!r}")
    return path


def _safe_extract_artifact(artifact: Path, destination: Path) -> tuple[Path, dict]:
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(artifact) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES:
                raise RuntimeError("artifact file count is empty or exceeds the clean-room limit")
            for info in infos:
                member = _archive_member_path(info.filename)
                normalized = member.as_posix()
                if normalized in seen:
                    raise RuntimeError(f"duplicate archive member: {normalized}")
                seen.add(normalized)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise RuntimeError(f"archive member is not a regular file: {normalized}")
                if info.flag_bits & 0x1:
                    raise RuntimeError(f"encrypted archive member is not allowed: {normalized}")
                total += info.file_size
                if total > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("artifact expanded bytes exceed the clean-room limit")
                target = destination.joinpath(*member.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(info, "r") as source:
                    payload = source.read(MAX_ARCHIVE_BYTES + 1)
                if len(payload) != info.file_size:
                    raise RuntimeError(f"archive member size mismatch: {normalized}")
                _write_private(target, payload)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"artifact cannot be safely extracted: {type(exc).__name__}") from exc

    run_root = destination / ARTIFACT_ROOT
    manifest_path = run_root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("artifact manifest is missing or unsafe")
    payload_paths = {
        item.relative_to(run_root).as_posix()
        for item in run_root.rglob("*")
        if item.is_file() and item.name != MANIFEST_NAME
    }
    manifest: dict[str, str] = {}
    for line_number, line in enumerate(
            manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\\\x00]+)", line)
        if match is None:
            raise RuntimeError(f"artifact manifest line {line_number} is invalid")
        digest, relative = match.groups()
        safe = PurePosixPath(relative)
        if safe.is_absolute() or ".." in safe.parts or safe.as_posix() != relative:
            raise RuntimeError(f"artifact manifest path is unsafe: {relative!r}")
        if relative in manifest:
            raise RuntimeError(f"artifact manifest path is duplicated: {relative}")
        manifest[relative] = digest
    if set(manifest) != payload_paths:
        missing = sorted(payload_paths - set(manifest))[:10]
        extra = sorted(set(manifest) - payload_paths)[:10]
        raise RuntimeError(f"artifact manifest membership mismatch: missing={missing} extra={extra}")
    for relative, expected in manifest.items():
        path = run_root / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"artifact payload is not a regular file: {relative}")
        if _sha256(path.read_bytes()) != expected:
            raise RuntimeError(f"artifact manifest digest mismatch: {relative}")

    tree_sha256, file_count, expanded_bytes = _tree_identity(run_root)
    recorder_path = run_root / "record_local_web_macos.py"
    if not recorder_path.is_file():
        raise RuntimeError("artifact recorder is missing")
    return run_root, {
        "root_name": ARTIFACT_ROOT,
        "tree_sha256": tree_sha256,
        "file_count": file_count,
        "expanded_bytes": expanded_bytes,
        "manifest_entries": len(manifest),
        "recorder_sha256": _sha256(recorder_path.read_bytes()),
    }


def _requirements(root: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for filename in ("requirements.txt", "requirements-office.txt"):
        for raw in (root / filename).read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
            if match is None:
                raise RuntimeError(f"unpinned requirement: {filename}: {line}")
            expected[match.group(1)] = match.group(2)
    return expected


def _dependency_result(root: Path) -> dict:
    mismatches: list[dict[str, str | None]] = []
    for name, expected in sorted(_requirements(root).items(), key=lambda item: item[0].lower()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            mismatches.append({"package": name, "expected": expected, "actual": actual})
    return {"status": "PASS" if not mismatches else "BLOCKED", "mismatches": mismatches}


def _clean_env(home: Path, temporary: Path) -> dict[str, str]:
    path_entries = [str(Path(sys.executable).resolve().parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "PATH": os.pathsep.join(path_entries),
        "LANG": "ja_JP.UTF-8",
        "LC_ALL": "ja_JP.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "RI_HUB_DISABLE_SOFFICE": "1",
        "RI_HUB_KEYS_DIR": str(home / ".ri-hub" / "keys"),
        "RI_HUB_AUDIT_KEY_PATH": str(home / ".ri-hub" / "keys" / "audit_chain.key"),
    }
    assert not any(
        marker in key.upper()
        for key in env
        for marker in SECRET_ENV_MARKERS
        if key not in {"RI_HUB_KEYS_DIR", "RI_HUB_AUDIT_KEY_PATH"}
    )
    return env


def _run_recorded(
    command: list[str], *, cwd: Path, env: dict[str, str], raw_path: Path,
    field: str, tree_sha256: str,
) -> dict:
    started = _now()
    result = subprocess.run(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=900, check=False,
    )
    finished = _now()
    output = result.stdout
    _write_private(raw_path, output)
    if result.returncode != 0:
        raise RuntimeError(f"{field} failed with exit {result.returncode}: {output[-2000:]!r}")
    return {
        "status": "pass",
        "exit_code": result.returncode,
        "command": command,
        "cwd_tree_sha256": tree_sha256,
        "started_at": started,
        "finished_at": finished,
        "output_summary": output.decode("utf-8", errors="replace")[-8000:],
        "output_file": raw_path.name,
        "output_bytes": len(output),
        "output_sha256": _sha256(output),
    }


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_page(port: int, path: str = "/", timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}{path}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.read().decode("utf-8", errors="replace")
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server did not respond on {url}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _runtime_state_sha256(cases: Path, nonce_path: Path) -> str:
    digest = hashlib.sha256()
    for label, path in ((b"cases", cases), (b"nonce", nonce_path)):
        payload = path.read_bytes()
        digest.update(label + b"\0" + len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def _runtime_probe(*, env: dict[str, str], run_root: Path, temporary: Path,
                   tree_sha256: str) -> bytes:
    data = temporary / "restart-state" / "out"
    data.mkdir(parents=True)
    nonce = uuid.uuid4().hex
    seed_script = (
        "import sys; from pathlib import Path; import serve, seed_demo; "
        "target=Path(sys.argv[1]); serve._write_fixture(target); seed_demo.seed(target); "
        "Path(sys.argv[2]).write_text(sys.argv[3], encoding='ascii')"
    )
    nonce_path = data / "release-probe-nonce.txt"
    seeded = subprocess.run(
        [sys.executable, "-c", seed_script, str(data), str(nonce_path), nonce],
        cwd=run_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120, check=False,
    )
    if seeded.returncode != 0:
        raise RuntimeError(f"seed failed: {seeded.stdout[-2000:]!r}")
    cases = data / "cases.csv"
    with cases.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise RuntimeError("seeded case state is empty")
    marker = str(rows[0].get("物件名") or "").strip()
    if not marker:
        raise RuntimeError("persisted property marker is empty")
    initial_state = _runtime_state_sha256(cases, nonce_path)
    events: list[dict] = [{
        "schema": RUNTIME_SCHEMA,
        "event": "seed",
        "artifact_tree_sha256": tree_sha256,
        "nonce": nonce,
        "state_sha256": initial_state,
        "marker": marker,
    }]
    for attempt in (1, 2):
        port = _free_port()
        command = [
            sys.executable, "serve.py", "--data-dir", str(data), "--port", str(port),
        ]
        process = subprocess.Popen(
            command, cwd=run_root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            body = _wait_page(port, "/properties")
            if marker not in body:
                raise RuntimeError(f"persisted marker missing after start {attempt}")
            lsof = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(process.pid),
                 "-iTCP", "-sTCP:LISTEN"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=15, check=False,
            )
            listen_text = lsof.stdout.decode("utf-8", errors="replace")
            listeners = re.findall(r"\bTCP\s+(\S+)\s+\(LISTEN\)", listen_text)
            expected = f"127.0.0.1:{port}"
            if lsof.returncode != 0 or listeners != [expected]:
                raise RuntimeError(f"loopback listener not proven exactly: {listen_text}")
            current_state = _runtime_state_sha256(cases, nonce_path)
            current_nonce = nonce_path.read_text(encoding="ascii")
            if current_state != initial_state or current_nonce != nonce:
                raise RuntimeError(f"persisted state changed after start {attempt}")
            events.append({
                "schema": RUNTIME_SCHEMA,
                "event": "start",
                "attempt": attempt,
                "pid": process.pid,
                "port": port,
                "listeners": listeners,
                "marker": marker,
                "nonce": current_nonce,
                "state_sha256": current_state,
                "page_sha256": _sha256(body.encode("utf-8")),
                "artifact_tree_sha256": tree_sha256,
            })
        finally:
            _stop(process)
    events.append({
        "schema": RUNTIME_SCHEMA,
        "event": "summary",
        "status": "PASS",
        "attempts": 2,
        "loopback_only": True,
        "restart_persisted": True,
        "nonce": nonce,
        "state_sha256": initial_state,
        "artifact_tree_sha256": tree_sha256,
    })
    return _jsonl(events)


def _tool_payload(response: dict) -> tuple[dict, bool]:
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    content = result.get("content") if isinstance(result.get("content"), list) else []
    if len(content) != 1 or not isinstance(content[0], dict):
        raise RuntimeError("MCP tool response content shape mismatch")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise RuntimeError("MCP tool response text is missing")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool payload is not an object")
    return payload, result.get("isError") is True


def _mcp_probe(*, env: dict[str, str], run_root: Path, temporary: Path,
               tree_sha256: str) -> bytes:
    data = temporary / "mcp-state" / "out"
    data.mkdir(parents=True)
    actor = "receipt-agent"
    display_name = "証拠担当"
    prepare_script = (
        "import sys; from pathlib import Path; from hub_core import auth; "
        "from hub_core.store import SqliteStore; d=Path(sys.argv[1]); "
        "auth.save_user(d, sys.argv[2], 'receipt-only-password', '担当', display_name=sys.argv[3]); "
        "s=SqliteStore(d/'hub.db'); s.insert_row('cases', {"
        "'case_id':'CASE-EVIDENCE','customer_id':'CUSTOMER-EVIDENCE',"
        "'property_id':'PROPERTY-EVIDENCE','property_name':'証拠物件',"
        "'deal_type':'賃貸','status':'反響','gate_status':'draft','hold_type':'',"
        "'assignee':sys.argv[3]})"
    )
    prepared = subprocess.run(
        [sys.executable, "-c", prepare_script, str(data), actor, display_name],
        cwd=run_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120, check=False,
    )
    if prepared.returncode != 0:
        raise RuntimeError(f"MCP fixture preparation failed: {prepared.stdout[-2000:]!r}")
    mcp_env = dict(env)
    mcp_env.update({
        "AINOTE_DATA_DIR": str(data),
        "AINOTE_MCP_ACTOR": actor,
        "AINOTE_URL": "http://127.0.0.1:8788",
    })
    mcp_env.pop("AINOTE_MCP_ALLOW_WRITES", None)
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_work", "arguments": {"kind": "cases", "limit": 20}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "operate", "arguments": {
             "op": "case_advance",
             "params": {"case_id": "CASE-EVIDENCE", "to_status": "内見"},
         }}},
    )
    input_payload = _jsonl(list(requests))
    result = subprocess.run(
        [sys.executable, "-S", "ainote_mcp_server.py"],
        cwd=run_root, env=mcp_env, input=input_payload, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"MCP probe failed: {result.stdout[-2000:]!r}")
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if [item.get("id") for item in responses] != [1, 2, 3, 4, 5]:
        raise RuntimeError(f"MCP response ids mismatch: {responses!r}")
    if any("result" not in item for item in responses):
        raise RuntimeError(f"MCP response result missing: {responses!r}")
    tools = responses[1]["result"].get("tools", [])
    names = {item.get("name") for item in tools if isinstance(item, dict)}
    if not {"get_status", "list_work", "operate", "open_human_gate"} <= names:
        raise RuntimeError(f"MCP tool list incomplete: {sorted(str(item) for item in names)}")
    status_payload, status_error = _tool_payload(responses[2])
    if (status_error or status_payload.get("status") != "OK"
            or status_payload.get("actor") != actor
            or status_payload.get("writes_enabled") is not False):
        raise RuntimeError(f"MCP get_status did not prove writes disabled: {status_payload!r}")
    work_payload, work_error = _tool_payload(responses[3])
    listed_ids = {
        item.get("case_id") for item in work_payload.get("items", [])
        if isinstance(item, dict)
    }
    if work_error or work_payload.get("status") != "OK" or "CASE-EVIDENCE" not in listed_ids:
        raise RuntimeError(f"MCP list_work did not prove authorized read: {work_payload!r}")
    denied_payload, denied_error = _tool_payload(responses[4])
    if not denied_error or denied_payload.get("code") != "WRITE_DISABLED":
        raise RuntimeError(f"MCP write was not explicitly denied: {denied_payload!r}")
    connection = sqlite3.connect(data / "hub.db")
    try:
        row = connection.execute(
            "SELECT status FROM cases WHERE case_id = ?", ("CASE-EVIDENCE",)).fetchone()
    finally:
        connection.close()
    if row is None or row[0] != "反響":
        raise RuntimeError("MCP denied write changed the case state")
    postcondition = {
        "schema": MCP_POSTCONDITION_SCHEMA,
        "event": "postcondition",
        "artifact_tree_sha256": tree_sha256,
        "actor": actor,
        "case_id": "CASE-EVIDENCE",
        "case_status_after": row[0],
        "writes_enabled": False,
        "write_denied_code": "WRITE_DISABLED",
    }
    return result.stdout + _jsonl([postcondition])


def _safari_version() -> str:
    command = [
        "/usr/libexec/PlistBuddy", "-c", "Print:CFBundleShortVersionString",
        "/Applications/Safari.app/Contents/Info.plist",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        raise RuntimeError("Safari version is unavailable")
    return version


def _bound_record(raw_path: Path, *, status: str = "pass", **fields) -> dict:
    payload = raw_path.read_bytes()
    return {
        "status": status,
        "output_file": raw_path.name,
        "output_bytes": len(payload),
        "output_sha256": _sha256(payload),
        **fields,
    }


def create_receipt(*, artifact: Path, source_commit: str, receipt: Path) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("clean-room receipt requires macOS")
    os_version = platform.mac_ver()[0]
    if not any(re.match(rf"^{re.escape(version)}(?:\.|$)", os_version)
               for version in VERIFIED_MACOS_VERSIONS):
        raise RuntimeError(f"unsupported macOS version: {os_version}")
    if platform.machine() != "arm64":
        raise RuntimeError(f"unsupported architecture: {platform.machine()}")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("receipt must be generated from a newly-created venv")
    if os.environ.get("PYTHONPATH"):
        raise RuntimeError("PYTHONPATH must be unset")

    artifact = artifact.expanduser().resolve(strict=True)
    if artifact.name != ARTIFACT_NAME or not artifact.is_file() or artifact.is_symlink():
        raise RuntimeError(f"artifact must be the regular file {ARTIFACT_NAME}")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise RuntimeError("source commit must be a 40-character lowercase Git id")
    artifact_payload = artifact.read_bytes()
    receipt = receipt.expanduser().resolve()
    if receipt.exists():
        raise FileExistsError(f"receipt already exists: {receipt}")
    evidence_root = receipt.parent
    raw_root = evidence_root / "raw-local-web-macos"
    if raw_root.exists():
        raise FileExistsError(f"raw evidence directory already exists: {raw_root}")
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix=".local-web-evidence-", dir=evidence_root) as stage_name, \
            tempfile.TemporaryDirectory(prefix="ainote_artifact_extract_") as extraction_name, \
            tempfile.TemporaryDirectory(prefix="ainote_clean_home_") as home_name, \
            tempfile.TemporaryDirectory(prefix="ainote_clean_tmp_") as temporary_name:
        stage = Path(stage_name)
        raw_stage = stage / "raw-local-web-macos"
        raw_stage.mkdir(mode=0o700)
        extraction_parent = Path(extraction_name) / "extracted"
        home = Path(home_name)
        temporary = Path(temporary_name)
        home_empty_before = not any(home.iterdir())
        run_root, extraction = _safe_extract_artifact(artifact, extraction_parent)
        current_recorder_sha = _sha256(Path(__file__).resolve().read_bytes())
        if current_recorder_sha != extraction["recorder_sha256"]:
            raise RuntimeError("executing recorder bytes do not match the supplied artifact")
        dependency_result = _dependency_result(run_root)
        if dependency_result["status"] != "PASS":
            raise RuntimeError(f"runtime dependency mismatch: {dependency_result['mismatches']}")
        isolation = {
            "source_zip_extraction": True,
            "git_metadata_absent": not (run_root / ".git").exists(),
            "empty_temporary_home": home_empty_before,
            "new_venv": sys.prefix != sys.base_prefix,
            "pythonpath_unset": not bool(os.environ.get("PYTHONPATH")),
        }
        if not all(isolation.values()):
            raise RuntimeError(f"clean-room isolation failed: {isolation}")
        isolation_payload = {
            "schema": ISOLATION_SCHEMA,
            "status": "PASS",
            "artifact_name": ARTIFACT_NAME,
            "artifact_sha256": _sha256(artifact_payload),
            "artifact_tree_sha256": extraction["tree_sha256"],
            "artifact_file_count": extraction["file_count"],
            "manifest_entries": extraction["manifest_entries"],
            "recorder_sha256": current_recorder_sha,
            "isolation": isolation,
        }
        isolation_path = raw_stage / "isolation-probe.json"
        _write_private(
            isolation_path,
            (json.dumps(isolation_payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        env = _clean_env(home, temporary)
        selftest = _run_recorded(
            [sys.executable, "serve.py", "--selftest"],
            cwd=run_root, env=env, raw_path=raw_stage / "selftest.txt",
            field="selftest", tree_sha256=extraction["tree_sha256"],
        )
        if "ALL PASS" not in selftest["output_summary"]:
            raise RuntimeError("selftest success marker missing")
        walkthrough = _run_recorded(
            [sys.executable, "demo_walkthrough.py"],
            cwd=run_root, env=env, raw_path=raw_stage / "walkthrough.txt",
            field="walkthrough", tree_sha256=extraction["tree_sha256"],
        )
        if not all(marker in walkthrough["output_summary"] for marker in (
                "48 / 48 通過", "fixture 0件・外部送信 0件")):
            raise RuntimeError("walkthrough success markers missing")
        walkthrough.update(passed=48, total=48)
        runtime_path = raw_stage / "runtime-probe.jsonl"
        _write_private(runtime_path, _runtime_probe(
            env=env, run_root=run_root, temporary=temporary,
            tree_sha256=extraction["tree_sha256"]))
        mcp_path = raw_stage / "mcp-probe.jsonl"
        _write_private(mcp_path, _mcp_probe(
            env=env, run_root=run_root, temporary=temporary,
            tree_sha256=extraction["tree_sha256"]))

        for item in (selftest, walkthrough):
            item["output_file"] = "raw-local-web-macos/" + Path(item["output_file"]).name
        isolation_record = _bound_record(
            isolation_path,
            isolation=isolation,
            artifact_tree_sha256=extraction["tree_sha256"],
        )
        runtime_record = _bound_record(
            runtime_path,
            loopback_only=True,
            restart_persisted=True,
            artifact_tree_sha256=extraction["tree_sha256"],
        )
        mcp_record = _bound_record(
            mcp_path,
            response_ids=[1, 2, 3, 4, 5],
            writes_enabled=False,
            write_denied_code="WRITE_DISABLED",
            artifact_tree_sha256=extraction["tree_sha256"],
        )
        for item in (isolation_record, runtime_record, mcp_record):
            item["output_file"] = "raw-local-web-macos/" + Path(item["output_file"]).name
        value = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "kind": "local_web_macos_test",
            "source_commit": source_commit,
            "recorded_at": _now(),
            "status": "pass",
            "clean_machine": True,
            "isolation": isolation,
            "os_version": f"macOS {os_version}",
            "architecture": "arm64",
            "artifact_name": ARTIFACT_NAME,
            "artifact_bytes": len(artifact_payload),
            "artifact_sha256": _sha256(artifact_payload),
            "artifact_tree_sha256": extraction["tree_sha256"],
            "artifact_file_count": extraction["file_count"],
            "artifact_expanded_bytes": extraction["expanded_bytes"],
            "manifest_entries": extraction["manifest_entries"],
            "recorder_sha256": current_recorder_sha,
            "python_version": platform.python_version(),
            "browser": f"Safari {_safari_version()} (installed; HTTP flow probed locally)",
            "loopback_only": True,
            "restart_persisted": True,
            "runtime_dependencies": dependency_result,
            "isolation_probe": isolation_record,
            "selftest": selftest,
            "walkthrough": walkthrough,
            "runtime_probe": runtime_record,
            "mcp_probe": mcp_record,
        }
        encoded = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        staged_receipt = stage / receipt.name
        _write_private(staged_receipt, encoded)
        os.replace(raw_stage, raw_root)
        os.replace(staged_receipt, receipt)
        receipt.chmod(0o600)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate artifact-owned macOS evidence for ainote-local-web-source.zip")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = create_receipt(
            artifact=args.artifact, source_commit=args.source_commit, receipt=args.receipt)
    except (FileExistsError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: receipt={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
