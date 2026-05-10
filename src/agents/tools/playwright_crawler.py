"""
Playwright Crawler — headless browser crawler for kapruka.com.

Extracts product Name, Price, Description, and Availability across
multiple gift categories and writes the results to catalog.json.

Usage:
    python src/agents/tools/playwright_crawler.py
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import nest_asyncio
from loguru import logger
from pydantic import BaseModel, field_validator

nest_asyncio.apply()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.kapruka.com"
DEFAULT_OUTPUT_PATH = "data/catalog.json"

CATEGORIES: Dict[str, str] = {
    "cakes":        f"{BASE_URL}/online/cakes",
    "flowers":      f"{BASE_URL}/online/flowers",
    "chocolates":   f"{BASE_URL}/online/chocolates",
    "clothing":     f"{BASE_URL}/online/clothing",
    "electronics":  f"{BASE_URL}/online/electronics",
    "fashion":      f"{BASE_URL}/online/fashion",
    "softtoys":    f"{BASE_URL}/online/softtoy",
    "grocery":      f"{BASE_URL}/online/grocery",
    "hampers":      f"{BASE_URL}/online/hampers",
    "greetingcards": f"{BASE_URL}/online/greetingcards",
    "sports":      f"{BASE_URL}/online/sports",
    "baby":         f"{BASE_URL}/online/baby",
    "jewellery":   f"{BASE_URL}/online/jewellery",
    "cosmatics":    f"{BASE_URL}/online/cosmatics",
    "customisedgifts":    f"{BASE_URL}/online/customisedGifts",
    "pharmacy":     f"{BASE_URL}/online/pharmacy",
    "home_lifestyle": f"{BASE_URL}/online/home_lifestyle",
    "combogifts":   f"{BASE_URL}/online/combogifts",
    "books":        f"{BASE_URL}/online/books",
    "fruitbaskets": f"{BASE_URL}/online/fruitbaskets",
}

# Candidate CSS selectors for product cards on listing pages — probed in order.
CARD_SELECTORS: List[str] = [
    "div.product-item",
    "div[class*='product-card']",
    "div[class*='productCard']",
    "div[class*='item'] a[href*='/buyonline']",
    "a[href*='/buyonline/']",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

AVAILABILITY_MAP = {
    "http://schema.org/instock": "In Stock",
    "https://schema.org/instock": "In Stock",
    "http://schema.org/outofstock": "Out of Stock",
    "https://schema.org/outofstock": "Out of Stock",
    "http://schema.org/preorder": "Pre-Order",
    "https://schema.org/preorder": "Pre-Order",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class ProductModel(BaseModel):
    product_id: str
    name: str
    price: Optional[float]
    description: Optional[str]
    availability: str
    url: str
    category: str
    scraped_at: str

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class PlaywrightCrawler:
    """
    Headless Playwright crawler for kapruka.com.

    All I/O is async internally; the public ``dispatch()`` method is
    synchronous so callers (agent tools, notebooks) need no event-loop
    management.
    """

    def __init__(
        self,
        output_path: str = DEFAULT_OUTPUT_PATH,
        headless: bool = True,
        max_products_per_category: int = 60,
        request_delay: float = 1.5,
    ) -> None:
        self.output_path = Path(output_path)
        self.headless = headless
        self.max_products_per_category = max_products_per_category
        self.request_delay = request_delay
        self._card_selector: Optional[str] = None  # cached after first probe

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def dispatch(self, action: str = "crawl", **kwargs: Any) -> Dict[str, Any]:
        """
        Synchronous entry point.

        Supported actions:
          - ``"crawl"``            : full crawl of all categories
          - ``"crawl_category"``   : crawl a single category (requires ``category`` kwarg)
          - ``"scrape_product"``   : scrape one product URL (requires ``url`` kwarg)
        """
        if action == "crawl":
            return asyncio.run(self.run())
        if action == "crawl_category":
            category = kwargs.get("category", "cakes")
            return asyncio.run(self._run_single_category(category))
        if action == "scrape_product":
            url = kwargs.get("url", "")
            return asyncio.run(self._run_single_product(url))
        return {"status": "error", "message": f"Unknown action: {action}"}

    # ------------------------------------------------------------------
    # Async orchestrators
    # ------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """Crawl all configured categories and write catalog.json."""
        from playwright.async_api import async_playwright

        all_products: List[Dict] = []
        start_time = time.time()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            for category_name, url in CATEGORIES.items():
                logger.info("Crawling category: {}", category_name)
                try:
                    products = await self._crawl_category(page, category_name, url)
                    all_products.extend(products)
                    logger.success(
                        "Category '{}' complete — {} products", category_name, len(products)
                    )
                except Exception as exc:
                    logger.error("Category '{}' failed: {}", category_name, exc)

                # Progressive save after each category
                self._save_catalog(all_products)

            await browser.close()

        elapsed = round(time.time() - start_time, 1)
        logger.success(
            "Crawl complete — {} products in {:.1f}s -> {}",
            len(all_products), elapsed, self.output_path,
        )
        return {
            "status": "success",
            "products_scraped": len(all_products),
            "output_path": str(self.output_path),
            "elapsed_seconds": elapsed,
        }

    async def _run_single_category(self, category: str) -> Dict[str, Any]:
        from playwright.async_api import async_playwright

        url = CATEGORIES.get(category)
        if not url:
            return {"status": "error", "message": f"Unknown category: {category}"}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            products = await self._crawl_category(page, category, url)
            await browser.close()

        self._save_catalog(products)
        return {"status": "success", "products_scraped": len(products)}

    async def _run_single_product(self, url: str) -> Dict[str, Any]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            card = {"name": "", "price_raw": "", "url": url}
            product = await self._scrape_product_detail(page, card, "unknown")
            await browser.close()

        return product or {}

    # ------------------------------------------------------------------
    # Category-level crawl
    # ------------------------------------------------------------------

    async def _crawl_category(
        self, page: Any, category_name: str, url: str
    ) -> List[Dict]:
        from playwright.async_api import TimeoutError as PWTimeout

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            logger.warning("Timeout loading category page: {}", url)
            return []

        # Wait for at least one product card to appear
        await self._wait_for_products(page)

        # Expand the listing up to max_products_per_category
        await self._load_all_products(page)

        # Extract lightweight card data from the listing page
        cards = await self._extract_listing_cards(page, category_name)
        logger.info("Found {} product cards for '{}'", len(cards), category_name)

        products: List[Dict] = []
        for idx, card in enumerate(cards[: self.max_products_per_category], 1):
            logger.debug("[{}/{}] Scraping: {}", idx, len(cards), card.get("url", ""))
            product = await self._scrape_product_detail(page, card, category_name)
            if product:
                products.append(product)

        return products

    # ------------------------------------------------------------------
    # Pagination: load-more button
    # ------------------------------------------------------------------

    async def _wait_for_products(self, page: Any) -> None:
        """Wait until at least one product card is visible."""
        from playwright.async_api import TimeoutError as PWTimeout

        for selector in CARD_SELECTORS:
            try:
                await page.wait_for_selector(selector, timeout=12_000)
                return
            except PWTimeout:
                continue
        logger.warning("No product cards detected — proceeding anyway")

    async def _load_all_products(self, page: Any) -> None:
        """
        Click the 'See More Products' button until we have enough items
        or the button disappears.
        """
        selector = await self._detect_card_selector(page)
        if not selector:
            return

        while True:
            current_count = len(await page.query_selector_all(selector))
            if current_count >= self.max_products_per_category:
                break

            button = page.locator(
                "button:has-text('See More'), "
                "a:has-text('See More'), "
                "button:has-text('Load More'), "
                "a:has-text('Load More'), "
                "button:has-text('Show More'), "
                "a:has-text('Show More')"
            ).first

            try:
                visible = await button.is_visible()
            except Exception:
                break

            if not visible:
                break

            await button.click()
            logger.debug("Clicked 'See More' — waiting for new cards...")

            try:
                await page.wait_for_function(
                    f"document.querySelectorAll({json.dumps(selector)}).length > {current_count}",
                    timeout=10_000,
                )
            except Exception:
                break

            await asyncio.sleep(self.request_delay)

    async def _detect_card_selector(self, page: Any) -> Optional[str]:
        """Probe CARD_SELECTORS and cache the first that finds > 3 elements."""
        if self._card_selector:
            return self._card_selector

        for selector in CARD_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                if len(elements) > 3:
                    self._card_selector = selector
                    logger.debug("Using card selector: {}", selector)
                    return selector
            except Exception:
                continue

        logger.warning("Could not detect a reliable card selector")
        return None

    # ------------------------------------------------------------------
    # Listing-page extraction
    # ------------------------------------------------------------------

    async def _extract_listing_cards(
        self, page: Any, category: str
    ) -> List[Dict[str, str]]:
        """Extract lightweight card data (name, price_raw, url) from the listing DOM."""
        selector = await self._detect_card_selector(page)
        if not selector:
            return []

        elements = await page.query_selector_all(selector)
        cards: List[Dict[str, str]] = []

        for el in elements:
            try:
                anchor = await el.query_selector("a[href*='/buyonline']") or el
                href = await anchor.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = BASE_URL + href

                name = ""
                for name_sel in ["h2", "h3", "[class*='name']", "[class*='title']", "p"]:
                    name_el = await el.query_selector(name_sel)
                    if name_el:
                        name = (await name_el.inner_text()).strip()
                        if name:
                            break
                if not name:
                    name = (await el.inner_text()).split("\n")[0].strip()

                price_raw = ""
                for price_sel in [
                    "[class*='price']", "[class*='Price']",
                    "span[class*='cost']", "div[class*='cost']",
                ]:
                    price_el = await el.query_selector(price_sel)
                    if price_el:
                        price_raw = (await price_el.inner_text()).strip()
                        if price_raw:
                            break
                if not price_raw:
                    card_text = await el.inner_text()
                    m = re.search(r"RS\.?\s*[\d,]+", card_text, re.IGNORECASE)
                    if m:
                        price_raw = m.group()

                if href:
                    cards.append({"name": name, "price_raw": price_raw, "url": href})
            except Exception as exc:
                logger.debug("Card extraction error: {}", exc)
                continue

        return cards

    # ------------------------------------------------------------------
    # Product detail-page extraction
    # ------------------------------------------------------------------

    async def _scrape_product_detail(
        self, page: Any, card: Dict[str, str], category: str
    ) -> Optional[Dict]:
        from playwright.async_api import TimeoutError as PWTimeout

        url = card.get("url", "")
        if not url:
            return None

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_selector("h1", timeout=10_000)
        except PWTimeout:
            logger.warning("Timeout on product page: {}", url)
            return self._build_partial_product(card, category, url)
        except Exception as exc:
            logger.warning("Error loading product page {}: {}", url, exc)
            return self._build_partial_product(card, category, url)

        # Name
        name = card.get("name", "")
        try:
            h1 = await page.query_selector("h1")
            if h1:
                name = (await h1.inner_text()).strip() or name
        except Exception:
            pass

        # JSON-LD (primary for price + availability)
        json_ld = await self._parse_json_ld(page)

        # Price
        price: Optional[float] = None
        if json_ld:
            offers = json_ld.get("offers") or json_ld
            raw = (
                offers.get("price")
                or offers.get("lowPrice")
                or json_ld.get("price")
            )
            if raw is not None:
                price = self._parse_price(str(raw))

        if price is None:
            price = self._parse_price(card.get("price_raw", ""))

        if price is None:
            try:
                page_text = await page.inner_text("body")
                m = re.search(r"RS\.?\s*([\d,]+)", page_text, re.IGNORECASE)
                if m:
                    price = self._parse_price(m.group())
            except Exception:
                pass

        # Availability
        availability = "Unknown"
        if json_ld:
            offers = json_ld.get("offers") or json_ld
            avail_raw = offers.get("availability") or json_ld.get("availability", "")
            availability = AVAILABILITY_MAP.get(
                str(avail_raw).lower().rstrip("/"), availability
            )

        if availability == "Unknown":
            availability = await self._parse_availability(page)

        # Description — try JSON-LD first, then page
        description: Optional[str] = None
        if json_ld:
            description = json_ld.get("description") or None
        if not description:
            description = await self._parse_description(page)

        await asyncio.sleep(self.request_delay)

        return ProductModel(
            product_id=self._get_product_id(url),
            name=name,
            price=price,
            description=description,
            availability=availability,
            url=url,
            category=category,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        ).model_dump()

    def _build_partial_product(
        self, card: Dict[str, str], category: str, url: str
    ) -> Dict:
        """Fallback product dict using listing-page card data when detail page fails."""
        return ProductModel(
            product_id=self._get_product_id(url),
            name=card.get("name", "Unknown"),
            price=self._parse_price(card.get("price_raw", "")),
            description=None,
            availability="Unknown",
            url=url,
            category=category,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        ).model_dump()

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    async def _parse_json_ld(self, page: Any) -> Optional[Dict]:
        """Extract and parse the first schema.org JSON-LD block on the page."""
        try:
            scripts = await page.query_selector_all("script[type='application/ld+json']")
            for script in scripts:
                raw = await script.inner_text()
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
                    if isinstance(data, list) and data:
                        return data[0]
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return None

    async def _parse_availability(self, page: Any) -> str:
        """Text-based availability fallback."""
        try:
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
            if "out of stock" in body_lower:
                return "Out of Stock"
            if "in stock" in body_lower or "delivery within" in body_lower:
                return "In Stock"
        except Exception:
            pass
        return "Unknown"

    async def _parse_description(self, page: Any) -> Optional[str]:
        """Extract product description: meta tag primary, paragraph fallback."""
        try:
            meta = await page.query_selector("meta[name='description']")
            if meta:
                content = await meta.get_attribute("content")
                if content and len(content.strip()) > 20:
                    return content.strip()
        except Exception:
            pass

        try:
            for p_sel in [
                "div[class*='desc'] p",
                "div[class*='detail'] p",
                "section p",
                "article p",
                "main p",
            ]:
                p_el = await page.query_selector(p_sel)
                if p_el:
                    text = (await p_el.inner_text()).strip()
                    if len(text) > 30:
                        return text
        except Exception:
            pass

        return None

    def _parse_price(self, raw_text: str) -> Optional[float]:
        """Parse 'RS.5,800' or 'LKR 5800' or '5800.00' to float, None on failure."""
        if not raw_text:
            return None
        # Strip currency prefix (RS., LKR, etc.) before touching digits
        stripped = re.sub(r"(?i)rs\.?\s*|lkr\.?\s*", "", str(raw_text)).strip()
        # Remove thousands separators (commas)
        stripped = stripped.replace(",", "")
        # Extract the first numeric token
        m = re.search(r"[\d]+(?:\.\d+)?", stripped)
        if not m:
            return None
        try:
            return float(m.group())
        except ValueError:
            return None

    def _get_product_id(self, url: str) -> str:
        """Extract product ID from URL query params or path segments."""
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if "kid" in qs:
                return qs["kid"][0]
            segments = [s for s in parsed.path.split("/") if s]
            if segments:
                return segments[-1]
        except Exception:
            pass
        return re.sub(r"[^a-zA-Z0-9_-]", "_", url)[-40:]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_catalog(self, products: List[Dict]) -> None:
        """Write catalog.json with metadata wrapper."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        categories_seen = sorted({p.get("category", "") for p in products})
        catalog = {
            "metadata": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "total_products": len(products),
                "categories_crawled": categories_seen,
                "source": BASE_URL,
            },
            "products": products,
        }

        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)

        logger.info("Saved {} products -> {}", len(products), self.output_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Kapruka.com Playwright Crawler")
    parser.add_argument("--category", default=None, help="Crawl a single category")
    parser.add_argument("--max", type=int, default=60, help="Max products per category")
    parser.add_argument("--visible", action="store_true", help="Run with visible browser")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests (s)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Output JSON path")
    args = parser.parse_args()

    crawler = PlaywrightCrawler(
        output_path=args.output,
        headless=not args.visible,
        max_products_per_category=args.max,
        request_delay=args.delay,
    )

    if args.category:
        result = crawler.dispatch("crawl_category", category=args.category)
    else:
        result = crawler.dispatch("crawl")

    print(json.dumps(result, indent=2))
