"""The common normalized record every source ingester produces.

Keeping one shape across sources is what lets us add email / purchases /
photos later without changing the store or the MCP layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Record:
    source: str  # "discord", "email", ...
    text: str  # the embedded/searchable text
    timestamp: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        """Stable id for idempotent upserts.

        Derived from source + a source-provided stable key (e.g. message id
        range) when available, otherwise from the text itself.
        """
        key = self.metadata.get("hash_key")
        basis = f"{self.source}\x00{key}" if key else f"{self.source}\x00{self.text}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()
