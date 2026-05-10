"""
Embedder implementations for the Gift Concierge RAG pipeline.

OpenRouterEmbedder — uses OpenRouter's OpenAI-compatible embedding endpoint.
                     Defaults to text-embedding-3-small (1 536 dims).

Usage
-----
    from memory.embedder import OpenRouterEmbedder

    embedder = OpenRouterEmbedder()
    vectors = embedder.embed(["dark chocolate gift", "flower bouquet"])
    # → List[List[float]], each inner list has 1 536 dimensions
"""

from __future__ import annotations

import os
import time
from typing import List

from dotenv import load_dotenv
from loguru import logger
from openai import OpenAI

load_dotenv()

EMBEDDING_DIM = 1536
EMBEDDING_MODEL = "text-embedding-3-small"
_BATCH_LIMIT = 2048
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterEmbedder:
    """
    Implements the ``Embedder`` protocol using OpenRouter's embedding endpoint.

    OpenRouter exposes an OpenAI-compatible API, so the openai SDK is reused
    with a custom base_url pointing to OpenRouter.

    Parameters
    ----------
    model:
        Embedding model name supported by OpenRouter.
        Defaults to ``text-embedding-3-small`` (1 536 dims).
    """

    def __init__(self, model: str = EMBEDDING_MODEL) -> None:
        api_key = os.getenv("OPENROUTE_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError("OPENROUTE_API_KEY is not set in the environment.")
        self._client = OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL)
        self.model = model
        self.dim = EMBEDDING_DIM

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a list of strings and return their float vectors.

        Handles batching transparently — callers can pass any number of
        texts without worrying about API limits.

        Parameters
        ----------
        texts:
            Strings to embed. Empty strings are replaced with a single
            space to avoid API errors.

        Returns
        -------
        List of float vectors, one per input text, in the same order.
        """
        if not texts:
            return []

        cleaned = [t if t.strip() else " " for t in texts]

        all_vectors: List[List[float]] = []
        for i in range(0, len(cleaned), _BATCH_LIMIT):
            batch = cleaned[i : i + _BATCH_LIMIT]
            all_vectors.extend(self._embed_batch(batch))

        return all_vectors

    def embed_one(self, text: str) -> List[float]:
        """Convenience wrapper for a single string."""
        return self.embed([text])[0]

    def _embed_batch(self, texts: List[str], retry: bool = True) -> List[List[float]]:
        try:
            response = self._client.embeddings.create(
                input=texts,
                model=self.model,
            )
            items = sorted(response.data, key=lambda x: x.index)
            return [item.embedding for item in items]
        except Exception as exc:
            if retry:
                logger.warning("Embedding batch failed, retrying in 2s: {}", exc)
                time.sleep(2)
                return self._embed_batch(texts, retry=False)
            logger.error("Embedding batch failed: {}", exc)
            raise
