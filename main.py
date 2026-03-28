"""JarvisLT — entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import yaml
from dotenv import load_dotenv

from jarvis.core.assistant import Assistant


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def write_gauth_from_env() -> None:
    """Generate .gauth.json from env vars if credentials are provided."""
    client_id = os.environ.get("GOOGLE_OAUTH_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_SECRET")
    if client_id and client_secret:
        with open(".gauth.json", "w") as f:
            json.dump({"installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": ["http://localhost:4100/code"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }}, f)


def main() -> None:
    load_dotenv()
    setup_logging()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set. Copy .env.example to .env and fill in your key.")
        sys.exit(1)

    write_gauth_from_env()

    email = os.environ.get("GOOGLE_ACCOUNT_EMAIL", "vitalij555@gmail.com")
    cred_path = f".oauth2.{email}.json"
    if not os.path.exists(cred_path):
        print("Google credentials not found. Starting OAuth flow...")
        import auth_google  # noqa: F401 — runs the auth flow as a side effect
        if not os.path.exists(cred_path):
            print("ERROR: OAuth flow did not complete. Exiting.")
            sys.exit(1)

    config = load_config()
    assistant = Assistant(config)
    asyncio.run(assistant.run())


if __name__ == "__main__":
    main()
