"""Parse a Discord GDPR data package into Records.

Discord's export only contains *your own* messages. Layout (the ``messages/``
folder of the unzipped package):

    messages/
      index.json              # { "<channel_id>": "<channel name>", ... }
      c<channel_id>/          # (older exports omit the leading "c")
        channel.json          # { id, type, name?, guild?, recipients? }
        messages.csv          # ID,Timestamp,Contents,Attachments   (older)
        messages.json         # [ { ID, Timestamp, Contents, ... }, ... ] (newer)

Consecutive messages in a channel are grouped into chunks so each embedded
unit carries some conversational context. A new chunk is started after
CHUNK_SIZE messages or a gap longer than GAP.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..models import Record

CHUNK_SIZE = 10
GAP = timedelta(hours=6)

_TS_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    s = raw.strip().replace(" ", "T", 1)
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        # Some exports use a trailing 'Z'.
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Export timestamps are UTC; newer packages omit the offset entirely.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _read_messages(channel_dir: Path) -> list[dict]:
    """Return raw message dicts with keys: id, ts (datetime|None), content."""
    out: list[dict] = []
    csv_path = channel_dir / "messages.csv"
    json_path = channel_dir / "messages.json"
    if json_path.exists():
        for m in json.loads(json_path.read_text(encoding="utf-8")):
            out.append(
                {
                    "id": str(m.get("ID") or m.get("id") or ""),
                    "ts": _parse_ts(str(m.get("Timestamp") or m.get("timestamp") or "")),
                    "content": (m.get("Contents") or m.get("content") or "").strip(),
                }
            )
    elif csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as fh:
            for m in csv.DictReader(fh):
                out.append(
                    {
                        "id": (m.get("ID") or "").strip(),
                        "ts": _parse_ts(m.get("Timestamp") or ""),
                        "content": (m.get("Contents") or "").strip(),
                    }
                )
    return out


def _channel_label(channel_dir: Path, index: dict) -> dict:
    """Best-effort human-readable channel metadata."""
    meta: dict = {}
    cj = channel_dir / "channel.json"
    if cj.exists():
        info = json.loads(cj.read_text(encoding="utf-8"))
        cid = str(info.get("id") or "")
        meta["channel_id"] = cid
        meta["channel_type"] = info.get("type")
        if info.get("name"):
            meta["channel_name"] = info["name"]
        guild = info.get("guild")
        if isinstance(guild, dict) and guild.get("name"):
            meta["guild"] = guild["name"]
        recipients = info.get("recipients")
        if isinstance(recipients, list) and recipients:
            meta["recipients"] = recipients
        if "channel_name" not in meta and cid in index:
            meta["channel_name"] = index[cid]
    return meta


def parse(export_root: str | Path) -> Iterator[Record]:
    """Yield Records from a Discord data package.

    ``export_root`` may point at the package root or directly at ``messages/``.
    """
    # The folder is "messages" in older packages, "Messages" in newer ones.
    root = Path(export_root)
    if root.name.lower() == "messages":
        messages_dir = root
    else:
        candidates = [p for p in root.iterdir() if p.is_dir() and p.name.lower() == "messages"]
        if not candidates:
            raise FileNotFoundError(f"no Messages/ dir under {root}")
        messages_dir = candidates[0]

    index: dict = {}
    index_path = messages_dir / "index.json"
    if index_path.exists():
        index = {str(k): v for k, v in json.loads(index_path.read_text()).items()}

    for channel_dir in sorted(p for p in messages_dir.iterdir() if p.is_dir()):
        chan_meta = _channel_label(channel_dir, index)
        msgs = [m for m in _read_messages(channel_dir) if m["content"]]
        msgs.sort(key=lambda m: (m["ts"] or _TS_MIN))
        yield from _chunk(msgs, chan_meta)


def _chunk(msgs: list[dict], chan_meta: dict) -> Iterator[Record]:
    buf: list[dict] = []

    def flush() -> Iterator[Record]:
        if not buf:
            return
        text = "\n".join(m["content"] for m in buf)
        first, last = buf[0], buf[-1]
        meta = dict(chan_meta)
        meta.update(
            {
                "hash_key": f"{chan_meta.get('channel_id','?')}:{first['id']}:{last['id']}",
                "message_ids": [m["id"] for m in buf],
                "first_ts": first["ts"].isoformat() if first["ts"] else None,
                "last_ts": last["ts"].isoformat() if last["ts"] else None,
                "message_count": len(buf),
            }
        )
        yield Record(source="discord", text=text, timestamp=first["ts"], metadata=meta)

    prev_ts: datetime | None = None
    for m in msgs:
        if buf and (
            len(buf) >= CHUNK_SIZE
            or (prev_ts and m["ts"] and m["ts"] - prev_ts > GAP)
        ):
            yield from flush()
            buf = []
        buf.append(m)
        prev_ts = m["ts"]
    yield from flush()
