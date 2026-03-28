"""Playwright-backed web crawler using crawl4ai for LLM-friendly depth crawling."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Max characters returned per page to avoid flooding the LLM context
_PAGE_CHAR_LIMIT = 8_000
# Max total characters across all pages in a deep crawl
_TOTAL_CHAR_LIMIT = 40_000


@dataclass
class CrawlPage:
    url: str
    title: str
    markdown: str
    depth: int = 0


@dataclass
class CrawlResult:
    pages: list[CrawlPage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_text(self, include_urls: bool = True) -> str:
        """Format all pages into a single text block for the LLM."""
        parts: list[str] = []
        total = 0
        for page in self.pages:
            header = f"### {page.title or page.url}"
            if include_urls:
                header += f"\nURL: {page.url}"
            body = page.markdown[:_PAGE_CHAR_LIMIT]
            chunk = f"{header}\n\n{body}"
            if total + len(chunk) > _TOTAL_CHAR_LIMIT:
                parts.append(f"### [truncated — {len(self.pages)} pages crawled, showing first {len(parts)}]")
                break
            parts.append(chunk)
            total += len(chunk)
        if self.errors:
            parts.append(f"\n**Errors during crawl:** {'; '.join(self.errors[:3])}")
        return "\n\n---\n\n".join(parts)


class WebCrawler:
    """
    Depth-first/BFS web crawler using crawl4ai (Playwright backend).

    Supports:
    - Single-page fetch (max_depth=1)
    - Multi-page BFS crawl following internal links (max_depth=2+)
    - Optional keyword filtering (topic_filter) applied per-page
    """

    async def crawl(
        self,
        url: str,
        max_depth: int = 1,
        max_pages: int = 10,
        topic_filter: str | None = None,
    ) -> CrawlResult:
        try:
            from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        except ImportError:
            return CrawlResult(errors=["crawl4ai not installed. Run: pip install crawl4ai && crawl4ai-setup"])

        result = CrawlResult()

        try:
            if max_depth <= 1:
                await self._crawl_single(url, result, topic_filter)
            else:
                await self._crawl_deep(url, max_depth, max_pages, result, topic_filter)
        except Exception as exc:
            logger.error("Crawl failed for %s: %s", url, exc)
            result.errors.append(str(exc))

        return result

    async def _crawl_single(
        self, url: str, result: CrawlResult, topic_filter: str | None
    ) -> None:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

        config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, verbose=False)
        async with AsyncWebCrawler() as crawler:
            res = await crawler.arun(url=url, config=config)
            if res.success:
                page = self._to_page(res, depth=0, topic_filter=topic_filter)
                result.pages.append(page)
            else:
                result.errors.append(f"{url}: {res.error_message or 'unknown error'}")

    async def _crawl_deep(
        self,
        url: str,
        max_depth: int,
        max_pages: int,
        result: CrawlResult,
        topic_filter: str | None,
    ) -> None:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

        try:
            from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
        except ImportError:
            logger.warning("crawl4ai deep_crawling not available, falling back to single-page")
            await self._crawl_single(url, result, topic_filter)
            return

        strategy = BFSDeepCrawlStrategy(
            max_depth=max_depth,
            max_pages=max_pages,
            include_external=False,
        )
        config = CrawlerRunConfig(
            deep_crawl_strategy=strategy,
            cache_mode=CacheMode.BYPASS,
            stream=False,
            verbose=False,
        )

        async with AsyncWebCrawler() as crawler:
            raw = await crawler.arun(url=url, config=config)

        # deep crawl returns a list; single crawl returns a CrawlResult object
        pages_raw = raw if isinstance(raw, list) else [raw]
        for res in pages_raw:
            if not res.success:
                result.errors.append(f"{getattr(res, 'url', '?')}: {res.error_message or 'failed'}")
                continue
            depth = res.metadata.get("depth", 0) if hasattr(res, "metadata") and res.metadata else 0
            page = self._to_page(res, depth=depth, topic_filter=topic_filter)
            result.pages.append(page)

        logger.info("Deep crawl of %s: %d pages, %d errors", url, len(result.pages), len(result.errors))

    @staticmethod
    def _to_page(res: object, depth: int, topic_filter: str | None) -> CrawlPage:
        url = getattr(res, "url", "")
        title = ""
        # crawl4ai puts the page title in metadata or in the markdown H1
        if hasattr(res, "metadata") and isinstance(res.metadata, dict):
            title = res.metadata.get("title", "")

        markdown = getattr(res, "markdown", "") or ""
        if not markdown and hasattr(res, "cleaned_html"):
            markdown = getattr(res, "cleaned_html", "") or ""

        if topic_filter and markdown:
            markdown = _filter_by_topic(markdown, topic_filter)

        return CrawlPage(url=url, title=title, markdown=markdown.strip(), depth=depth)


def _filter_by_topic(markdown: str, topic: str) -> str:
    """
    Keep only paragraphs/sections that contain any keyword from topic_filter.
    Falls back to full text if nothing matches (better than returning nothing).
    """
    keywords = [kw.strip().lower() for kw in topic.replace(",", " ").split() if kw.strip()]
    if not keywords:
        return markdown

    paragraphs = markdown.split("\n\n")
    matched = [p for p in paragraphs if any(kw in p.lower() for kw in keywords)]

    if len(matched) < 3:
        # Too few matches — return full text so the LLM can decide
        return markdown
    return "\n\n".join(matched)
