"""Parse a Discord history export into Records.

Two layouts are supported and auto-detected, because they carry very different
amounts of the conversation:

**GDPR data package** (``Messages/`` present) — Discord's official export. It
contains *only your own messages*; there is no author field because there is
nothing to disambiguate, and nothing the other side said is in the package at
all. Chunks from it necessarily read as a monologue. Layout::

    messages/
      index.json              # { "<channel_id>": "<channel name>", ... }
      c<channel_id>/          # (older exports omit the leading "c")
        channel.json          # { id, type, name?, guild?, recipients? }
        messages.csv          # ID,Timestamp,Contents,Attachments   (older)
        messages.json         # [ { ID, Timestamp, Contents, ... }, ... ] (newer)

**DiscordChatExporter (DCE) JSON** (anything else) — one ``.json`` per channel,
optionally nested a directory deep per guild. This one has *both sides* plus
real author metadata, so chunks are rendered as ``"<author>: <text>"`` turns the
way ``ingest/claude.py`` renders its human/assistant ones. Layout::

    { "guild":   { "id", "name", ... },
      "channel": { "id", "type", "name", ... },
      "messages": [ { "id", "timestamp", "content",
                      "author": { "id", "name", "nickname", "isBot" },
                      "attachments": [...] }, ... ] }

Consecutive messages in a channel are grouped into chunks so each embedded unit
carries some conversational context. A new chunk is started after CHUNK_SIZE
messages or a gap longer than GAP.

DCE records are hash-keyed under a ``dce:`` prefix, so they do *not* collide
with GDPR-derived rows for the same channel — which means the two would happily
coexist and index your own messages twice. When switching a channel's history
from one to the other, purge first: ``slop-trove purge --source discord``.
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

# Bot output (now-playing spam, command confirmations, embed-only posts) is
# mostly noise in a semantic index. Flip to False to keep it.
SKIP_BOTS = True

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


# ── GDPR data package ───────────────────────────────────────────────────────


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


def _parse_gdpr(messages_dir: Path) -> Iterator[Record]:
    index: dict = {}
    index_path = messages_dir / "index.json"
    if index_path.exists():
        index = {str(k): v for k, v in json.loads(index_path.read_text()).items()}

    for channel_dir in sorted(p for p in messages_dir.iterdir() if p.is_dir()):
        chan_meta = _channel_label(channel_dir, index)
        msgs = [m for m in _read_messages(channel_dir) if m["content"]]
        msgs.sort(key=lambda m: (m["ts"] or _TS_MIN))
        yield from _chunk(msgs, chan_meta)


# ── DiscordChatExporter JSON ────────────────────────────────────────────────


def _dce_author(m: dict) -> str:
    author = m.get("author") or {}
    return (author.get("nickname") or author.get("name") or "?").strip() or "?"


def _dce_text(m: dict) -> str:
    """Display text: the content, else a placeholder naming any attachments.

    An image-only message still says something about a conversation ("here's
    the monitor"), and the export carries no pixels to embed, so name the file
    rather than dropping the turn entirely -- same trade-off `claude.py` makes
    for shared files.
    """
    text = (m.get("content") or "").strip()
    if text:
        return text
    names = [
        (a.get("fileName") or "?")
        for a in (m.get("attachments") or [])
    ]
    if names:
        return f"[attachment(s): {', '.join(names)}]"
    return ""


def _parse_dce_file(path: Path) -> Iterator[Record]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return  # not a DCE export (stray json in the output dir)

    channel = data.get("channel") or {}
    guild = data.get("guild") or {}
    chan_meta: dict = {"exporter": "dce"}
    if channel.get("id"):
        chan_meta["channel_id"] = str(channel["id"])
    if channel.get("type"):
        chan_meta["channel_type"] = channel["type"]
    if channel.get("name"):
        chan_meta["channel_name"] = channel["name"]
    # DCE labels the pseudo-guild holding DMs "Direct Messages"; keep it, it
    # distinguishes a DM chunk from a same-named guild channel in results.
    if guild.get("name"):
        chan_meta["guild"] = guild["name"]

    msgs: list[dict] = []
    for m in data["messages"]:
        if SKIP_BOTS and (m.get("author") or {}).get("isBot"):
            continue
        content = _dce_text(m)
        if not content:
            continue
        msgs.append(
            {
                "id": str(m.get("id") or ""),
                "ts": _parse_ts(str(m.get("timestamp") or "")),
                "content": content,
                "author": _dce_author(m),
            }
        )
    msgs.sort(key=lambda m: (m["ts"] or _TS_MIN))
    yield from _chunk(msgs, chan_meta, hash_prefix="dce:")


# ── Shared chunking ─────────────────────────────────────────────────────────


def parse(export_root: str | Path) -> Iterator[Record]:
    """Yield Records from a Discord export, auto-detecting the layout.

    ``export_root`` may be a GDPR package root, its ``messages/`` folder, a DCE
    output directory, or a single DCE ``.json`` file.
    """
    root = Path(export_root)

    if root.is_file():
        yield from _parse_dce_file(root)
        return

    # GDPR package: the folder is "messages" in older packages, "Messages" in
    # newer ones. Its presence is what distinguishes the two layouts.
    if root.name.lower() == "messages":
        yield from _parse_gdpr(root)
        return
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name.lower() == "messages":
            yield from _parse_gdpr(p)
            return

    # Otherwise: DCE JSON, one file per channel, optionally nested per guild.
    # Skip AppleDouble sidecars ("._foo.json"), which a macOS tar leaves behind
    # and pathlib.glob (unlike a shell glob) happily matches.
    files = sorted(p for p in root.rglob("*.json") if not p.name.startswith("."))
    if not files:
        raise FileNotFoundError(
            f"{root} is neither a Discord GDPR package (no Messages/ dir) nor a "
            f"DiscordChatExporter output directory (no .json files)"
        )
    for f in files:
        yield from _parse_dce_file(f)


def _chunk(
    msgs: list[dict],
    chan_meta: dict,
    *,
    hash_prefix: str = "",
) -> Iterator[Record]:
    buf: list[dict] = []

    def flush() -> Iterator[Record]:
        if not buf:
            return
        # Prefix the speaker only when the source actually knows who it was.
        # The GDPR path deliberately renders bare, so its hashes -- and the
        # rows already in the store -- stay byte-identical.
        text = "\n".join(
            f"{m['author']}: {m['content']}" if m.get("author") else m["content"]
            for m in buf
        )
        first, last = buf[0], buf[-1]
        meta = dict(chan_meta)
        meta.update(
            {
                "hash_key": (
                    f"{hash_prefix}{chan_meta.get('channel_id','?')}"
                    f":{first['id']}:{last['id']}"
                ),
                "message_ids": [m["id"] for m in buf],
                "first_ts": first["ts"].isoformat() if first["ts"] else None,
                "last_ts": last["ts"].isoformat() if last["ts"] else None,
                "message_count": len(buf),
            }
        )
        authors = sorted({m["author"] for m in buf if m.get("author")})
        if authors:
            meta["authors"] = authors
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
