"""MCP server exposing semantic search over the personal-data store.

Runs over streamable HTTP so Hermes (and anything else) can reach it via
  mcp_servers.slop_trove.url = "http://127.0.0.1:9120/mcp"
Hermes registers the tool as `mcp_slop_trove_search_personal_data`.
"""

from __future__ import annotations

from datetime import datetime

from mcp.server.fastmcp import FastMCP

from . import config, db, embed


def build_server() -> FastMCP:
    cfg = config.load()
    mcp = FastMCP("slop-trove", host=cfg.mcp_host, port=cfg.mcp_port)
    embedder = embed.Embedder(cfg.embed_endpoint, cfg.embed_model, cfg.embed_dim)

    def _parse_dt(v: str | None) -> datetime | None:
        return datetime.fromisoformat(v) if v else None

    @mcp.tool()
    def search_personal_data(
        query: str,
        source: str | None = None,
        limit: int = 10,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """Semantically search the user's personal data archive.

        Args:
            query: Natural-language description of what to find.
            source: Restrict to one source (e.g. "discord"). Omit for all.
            limit: Max results (default 10).
            start: ISO datetime lower bound on the record timestamp.
            end: ISO datetime upper bound on the record timestamp.

        Returns a list of {source, timestamp, text, metadata, score},
        most-similar first.
        """
        qvec = embedder.embed_one(query)
        with db.connect(cfg.db_url) as conn:
            rows = db.search(
                conn, qvec, source=source, start=_parse_dt(start),
                end=_parse_dt(end), limit=max(1, min(limit, 50)),
            )
        for r in rows:
            ts = r.get("ts")
            r["timestamp"] = ts.isoformat() if isinstance(ts, datetime) else None
            r.pop("ts", None)
        return rows

    return mcp


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
