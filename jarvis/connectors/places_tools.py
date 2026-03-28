"""Google Places API tool — find restaurants, shops, and points of interest nearby."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

PLACES_TOOLS = [
    {
        "name": "search_places",
        "description": (
            "Search for places (restaurants, cafes, shops, services, etc.) using Google Places. "
            "Can find places near an address, in a city, or matching any location description. "
            "Returns name, address, rating, opening hours, and price level. "
            "If the user asks for places 'nearby' or 'near me', use their stored home address from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to search for, e.g. 'Italian restaurants', 'coffee shops', 'pharmacies'. "
                        "Do NOT include location here — use location parameter instead."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": (
                        "Address or area to search near, e.g. 'Gedimino pr. 1, Vilnius' or 'Vilnius city center'. "
                        "Use the user's stored home address if they ask for places 'nearby' or 'near me'."
                    ),
                },
                "radius_meters": {
                    "type": "integer",
                    "description": "Search radius in meters when location is an address (default: 1500, max: 5000)",
                },
                "open_now": {
                    "type": "boolean",
                    "description": "If true, return only places that are currently open",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 10)",
                },
            },
            "required": ["query"],
        },
    },
]


class PlacesToolHandler:
    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name != "search_places":
            raise ValueError(f"Unknown places tool: {tool_name}")

        api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not api_key:
            return {"error": "GOOGLE_PLACES_API_KEY not set in environment. Add it to .env."}

        query = tool_input["query"]
        location = tool_input.get("location", "")
        radius = min(int(tool_input.get("radius_meters", 1500)), 5000)
        open_now = tool_input.get("open_now", False)
        max_results = min(int(tool_input.get("max_results", 5)), 10)

        logger.info("search_places: query=%r location=%r radius=%d", query, location, radius)

        try:
            if location:
                # Geocode the address → use Nearby Search for precise radius control
                coords = await _geocode(location, api_key)
                if coords:
                    return await _nearby_search(query, coords, radius, open_now, max_results, api_key)

            # Fallback: Text Search combines query + location in one call
            return await _text_search(query, location, open_now, max_results, api_key)

        except Exception as exc:
            logger.error("search_places failed: %s", exc)
            return {"error": str(exc)}


async def _geocode(address: str, api_key: str) -> tuple[float, float] | None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(_GEOCODE_URL, params={"address": address, "key": api_key})
        resp.raise_for_status()
        data = resp.json()
    if data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None


async def _nearby_search(
    query: str,
    coords: tuple[float, float],
    radius: int,
    open_now: bool,
    max_results: int,
    api_key: str,
) -> str:
    params: dict[str, Any] = {
        "location": f"{coords[0]},{coords[1]}",
        "radius": radius,
        "keyword": query,
        "key": api_key,
    }
    if open_now:
        params["opennow"] = "true"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_NEARBY_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    return _format_results(data.get("results", [])[:max_results], query)


async def _text_search(
    query: str,
    location: str,
    open_now: bool,
    max_results: int,
    api_key: str,
) -> str:
    full_query = f"{query} near {location}" if location else query
    params: dict[str, Any] = {"query": full_query, "key": api_key}
    if open_now:
        params["opennow"] = "true"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_TEXT_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    return _format_results(data.get("results", [])[:max_results], query)


def _format_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"No places found matching '{query}'."

    lines = [f"**Places matching '{query}':**\n"]
    for i, place in enumerate(results, 1):
        name = place.get("name", "Unknown")
        address = place.get("formatted_address") or place.get("vicinity", "")
        rating = place.get("rating")
        total_ratings = place.get("user_ratings_total", 0)
        price = "💰" * place.get("price_level", 0) if place.get("price_level") else ""
        hours = place.get("opening_hours", {})
        open_status = ""
        if "open_now" in hours:
            open_status = "🟢 Open now" if hours["open_now"] else "🔴 Closed now"

        rating_str = f"⭐ {rating}/5 ({total_ratings} reviews)" if rating else ""

        parts = [f"{i}. **{name}**"]
        if address:
            parts.append(f"   📍 {address}")
        if rating_str:
            parts.append(f"   {rating_str}")
        if price:
            parts.append(f"   Price: {price}")
        if open_status:
            parts.append(f"   {open_status}")

        lines.append("\n".join(parts))

    return "\n\n".join(lines)
