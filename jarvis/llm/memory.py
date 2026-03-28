"""Conversation memory — persists turn history across restarts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class Turn(TypedDict):
    role: str   # "user" or "assistant"
    content: str


class ConversationMemory:
    def __init__(self, max_turns: int = 20, persist_path: str | None = "conversation_history.json") -> None:
        self.max_turns = max_turns
        self.persist_path = Path(persist_path) if persist_path else None
        self._turns: list[Turn] = []
        self._load()

    def add_turn(self, role: str, content: str) -> None:
        self._turns.append({"role": role, "content": content})
        # Keep only the most recent max_turns pairs (2 messages per pair)
        if len(self._turns) > self.max_turns * 2:
            self._turns = self._turns[-(self.max_turns * 2):]

    def get_context(self) -> list[Turn]:
        """Return turns as a list suitable for the Claude messages API."""
        return list(self._turns)

    def clear(self) -> None:
        self._turns = []
        self.save()

    def save(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.write_text(json.dumps(self._turns, indent=2))
        except OSError as exc:
            logger.warning("Could not save conversation history: %s", exc)

    def _load(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text())
            if isinstance(data, list):
                self._turns = data
                logger.info("Loaded %d turns from %s", len(self._turns), self.persist_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load conversation history: %s", exc)
