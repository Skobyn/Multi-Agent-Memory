"""Embedding providers (DESIGN.md §12). No hash embedder: v0.1's SHA-256
"embeddings" had no semantics and are deliberately not carried forward."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenRouterEmbedder:
    """OpenAI-compatible ``/embeddings`` endpoint on OpenRouter."""

    def __init__(self, model: str, base_url: str = "https://openrouter.ai/api/v1",
                 timeout_ms: int = 10000, api_key: str | None = None) -> None:
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout_ms / 1000
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.name = f"openrouter:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/embeddings", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [list(map(float, r["embedding"])) for r in rows]


class LocalEmbedder:
    """sentence-transformers, loaded lazily; optional dependency."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name, self._model, self.name = model, None, f"local:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
            self._model = SentenceTransformer(self.model_name)
        return [list(map(float, v)) for v in self._model.encode(texts, normalize_embeddings=True)]


class NoEmbedder:
    """Disables vector retrieval (keyword and scope matching only)."""

    name = "none"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


def make_embedder(cfg: dict) -> Embedder:
    provider = cfg.get("provider", "openrouter")
    if provider == "openrouter":
        return OpenRouterEmbedder(cfg.get("model", "openai/text-embedding-3-small"),
                                 cfg.get("base_url", "https://openrouter.ai/api/v1"),
                                 int(cfg.get("timeout_ms", 10000)))
    if provider == "local":
        return LocalEmbedder(cfg.get("model", "BAAI/bge-small-en-v1.5"))
    if provider == "none":
        return NoEmbedder()
    raise ValueError(f"unsupported embeddings provider {provider!r} (openrouter | local | none)")
