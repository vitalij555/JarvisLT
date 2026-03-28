"""Google search via Serper.dev — real Google results, 2500 free searches/month."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"

SEARCH_TOOLS = [
    {
        "name": "google_search",
        "description": (
            "Search the web using Google (via Serper). "
            "Use this when you need up-to-date information, news, facts, or any topic "
            "that requires a real-time web search. "
            "Returns titles, URLs, and snippets for the top results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query, e.g. 'latest AI news 2025' or 'weather in Vilnius today'",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 10)",
                },
            },
            "required": ["query"],
        },
    },
]


class SearchToolHandler:
    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name != "google_search":
            raise ValueError(f"Unknown search tool: {tool_name}")

        api_key = os.environ.get("SERPER_API_KEY", "")
        if not api_key:
            return {"error": "SERPER_API_KEY not set in environment. Add it to .env."}

        query = tool_input["query"]
        num = min(int(tool_input.get("num_results", 5)), 10)

        logger.info("google_search: %r (n=%d)", query, num)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    SERPER_URL,
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                    json={"q": query, "num": num},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("Serper API error: %s", exc)
            return {"error": f"Search API returned {exc.response.status_code}"}
        except Exception as exc:
            logger.error("Search request failed: %s", exc)
            return {"error": str(exc)}

        return _format_results(data, query)


def _format_results(data: dict[str, Any], query: str) -> str:
    lines: list[str] = [f"**Google search results for: {query}**\n"]

    # Answer box (featured snippet)
    if answer := data.get("answerBox"):
        box_text = answer.get("answer") or answer.get("snippet", "")
        if box_text:
            lines.append(f"**Answer:** {box_text}\n")

    # Knowledge graph
    if kg := data.get("knowledgeGraph"):
        if desc := kg.get("description"):
            lines.append(f"**{kg.get('title', '')}:** {desc}\n")

    # Organic results
    organic = data.get("organic", [])
    if not organic:
        lines.append("No results found.")
        return "\n".join(lines)

    for i, result in enumerate(organic, 1):
        title = result.get("title", "")
        link = result.get("link", "")
        snippet = result.get("snippet", "")
        lines.append(f"{i}. **{title}**\n   {snippet}\n   {link}")

    return "\n\n".join(lines)
