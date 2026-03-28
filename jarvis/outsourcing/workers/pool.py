"""Worker pool — selects the best available CLI worker for a task."""

from __future__ import annotations

import logging

from jarvis.outsourcing.workers.base import WorkerResult
from jarvis.outsourcing.workers.claude_worker import ClaudeWorker
from jarvis.outsourcing.workers.codex_worker import CodexWorker

logger = logging.getLogger(__name__)


class WorkerPool:
    """Tries Claude CLI first, falls back to Codex CLI, degrades gracefully if neither available."""

    def __init__(self, timeout: int = 300) -> None:
        self._workers = [ClaudeWorker(timeout=timeout), CodexWorker(timeout=timeout)]

    async def run(self, prompt: str) -> WorkerResult:
        for worker in self._workers:
            if await worker.is_available():
                logger.info("WorkerPool: delegating to %s", worker.name)
                return await worker.run(prompt)

        logger.warning("WorkerPool: no CLI workers available (claude/codex not in PATH)")
        return WorkerResult(
            success=False,
            skipped=True,
            error=(
                "No CLI workers available. Install Claude Code (claude) or "
                "OpenAI Codex CLI (codex) and ensure they are in PATH."
            ),
        )

    async def available_worker_names(self) -> list[str]:
        return [w.name for w in self._workers if await w.is_available()]
