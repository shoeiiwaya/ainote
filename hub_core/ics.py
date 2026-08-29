"""M-ics — 内見予約の iCalendar (.ics) 生成（RFC5545 最小準拠・stdlib only）。

内見(viewing)が成立したときに、担当・顧客がカレンダーに取り込める VEVENT を1件作る。
- 依存なし（datetime/hashlib のみ）。ネットワーク非接触。
- 捏造しない: 物件名/住所が無ければ SUMMARY/LOCATION に埋めない（空欄のまま）。
- 日時は event_at（担当が確定した値・"YYYY-MM-DDTHH:MM"）を **floating local time** として出す
  （タイムゾーンDBを持ち込まない＝tz を捏造しない。DTSTAMP のみ UTC=Z）。
- 改行は CRLF。TEXT値は RFC5545 3.3.11 に沿ってエスケープ、行は 75 octet で UTF-8 境界を保って折る。
"""
from __future__ import annotations

import datetime
import hashlib

_CRLF = "\r\n"
_DEFAULT_DURATION_MIN = 60   # 内見の所要目安（担当が別途調整可）。DTEND の算出のみに使う。


def _escape_text(value: str) -> str:
    """RFC5545 3.3.11 TEXT エスケープ（\\ ; , と改行）。"""
    s = str(value or "")
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    return s


def _fold(line: str) -> str:
    """1論理行を 75 octet で物理行に折る（継続行は先頭 1 空白）。UTF-8 の多バイト境界は割らない。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out = []
    chunk = bytearray()
    # 継続行は先頭空白1つ分(=1 octet)を差し引く。最初の行は 75、以降は 74。
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
            limit = 74   # 継続行は先頭空白分を引く
        chunk += b
    if chunk:
        out.append(chunk.decode("utf-8"))
    return (_CRLF + " ").join(out)


def _parse_local(event_at: str) -> datetime.datetime:
    """"YYYY-MM-DDTHH:MM"（秒任意）→ naive datetime。不正は ValueError。"""
    s = str(event_at or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"event_at は 'YYYY-MM-DDTHH:MM' 形式で: {event_at!r}")


def make_uid(event_id: str, *, domain: str = "ainote.local") -> str:
    """VEVENT の UID（イベントIDに束縛・決定的・世界一意）。event_id が空でも安定に作る。"""
    base = str(event_id or "").strip()
    if not base:
        base = hashlib.sha256(repr(event_id).encode("utf-8")).hexdigest()[:16].upper()
    return f"{base}@{domain}"


def build_viewing_ics(*, event_id: str, event_at: str, summary: str = "",
                      location: str = "", description: str = "",
                      duration_min: int = _DEFAULT_DURATION_MIN,
                      organizer: str = "", organizer_email: str = "",
                      dtstamp: datetime.datetime | None = None) -> str:
    """内見1件の VCALENDAR/VEVENT テキストを返す（RFC5545 最小準拠・CRLF）。

    event_at は担当が確定した内見日時（floating local）。summary/location は空なら埋めない（捏造しない）。

    organizer は**お客様の予定表に出る主催者名＝取扱会社**。空なら ORGANIZER 行を出さない
    ＝別の名前を代わりに置かない。
    """
    start = _parse_local(event_at)
    dur = int(duration_min) if int(duration_min or 0) > 0 else _DEFAULT_DURATION_MIN
    end = start + datetime.timedelta(minutes=dur)
    now = dtstamp or datetime.datetime.now(datetime.timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ainote//viewing//JP",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{make_uid(event_id)}",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_escape_text(summary or '内見')}",
    ]
    org = str(organizer or "").strip()
    if org:
        mail = str(organizer_email or "").strip()
        cn = f'ORGANIZER;CN={_escape_text(org)}'
        lines.append(f"{cn}:mailto:{mail}" if mail else f"{cn}:invalid:nomail")
    if str(location or "").strip():
        lines.append(f"LOCATION:{_escape_text(location)}")
    if str(description or "").strip():
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _CRLF.join(_fold(ln) for ln in lines) + _CRLF
