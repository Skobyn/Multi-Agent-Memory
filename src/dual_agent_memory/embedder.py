"""Text embedding generator with pluggable backend."""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Callable

from dual_agent_memory.config import Config


class Embedder:
    """Generates text embeddings with a deterministic hash-based default or a pluggable backend."""

    def __init__(
        self,
        config: Config | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
    ) -> None:
        self._dim = config.embedding_dim if config else 384
        self._embed_fn = embed_fn

    def embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            vec = self._embed_fn(text)
            return self._normalize(vec)
        return self._hash_embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _hash_embed(self, text: str) -> list[float]:
        raw: list[float] = []
        block = 0
        while len(raw) < self._dim:
            digest = hashlib.sha256(
                f"{block}:{text}".encode("utf-8")
            ).digest()
            # Each SHA-256 digest gives 32 bytes = 8 floats (4 bytes each)
            for i in range(0, 32, 4):
                if len(raw) >= self._dim:
                    break
                # Unpack 4 bytes as an unsigned 32-bit int, map to [-1, 1]
                (val,) = struct.unpack("<I", digest[i : i + 4])
                raw.append(val / 2_147_483_648.0 - 1.0)  # map 0..2^32 -> -1..1
            block += 1
        return self._normalize(raw[: self._dim])

    @staticmethod
    def _normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < 1e-12:
            return vec
        return [v / norm for v in vec]
