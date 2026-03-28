"""Worker that calls the `codex` CLI (OpenAI Codex) as a subprocess."""

from __future__ import annotations

import logging

from jarvis.outsourcing.workers.base import BaseWorker, WorkerResult

logger = logging.getLogger(__name__)


class CodexWorker(BaseWorker):
    """Invokes `codex --full-auto <prompt>` and returns the output."""

    @property
    def name(self) -> str:
        return "codex"

    async def is_available(self) -> bool:
        return await self._check_command_exists("codex")

    async def run(self, prompt: str, cwd: str | None = None) -> WorkerResult:
        logger.info("CodexWorker: running prompt (%d chars)", len(prompt))
        # --full-auto: non-interactive, no approval prompts
        return await self._run_subprocess(
            ["codex", "--full-auto", prompt],
            prompt,
            cwd=cwd,
        )
