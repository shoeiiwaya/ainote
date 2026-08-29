"""会話履歴の永続化 — Consoleの会話をサーバ側にスレッド単位で保存し、リロード後も復元する。

ローカルファースト: スレッドは <data_dir>/chat_threads/<thread_id>.jsonl(1行=1ターン)に**全文**保存
(ユーザー自身の端末/自分のクラウド内。外部送信時のredactとは別＝手元の履歴は読める形で残す)。
法的監査(HMAC)・利用ログ(chat_sessions.jsonl 伏字)とは独立の「会話の保存」層。stdlibのみ。
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

_JST = datetime.timezone(datetime.timedelta(hours=9))
_SAFE = re.compile(r"[^0-9A-Za-z\-]")


def _now() -> str:
    return datetime.datetime.now(_JST).isoformat(timespec="seconds")


def _threads_dir(data_dir) -> Path:
    return Path(data_dir) / "chat_threads"


def _safe_id(thread_id: str) -> str:
    tid = _SAFE.sub("", str(thread_id or ""))[:64]
    return tid


def new_thread_id() -> str:
    now = datetime.datetime.now(_JST)
    # 列挙耐性のため高エントロピー(16バイト)。所有者スコープと併せ多層防御。
    return "T-" + now.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(16).hex()


def thread_path(data_dir, thread_id: str) -> Path | None:
    tid = _safe_id(thread_id)
    if not tid:
        return None
    return _threads_dir(data_dir) / f"{tid}.jsonl"


def _owner_path(data_dir, thread_id: str) -> Path | None:
    p = thread_path(data_dir, thread_id)
    return p.with_suffix(".owner") if p is not None else None


def owner_of(data_dir, thread_id: str) -> str | None:
    """スレッドの所有者(viewer.user)。未記録なら None。"""
    op = _owner_path(data_dir, thread_id)
    if op is None or not op.exists():
        return None
    try:
        return op.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _can_access(data_dir, thread_id: str, viewer_user: str | None) -> bool:
    """所有者スコープ(IDOR防止)。viewer_user=None は呼出側がスコープ無効を明示した場合のみ(dev等)。"""
    if viewer_user is None:
        return True
    o = owner_of(data_dir, thread_id)
    return o is None or o == viewer_user  # 未所有(新規)は初回appendで claim される


def append_turn(data_dir, thread_id: str, role: str, content: str, *,
                owner: str | None = None, meta: dict | None = None) -> bool:
    """会話ターンを追記。owner 指定時は所有者を強制(他人のスレへの書込はfail-closedで拒否)。"""
    p = thread_path(data_dir, thread_id)
    if p is None:
        return False
    if owner is not None:
        existing = owner_of(data_dir, thread_id)
        if existing is not None and existing != owner:
            return False  # IDOR: 他ユーザーのスレッドへの書込を拒否
    p.parent.mkdir(parents=True, exist_ok=True)
    if owner is not None and owner_of(data_dir, thread_id) is None:
        try:
            _owner_path(data_dir, thread_id).write_text(owner, encoding="utf-8")
        except Exception:
            pass
    rec = {"ts": _now(), "role": role, "content": str(content or "")}
    if meta:
        rec["meta"] = meta
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def load_thread(data_dir, thread_id: str, *, owner: str | None = None) -> list[dict]:
    if owner is not None and not _can_access(data_dir, thread_id, owner):
        return []  # IDOR: 他ユーザーのスレッドは読ませない
    p = thread_path(data_dir, thread_id)
    if p is None or not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _title_from(turns: list[dict]) -> str:
    for t in turns:
        if t.get("role") == "user" and (t.get("content") or "").strip():
            s = " ".join(str(t["content"]).split())
            return s[:38] + ("…" if len(s) > 38 else "")
    return "(新しい会話)"


def list_threads(data_dir, *, owner: str | None = None) -> list[dict]:
    """新しい順のスレッド一覧。owner 指定時は所有者一致のスレッドのみ(IDOR防止)。"""
    d = _threads_dir(data_dir)
    if not d.exists():
        return []
    items = []
    for p in d.glob("*.jsonl"):
        if owner is not None:
            o = owner_of(data_dir, p.stem)
            if o is not None and o != owner:
                continue  # 他ユーザーのスレッドは一覧に出さない
        turns = load_thread(data_dir, p.stem)
        if not turns:
            continue
        items.append({
            "thread_id": p.stem,
            "title": _title_from(turns),
            "updated": turns[-1].get("ts", ""),
            "turns": sum(1 for t in turns if t.get("role") in ("user", "assistant")),
        })
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def history_for_llm(data_dir, thread_id: str, *, owner: str | None = None) -> list[dict]:
    """LLMに渡せる [{role, content}] 形へ。user/assistant のみ。"""
    out = []
    for t in load_thread(data_dir, thread_id, owner=owner):
        if t.get("role") in ("user", "assistant"):
            out.append({"role": t["role"], "content": t.get("content", "")})
    return out
