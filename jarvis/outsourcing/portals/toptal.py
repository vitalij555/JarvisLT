"""Toptal portal configuration and helpers."""

from __future__ import annotations

# Public job listing URL (no auth required for the index; individual listings may need auth)
LISTINGS_URL = "https://www.toptal.com/freelance-jobs"

# Path to Playwright session state file (written once after manual login)
SESSION_STATE_FILE = "toptal_session.json"

# Crawl config
MAX_DEPTH = 2
MAX_PAGES = 30
TOPIC_FILTER = "developer,engineer,backend,python,api,llm,ai,ml,software"

# Strings that indicate we've been redirected to a login wall
_AUTH_INDICATORS = [
    "sign in to toptal",
    "log in to toptal",
    "create a toptal account",
    "toptal login",
    "/login",
    "please log in",
]


def detect_auth_redirect(markdown: str) -> bool:
    """Return True if the crawled content looks like a login page."""
    lowered = markdown.lower()
    return any(indicator in lowered for indicator in _AUTH_INDICATORS)


def is_listing_url(url: str) -> bool:
    """Return True if the URL looks like an individual job listing."""
    return "/freelance-jobs/" in url and url != LISTINGS_URL


def parse_listing_title(markdown: str, url: str) -> str:
    """Extract a best-effort title from the crawled markdown."""
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("# ") and len(line) > 3:
            return line[2:].strip()
        if line.startswith("## ") and len(line) > 4:
            return line[3:].strip()
    # Fallback: derive from URL slug
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()
