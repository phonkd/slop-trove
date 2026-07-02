"""Command line entrypoint: init-db, ingest, query, serve."""

from __future__ import annotations

import argparse
import sys

from . import config, db, embed
from .ingest import discord


def _batched(seq, n):
    batch = []
    for item in seq:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def cmd_init_db(_args) -> int:
    cfg = config.load()
    with db.connect(cfg.db_url) as conn:
        db.init_schema(conn, cfg.embed_dim)
    print(f"schema ready (dim={cfg.embed_dim}) in {cfg.db_url}")
    return 0


def cmd_ingest(args) -> int:
    cfg = config.load()
    if args.source != "discord":
        print(f"unknown source: {args.source}", file=sys.stderr)
        return 2

    embedder = embed.Embedder(cfg.embed_endpoint, cfg.embed_model, cfg.embed_dim)
    total_new = 0
    seen = 0
    with db.connect(cfg.db_url) as conn:
        db.init_schema(conn, cfg.embed_dim)
        for batch in _batched(discord.parse(args.path), args.batch_size):
            vecs = embedder.embed([r.text for r in batch])
            total_new += db.upsert(conn, zip(batch, vecs))
            seen += len(batch)
            print(f"\r  embedded {seen} chunks, {total_new} new", end="", flush=True)
    print(f"\ndone: {seen} chunks processed, {total_new} newly stored")
    return 0


def cmd_query(args) -> int:
    cfg = config.load()
    embedder = embed.Embedder(cfg.embed_endpoint, cfg.embed_model, cfg.embed_dim)
    qvec = embedder.embed_one(args.text)
    with db.connect(cfg.db_url) as conn:
        rows = db.search(conn, qvec, source=args.source, limit=args.limit)
    for r in rows:
        ch = r["metadata"].get("channel_name", "?")
        ts = r["ts"].isoformat() if r["ts"] else "?"
        snippet = r["text"].replace("\n", " ⏎ ")[:200]
        print(f"[{r['score']:.3f}] {r['source']}/{ch} {ts}\n  {snippet}\n")
    return 0


def cmd_serve(_args) -> int:
    from .mcp_server import main as serve_main

    serve_main()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="slop-trove")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create schema/extension/indexes").set_defaults(
        func=cmd_init_db
    )

    pi = sub.add_parser("ingest", help="parse + embed a source export")
    pi.add_argument("--source", default="discord")
    pi.add_argument("--path", required=True, help="path to the export root")
    pi.add_argument("--batch-size", type=int, default=64)
    pi.set_defaults(func=cmd_ingest)

    pq = sub.add_parser("query", help="semantic search from the CLI")
    pq.add_argument("text")
    pq.add_argument("--source")
    pq.add_argument("--limit", type=int, default=10)
    pq.set_defaults(func=cmd_query)

    sub.add_parser("serve", help="run the MCP HTTP server").set_defaults(func=cmd_serve)

    args = p.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
