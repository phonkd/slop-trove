"""Parse a Claude.ai data export into Records.

Layout (root of the unzipped export):
    conversations.json   # [ { uuid, name, chat_messages: [...] }, ... ]
    design_chats/*.json  # same per-conversation shape as conversations.json
    memories.json         # [ { conversations_memory: "**Header**\\n...", ... } ]
    projects/*.json, users.json  # not ingested (bundled example project /
                                  # account identity, not personal history)

Chat messages are chunked the same way as the Discord ingester: group
consecutive messages until CHUNK_SIZE is hit or a GAP passes between them, so
each embedded unit carries some conversational context. tool_use/tool_result/
thinking content blocks are skipped -- only human-authored and final-answer
text is indexed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..models import Record

CHUNK_SIZE = 10
GAP = timedelta(hours=6)

_TS_MIN = datetime.min.replace(tzinfo=timezone.utc)
_SECTION_RE = re.compile(r"\n(?=\*\*[^\n*]+\*\*\n)")
_HEADER_RE = re.compile(r"\*\*([^\n*]+)\*\*")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _message_text(m: dict) -> str:
    """Best-effort display text: m['text'], else text-type content blocks,
    else a placeholder naming any attached files (export carries no image
    bytes to embed)."""
    text = (m.get("text") or "").strip()
    if text:
        return text
    parts = [
        c.get("text", "").strip()
        for c in (m.get("content") or [])
        if c.get("type") == "text" and c.get("text", "").strip()
    ]
    if parts:
        return "\n".join(parts)
    # `.get("file_name", "?")` only defaults a *missing* key; an export can
    # carry the key with an explicit null (a file shared without a name), so
    # fall back with `or` to keep join() from choking on a NoneType.
    files = [f.get("file_name") or "?" for f in (m.get("files") or [])] + [
        a.get("file_name") or "?" for a in (m.get("attachments") or [])
    ]
    if files:
        return f"[shared file(s): {', '.join(files)}]"
    return ""


def _chunk_conversation(conv: dict, source: str) -> Iterator[Record]:
    conv_uuid = conv.get("uuid", "?")
    conv_name = conv.get("name") or ""
    msgs = []
    for m in conv.get("chat_messages") or conv.get("messages") or []:
        text = _message_text(m)
        if not text:
            continue
        msgs.append(
            {
                "id": m.get("uuid", ""),
                "ts": _parse_ts(m.get("created_at")),
                "sender": m.get("sender", "?"),
                "text": text,
            }
        )
    msgs.sort(key=lambda m: (m["ts"] or _TS_MIN))

    buf: list[dict] = []

    def flush() -> Iterator[Record]:
        if not buf:
            return
        text = "\n".join(f"{m['sender']}: {m['text']}" for m in buf)
        first, last = buf[0], buf[-1]
        meta = {
            "conversation_uuid": conv_uuid,
            "conversation_name": conv_name,
            "hash_key": f"{conv_uuid}:{first['id']}:{last['id']}",
            "message_ids": [m["id"] for m in buf],
            "first_ts": first["ts"].isoformat() if first["ts"] else None,
            "last_ts": last["ts"].isoformat() if last["ts"] else None,
            "message_count": len(buf),
        }
        yield Record(source=source, text=text, timestamp=first["ts"], metadata=meta)

    prev_ts: datetime | None = None
    for m in msgs:
        if buf and (
            len(buf) >= CHUNK_SIZE or (prev_ts and m["ts"] and m["ts"] - prev_ts > GAP)
        ):
            yield from flush()
            buf = []
        buf.append(m)
        prev_ts = m["ts"]
    yield from flush()


def _parse_memories(root: Path) -> Iterator[Record]:
    path = root / "memories.json"
    if not path.exists():
        return
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        text = (entry.get("conversations_memory") or "").strip()
        if not text:
            continue
        # Split on markdown-bold section headers ("**Header**") so each
        # section (Work context, Personal context, ...) is independently
        # retrievable instead of one huge blob.
        for i, section in enumerate(_SECTION_RE.split(text)):
            section = section.strip()
            if not section:
                continue
            header_match = _HEADER_RE.match(section)
            header = header_match.group(1) if header_match else f"section {i}"
            yield Record(
                source="claude_memory",
                text=section,
                timestamp=None,
                metadata={
                    "hash_key": f"memory:{entry.get('account_uuid', '?')}:{i}",
                    "section": header,
                },
            )


def parse(export_root: str | Path) -> Iterator[Record]:
    """Yield Records from an unzipped Claude.ai data export root."""
    root = Path(export_root)

    conv_path = root / "conversations.json"
    if conv_path.exists():
        for conv in json.loads(conv_path.read_text(encoding="utf-8")):
            yield from _chunk_conversation(conv, "claude")

    design_dir = root / "design_chats"
    if design_dir.is_dir():
        # Exports that pass through a macOS tar can carry AppleDouble
        # resource-fork sidecars ("._foo.json") alongside the real file;
        # pathlib.glob (unlike a shell glob) matches those too.
        for f in sorted(design_dir.glob("*.json")):
            if f.name.startswith("."):
                continue
            conv = json.loads(f.read_text(encoding="utf-8"))
            yield from _chunk_conversation(conv, "claude_design_chat")

    yield from _parse_memories(root)
