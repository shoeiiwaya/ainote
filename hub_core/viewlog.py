"""閲覧監査(Stage0 S0-5)。

「誰が・いつ・何を閲覧したか」を append-only で記録する。actor は実ユーザー(認証セッション)
または dev。hub の改ざん防止チェーン(out/audit_log.jsonl)とは別 sink(out/view_audit.jsonl)に
書き、hub の監査を汚さない。読み取り操作の証跡=漏洩時に被害範囲を特定する土台。

stdlib のみ・全ローカル。複数スレッド(ThreadingMixIn)からの追記を Lock で直列化。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOCK = threading.Lock()
JST = timezone(timedelta(hours=9))
VIEW_LOG = "view_audit.jsonl"


def record_view(data_dir, actor: str, role: str, target: str, ts: str | None = None,
                action: str = "VIEW"):
    """1操作を out/view_audit.jsonl へ追記(append-only)。action 既定=VIEW。
    S0-7: エクスポート禁止の試行は action='export' で記録する(誰がエクスポートを試みたか)。"""
    rec = {
        "ts": ts or datetime.now(JST).replace(microsecond=0).isoformat(),
        "actor": actor or "?", "role": role or "?", "action": action, "target": target,
    }
    path = Path(data_dir) / VIEW_LOG
    line = json.dumps(rec, ensure_ascii=False)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return rec


def read_view_audit(data_dir, limit: int = 1000):
    path = Path(data_dir) / VIEW_LOG
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]
