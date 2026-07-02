"""Text embeddings via an Ollama-compatible /api/embed endpoint.

Batched. Swap the model via SLOP_TROVE_EMBED_MODEL; keep SLOP_TROVE_EMBED_DIM
in sync with the model (the DB column is a fixed-width vector).
"""

from __future__ import annotations

import httpx


class Embedder:
    def __init__(self, endpoint: str, model: str, dim: int, timeout: float = 120.0):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.dim = dim
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Order is preserved."""
        if not texts:
            return []
        resp = self._client.post(
            f"{self.endpoint}/api/embed",
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        vecs = data.get("embeddings")
        if vecs is None:
            raise RuntimeError(f"unexpected embed response: {data!r}")
        for v in vecs:
            if len(v) != self.dim:
                raise RuntimeError(
                    f"model returned dim {len(v)} but SLOP_TROVE_EMBED_DIM={self.dim}"
                )
        return vecs

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()
