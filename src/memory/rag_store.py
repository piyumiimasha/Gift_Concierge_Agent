"""
Tier 2 — Long-Term RAG Store (Qdrant).

Implements the ``RAGStore`` protocol from ``memory.schemas`` using
Qdrant Cloud as the vector database and OpenRouter embeddings.

Each kapruka.com product from catalog.json is stored as a Qdrant point:
  - vector : OpenRouter embedding of CatalogProduct.embed_text
  - payload: full product dict (all fields from CatalogProduct)

Semantic search flow
--------------------
    User query: "sweet gift for wife under LKR 3000"
         │
         ▼
    OpenRouterEmbedder.embed_one(query)  → 1 536-dim vector
         │
         ▼
    Qdrant.query_points(collection, vector, → top-k points by cosine sim
                  filter=category)
         │
         ▼
    List[ProductSearchResult]            → returned to the agent

Usage
-----
    from memory.rag_store import QdrantRAGStore
    from memory.embedder import OpenRouterEmbedder

    store = QdrantRAGStore(embedder=OpenRouterEmbedder())
    store.ensure_collection()

    # Ingest
    store.upsert(products)          # list of CatalogProduct

    # Search
    results = store.query(
        text="birthday cake chocolate",
        k=5,
        threshold=0.5,
        category_filter="cakes",
    )
    for r in results:
        print(r.product.name, r.score)
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Iterable, List, Optional

from dotenv import load_dotenv
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from memory.embedder import EMBEDDING_DIM, OpenRouterEmbedder
from memory.schemas import CatalogProduct, ProductSearchResult

load_dotenv()

# Default batch size when upserting products into Qdrant.
# Keeps individual API payloads small and gives progress visibility.
_UPSERT_BATCH = 25          # smaller batches avoid Qdrant Cloud read timeouts
_QDRANT_TIMEOUT = 60        # seconds per request


def _product_to_point_id(product_id: str) -> str:
    """
    Convert a kapruka product_id string to a stable UUID for Qdrant.

    Qdrant point IDs must be unsigned int or UUID string.
    We MD5-hash the product_id so the mapping is deterministic and
    re-ingesting the same product always overwrites the same point.
    """
    digest = hashlib.md5(product_id.encode()).hexdigest()
    return str(uuid.UUID(digest))


class QdrantRAGStore:
    """
    RAG store backed by Qdrant Cloud.

    Satisfies the ``RAGStore`` protocol defined in ``memory.schemas``.

    Parameters
    ----------
    embedder:
        Any object with an ``embed(texts) -> List[List[float]]`` method.
        Defaults to ``OpenRouterEmbedder()``.
    collection_name:
        Qdrant collection to use.  Defaults to QDRANT_COLLECTION_NAME env var.
    upsert_batch_size:
        Number of products to embed + upload per Qdrant batch.
    """

    def __init__(
        self,
        embedder: Optional[OpenRouterEmbedder] = None,
        collection_name: Optional[str] = None,
        upsert_batch_size: int = _UPSERT_BATCH,
    ) -> None:
        url = os.getenv("QDRANT_URL", "").strip()
        api_key = os.getenv("QDRANT_API_KEY", "").strip()
        if not url:
            raise EnvironmentError("QDRANT_URL is not set in the environment.")

        self._client = QdrantClient(url=url, api_key=api_key or None, timeout=_QDRANT_TIMEOUT)
        self._embedder = embedder or OpenRouterEmbedder()
        self.collection_name = (
            collection_name
            or os.getenv("QDRANT_COLLECTION_NAME", "kapruka_catalog")
        )
        self.upsert_batch_size = upsert_batch_size
        logger.info(
            "QdrantRAGStore initialised — collection: '{}'", self.collection_name
        )

    # ------------------------------------------------------------------
    # RAGStore protocol
    # ------------------------------------------------------------------

    def upsert(self, products: Iterable[CatalogProduct]) -> None:
        """
        Embed and upsert products into Qdrant in batches.

        Idempotent — re-upserting the same product_id overwrites the
        existing point (same UUID point ID derived from product_id).
        """
        batch: List[CatalogProduct] = []
        total = 0

        for product in products:
            batch.append(product)
            if len(batch) >= self.upsert_batch_size:
                self._upsert_batch(batch)
                total += len(batch)
                logger.info("Upserted {} products so far...", total)
                batch = []

        if batch:
            self._upsert_batch(batch)
            total += len(batch)

        logger.success("Upsert complete — {} products in collection '{}'",
                       total, self.collection_name)

    def query(
        self,
        text: str,
        k: int = 5,
        threshold: float = 0.5,
        category_filter: Optional[str] = None,
    ) -> List[ProductSearchResult]:
        """
        Semantic search over the gift catalog.

        Parameters
        ----------
        text:
            Natural-language gift query, e.g. "chocolate for wife birthday".
        k:
            Maximum number of results to return.
        threshold:
            Minimum cosine similarity score (0–1).  Results below this
            are discarded.
        category_filter:
            If set, restrict results to this category (e.g. "cakes").

        Returns
        -------
        List of ``ProductSearchResult`` sorted by score descending.
        """
        query_vector = self._embedder.embed_one(text)

        qdrant_filter = None
        if category_filter:
            qdrant_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="category",
                        match=qmodels.MatchValue(value=category_filter),
                    )
                ]
            )

        try:
            response = self._client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=k,
                score_threshold=threshold,
                query_filter=qdrant_filter,
                with_payload=True,
            )
            hits = response.points
        except Exception as exc:
            logger.error("Qdrant search failed: {}", exc)
            return []

        results: List[ProductSearchResult] = []
        for hit in hits:
            try:
                payload = hit.payload or {}
                product = CatalogProduct(
                    product_id=payload.get("product_id", ""),
                    name=payload.get("name", ""),
                    category=payload.get("category", ""),
                    url=payload.get("url", ""),
                    price=payload.get("price"),
                    description=payload.get("description"),
                    availability=payload.get("availability", "Unknown"),
                    scraped_at=payload.get("scraped_at", ""),
                    similarity=hit.score,
                )
                results.append(
                    ProductSearchResult(product=product, score=hit.score, query=text)
                )
            except Exception as exc:
                logger.warning("Could not deserialise search hit: {}", exc)

        return results

    def delete(self, product_id: str) -> None:
        """Delete a single product from the collection by its product_id."""
        point_id = _product_to_point_id(product_id)
        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.PointIdsList(points=[point_id]),
            )
            logger.info("Deleted product '{}' from Qdrant", product_id)
        except Exception as exc:
            logger.error("Qdrant delete failed for '{}': {}", product_id, exc)

    def count(self) -> int:
        """Return the total number of vectors in the collection."""
        try:
            info = self._client.get_collection(self.collection_name)
            return info.points_count or 0
        except Exception as exc:
            logger.error("Qdrant count failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def ensure_collection(self) -> None:
        """
        Create the Qdrant collection if it does not already exist.

        Uses cosine distance — appropriate for OpenAI embeddings.
        Call once before the first upsert (``ingest_catalog.py`` does this).
        """
        existing = [c.name for c in self._client.get_collections().collections]
        if self.collection_name in existing:
            logger.info(
                "Collection '{}' already exists ({} vectors)",
                self.collection_name, self.count(),
            )
            return

        self._client.create_collection(
            collection_name=self.collection_name,
            vectors_config=qmodels.VectorParams(
                size=EMBEDDING_DIM,
                distance=qmodels.Distance.COSINE,
            ),
        )
        self._ensure_indexes()
        logger.success("Created Qdrant collection '{}'", self.collection_name)

    def _ensure_indexes(self) -> None:
        """Create payload indexes required for filtered search."""
        self._client.create_payload_index(
            collection_name=self.collection_name,
            field_name="category",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
        logger.info("Payload index created for 'category'")

    def delete_collection(self) -> None:
        """Drop the entire collection. Use with caution."""
        self._client.delete_collection(self.collection_name)
        logger.warning("Deleted Qdrant collection '{}'", self.collection_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_batch(self, products: List[CatalogProduct]) -> None:
        """Embed one batch of products and push to Qdrant."""
        texts = [p.embed_text for p in products]
        vectors = self._embedder.embed(texts)

        points = [
            qmodels.PointStruct(
                id=_product_to_point_id(p.product_id),
                vector=vec,
                payload=p.to_dict(),
            )
            for p, vec in zip(products, vectors)
        ]

        self._client.upsert(
            collection_name=self.collection_name,
            points=points,
        )
