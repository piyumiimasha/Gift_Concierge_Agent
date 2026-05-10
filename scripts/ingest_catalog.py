"""
Ingest catalog.json into the Qdrant vector store.

Reads data/catalog.json (produced by the Playwright crawler), converts
each product into a CatalogProduct, embeds it with OpenRouter, and upserts
the vectors into the Qdrant collection.

Safe to re-run — upserting the same product_id overwrites the existing
point, so you can refresh after a new crawl without duplicates.

Usage
-----
    # Full ingest
    python scripts/ingest_catalog.py

    # Ingest a specific category only
    python scripts/ingest_catalog.py --category cakes

    # Preview without writing to Qdrant
    python scripts/ingest_catalog.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make src/ importable when run from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loguru import logger
from memory.embedder import OpenRouterEmbedder
from memory.rag_store import QdrantRAGStore
from memory.schemas import CatalogProduct


DEFAULT_CATALOG = Path("data/catalog.json")


def load_products(
    catalog_path: Path,
    category_filter: str | None = None,
) -> list[CatalogProduct]:
    """Parse catalog.json into a list of CatalogProduct dataclasses."""
    if not catalog_path.exists():
        logger.error("catalog.json not found at '{}'", catalog_path)
        sys.exit(1)

    with open(catalog_path, encoding="utf-8") as f:
        data = json.load(f)

    raw_products = data.get("products", [])
    logger.info(
        "Loaded {} products from '{}' (scraped {})",
        len(raw_products),
        catalog_path,
        data.get("metadata", {}).get("scraped_at", "unknown"),
    )

    products = [CatalogProduct.from_dict(p) for p in raw_products]

    if category_filter:
        products = [p for p in products if p.category == category_filter]
        logger.info(
            "Filtered to category '{}' → {} products", category_filter, len(products)
        )

    return products


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest kapruka catalog.json into Qdrant."
    )
    parser.add_argument(
        "--catalog",
        default=str(DEFAULT_CATALOG),
        help="Path to catalog.json (default: data/catalog.json)",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Only ingest products from this category (e.g. 'cakes')",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Products per Qdrant upsert batch (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and embed but do NOT write to Qdrant",
    )
    args = parser.parse_args()

    products = load_products(Path(args.catalog), category_filter=args.category)

    if not products:
        logger.warning("No products to ingest. Exiting.")
        return

    embedder = OpenRouterEmbedder()
    store = QdrantRAGStore(embedder=embedder, upsert_batch_size=args.batch_size)

    if args.dry_run:
        logger.info("Dry-run mode — embedding first 3 products to verify...")
        sample = products[:3]
        vectors = embedder.embed([p.embed_text for p in sample])
        for p, v in zip(sample, vectors):
            logger.info("  '{}' → vector dim={}, first3={}", p.name, len(v), v[:3])
        logger.success("Dry-run complete. No data written to Qdrant.")
        return

    # Create collection if it doesn't exist
    store.ensure_collection()

    logger.info("Starting ingest of {} products...", len(products))
    store.upsert(products)

    final_count = store.count()
    logger.success(
        "Ingest complete — Qdrant collection '{}' now has {} vectors.",
        store.collection_name,
        final_count,
    )


if __name__ == "__main__":
    main()
