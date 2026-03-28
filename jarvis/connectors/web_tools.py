"""LLM tool schemas and handler for Playwright-backed web crawling."""

from __future__ import annotations

import logging
from typing import Any

from jarvis.connectors.web_crawler import WebCrawler

logger = logging.getLogger(__name__)

WEB_TOOLS = [
    {
        "name": "web_crawl",
        "description": (
            "Fetch and extract text content from a web page or recursively from multiple linked pages. "
            "Uses a real Playwright browser — works on JavaScript-heavy sites. "
            "For a news listing page set max_depth=2 to also fetch each linked article. "
            "Returns clean markdown text suitable for summarisation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch, e.g. 'https://www.bbc.com/news'",
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "How many link-levels deep to crawl. "
                        "1 = only the given URL. "
                        "2 = the URL plus all pages linked from it (ideal for news listing pages). "
                        "Default: 1."
                    ),
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Maximum number of pages to visit in total. Default: 10. Max recommended: 20.",
                },
                "topic_filter": {
                    "type": "string",
                    "description": (
                        "Optional keywords (comma- or space-separated) to pre-filter page content. "
                        "Only paragraphs mentioning these keywords are kept. "
                        "Leave empty to return full page content."
                    ),
                },
            },
            "required": ["url"],
        },
    },
]

_crawler = WebCrawler()


class WebToolHandler:
    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name != "web_crawl":
            raise ValueError(f"Unknown web tool: {tool_name}")

        url = tool_input["url"]
        max_depth = int(tool_input.get("max_depth", 1))
        max_pages = int(tool_input.get("max_pages", 10))
        topic_filter = tool_input.get("topic_filter") or None

        # Clamp to sane limits
        max_depth = min(max_depth, 3)
        max_pages = min(max_pages, 20)

        logger.info("web_crawl: url=%s depth=%d pages=%d filter=%r", url, max_depth, max_pages, topic_filter)

        result = await _crawler.crawl(
            url=url,
            max_depth=max_depth,
            max_pages=max_pages,
            topic_filter=topic_filter,
        )

        if not result.pages and result.errors:
            return {"error": result.errors[0]}

        text = result.to_text()
        logger.info("web_crawl: returned %d chars from %d pages", len(text), len(result.pages))
        return text
