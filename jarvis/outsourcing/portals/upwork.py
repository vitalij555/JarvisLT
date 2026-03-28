"""Upwork portal configuration and helpers."""

from __future__ import annotations

# RSS feed — no auth required, returns recent job postings as XML
RSS_URL = (
    "https://www.upwork.com/ab/feed/jobs/rss"
    "?q=python+backend+api+llm&sort=recency&paging=0%3B20"
)

# Web listing fallback (requires login for full details)
LISTINGS_URL = "https://www.upwork.com/nx/find-work/best-matches"

SESSION_STATE_FILE = "upwork_session.json"

MAX_DEPTH = 1
MAX_PAGES = 20
TOPIC_FILTER = "python,api,backend,llm,ai,software,developer"

_AUTH_INDICATORS = [
    "sign up for free",
    "log in to upwork",
    "create account",
    "join upwork",
    "/login",
]


def detect_auth_redirect(markdown: str) -> bool:
    lowered = markdown.lower()
    return any(indicator in lowered for indicator in _AUTH_INDICATORS)


def parse_listing_title(markdown: str, url: str) -> str:
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("# ") and len(line) > 3:
            return line[2:].strip()
        if line.startswith("## ") and len(line) > 4:
            return line[3:].strip()
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()


def parse_rss_listings(xml_text: str, portal: str = "upwork") -> list[dict]:
    """Parse Upwork RSS XML into a list of {title, url, raw_text} dicts."""
    import re
    items = []
    for item_match in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        item_xml = item_match.group(1)

        def extract(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item_xml, re.DOTALL)
            if not m:
                return ""
            # strip CDATA wrapper if present
            content = m.group(1).strip()
            content = re.sub(r"^<!\[CDATA\[", "", content)
            content = re.sub(r"\]\]>$", "", content)
            return content.strip()

        title = extract("title")
        url = extract("link") or extract("guid")
        description = extract("description")
        if url:
            items.append({"title": title, "url": url, "raw_text": description})
    return items
