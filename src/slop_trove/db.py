"""Postgres + pgvector storage layer.

One `records` table holds the normalized records plus a text-embedding column.
Image embeddings (v2) will live in a separate table/column — different vector
space, not comparable to text.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from .models import Record


@contextmanager
def connect(db_url: str, register: bool = True) -> Iterator[psycopg.Connection]:
    """Connect, optionally registering the pgvector type adapter.

    Pass register=False when the extension may not exist yet (bootstrap
    paths); init_schema() registers it once the extension is in place.
    """
    conn = psycopg.connect(db_url, autocommit=False)
    try:
        if register:
            register_vector(conn)
        yield conn
    finally:
        conn.close()


def init_schema(conn: psycopg.Connection, dim: int) -> None:
    """Create the extension, table and indexes if they don't exist.

    The extension may already have been provisioned by a superuser (the NixOS
    module does this); if we lack privilege to create it ourselves we assume
    it's present and carry on.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_type WHERE typname = 'vector'")
        if cur.fetchone() is None:
            raise RuntimeError(
                "pgvector extension is not installed and this role may not "
                "create it (pgvector is not 'trusted'). Run as superuser: "
                "CREATE EXTENSION vector;"
            )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS records (
                id            BIGSERIAL PRIMARY KEY,
                source        TEXT        NOT NULL,
                ts            TIMESTAMPTZ,
                text          TEXT        NOT NULL,
                metadata      JSONB       NOT NULL DEFAULT '{{}}',
                content_hash  TEXT        NOT NULL UNIQUE,
                embedding     vector({dim})
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS records_embedding_idx "
            "ON records USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS records_source_idx ON records (source)")
        cur.execute("CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts)")
    conn.commit()
    # Now that the extension surely exists, the adapter can be registered on
    # this connection (callers that connected with register=False rely on it).
    register_vector(conn)


def upsert(
    conn: psycopg.Connection,
    rows: Iterable[tuple[Record, list[float]]],
) -> int:
    """Insert (record, embedding) pairs, skipping ones already stored.

    Idempotent via the content_hash UNIQUE constraint, so re-running ingest
    only adds what's new. Returns the number of newly inserted rows.
    """
    inserted = 0
    with conn.cursor() as cur:
        for rec, vec in rows:
            cur.execute(
                """
                INSERT INTO records (source, ts, text, metadata, content_hash, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_hash) DO NOTHING
                """,
                (rec.source, rec.timestamp, rec.text, Jsonb(rec.metadata),
                 rec.content_hash(), vec),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def search(
    conn: psycopg.Connection,
    query_vec: list[float],
    *,
    source: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    """Cosine-similarity search with optional source/time filtering."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, ts, text, metadata,
                   1 - (embedding <=> %(q)s) AS score
            FROM records
            WHERE embedding IS NOT NULL
              AND (%(source)s IS NULL OR source = %(source)s)
              AND (%(start)s IS NULL OR ts >= %(start)s)
              AND (%(end)s   IS NULL OR ts <= %(end)s)
            ORDER BY embedding <=> %(q)s
            LIMIT %(limit)s
            """,
            {"q": query_vec, "source": source, "start": start, "end": end,
             "limit": limit},
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
