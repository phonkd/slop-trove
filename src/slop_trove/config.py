"""Runtime configuration, read from the environment.

The NixOS module (nixos-module.nix) sets these env vars on the systemd
services. Defaults here are aimed at local development.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # libpq connection string. With database.createLocally + peer auth this is
    # just "dbname=slop_trove"; otherwise a full "postgresql://user:pw@host/db".
    db_url: str = os.environ.get("SLOP_TROVE_DB_URL", "dbname=slop_trove")

    # Ollama (or any OpenAI-/Ollama-compatible /api/embed endpoint).
    embed_endpoint: str = os.environ.get(
        "SLOP_TROVE_EMBED_ENDPOINT", "http://127.0.0.1:11434"
    )
    embed_model: str = os.environ.get("SLOP_TROVE_EMBED_MODEL", "nomic-embed-text")
    # Must match the model's output dimensionality (nomic-embed-text = 768).
    embed_dim: int = int(os.environ.get("SLOP_TROVE_EMBED_DIM", "768"))

    # MCP server bind address.
    mcp_host: str = os.environ.get("SLOP_TROVE_MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.environ.get("SLOP_TROVE_MCP_PORT", "9120"))


def load() -> Config:
    return Config()
