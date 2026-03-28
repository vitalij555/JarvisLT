"""Job portal scraper — fetches new listings and deduplicates against the DB."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from jarvis.connectors.web_crawler import WebCrawler
from jarvis.outsourcing.job_store import JobListing, JobStore, listing_id
from jarvis.outsourcing.portals import toptal, upwork

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class JobScraper:
    def __init__(
        self,
        job_store: JobStore,
        pending_queue: asyncio.Queue,
        config: dict,
    ) -> None:
        self._store = job_store
        self._queue = pending_queue
        self._config = config
        self._crawler = WebCrawler()

    async def fetch_new(self, portal: str) -> list[JobListing]:
        """Fetch listings from the portal and return only ones not yet in DB."""
        logger.info("Scraping portal: %s", portal)
        try:
            if portal == "toptal":
                raw = await self._fetch_toptal()
            elif portal == "upwork":
                raw = await self._fetch_upwork()
            else:
                logger.warning("Unknown portal: %s", portal)
                return []
        except Exception as exc:
            logger.error("Scrape failed for %s: %s", portal, exc)
            return []

        new_listings: list[JobListing] = []
        for item in raw:
            lid = listing_id(item["url"])
            if await self._store.exists(lid):
                continue
            listing = JobListing(
                portal=portal,
                title=item.get("title", ""),
                url=item["url"],
                raw_text=item.get("raw_text", "")[:4000],  # cap stored text
            )
            await self._store.save_listing(listing)
            new_listings.append(listing)

        logger.info("Portal %s: %d new listings", portal, len(new_listings))
        return new_listings

    # ── Portal-specific fetch ──────────────────────────────────────────────────

    async def _fetch_toptal(self) -> list[dict]:
        portal_cfg = self._config.get("portals", {}).get("toptal", {})
        session_file = portal_cfg.get("session_state", toptal.SESSION_STATE_FILE)

        result = await self._crawler.crawl(
            url=toptal.LISTINGS_URL,
            max_depth=toptal.MAX_DEPTH,
            max_pages=toptal.MAX_PAGES,
            topic_filter=toptal.TOPIC_FILTER,
        )

        # Check for auth redirect in first page
        if result.pages:
            first_md = result.pages[0].markdown
            if toptal.detect_auth_redirect(first_md):
                logger.warning("Toptal requires authentication — scrape blocked")
                await self._queue.put({
                    "type": "auth_required",
                    "portal": "toptal",
                    "message": (
                        "Toptal requires login. Open a browser, log in to toptal.com, "
                        "then run: pipenv run python -c \"from playwright.sync_api import "
                        "sync_playwright; p=sync_playwright().start(); b=p.chromium.launch("
                        "headless=False); ctx=b.new_context(); ctx.storage_state("
                        f"path='{session_file}')\" to save your session."
                    ),
                })
                return []

        items = []
        for page in result.pages:
            if toptal.is_listing_url(page.url):
                items.append({
                    "title": toptal.parse_listing_title(page.markdown, page.url),
                    "url": page.url,
                    "raw_text": page.markdown,
                })
        return items

    async def _fetch_upwork(self) -> list[dict]:
        # Try RSS feed first (no auth, lightweight)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(upwork.RSS_URL, follow_redirects=True)
                if resp.status_code == 200 and "<rss" in resp.text[:200]:
                    items = upwork.parse_rss_listings(resp.text, "upwork")
                    if items:
                        logger.info("Upwork RSS returned %d items", len(items))
                        return items
        except Exception as exc:
            logger.warning("Upwork RSS fetch failed: %s — falling back to crawl", exc)

        # Fallback: crawl the listings page
        result = await self._crawler.crawl(
            url=upwork.LISTINGS_URL,
            max_depth=upwork.MAX_DEPTH,
            max_pages=upwork.MAX_PAGES,
            topic_filter=upwork.TOPIC_FILTER,
        )

        if result.pages and upwork.detect_auth_redirect(result.pages[0].markdown):
            logger.warning("Upwork requires authentication — scrape blocked")
            await self._queue.put({
                "type": "auth_required",
                "portal": "upwork",
                "message": "Upwork login required. RSS feed also unavailable.",
            })
            return []

        items = []
        for page in result.pages:
            items.append({
                "title": upwork.parse_listing_title(page.markdown, page.url),
                "url": page.url,
                "raw_text": page.markdown,
            })
        return items
