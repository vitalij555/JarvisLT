"""Worker that calls the `claude` CLI (Claude Code) as a subprocess."""

from __future__ import annotations

import logging

from jarvis.outsourcing.workers.base import BaseWorker, WorkerResult

logger = logging.getLogger(__name__)


class ClaudeWorker(BaseWorker):
    """Invokes `claude --print <prompt>` and returns the output."""

    @property
    def name(self) -> str:
        return "claude"

    async def is_available(self) -> bool:
        return await self._check_command_exists("claude")

    async def run(self, prompt: str) -> WorkerResult:
        logger.info("ClaudeWorker: running prompt (%d chars)", len(prompt))
        # --print: non-interactive, output to stdout, no streaming UI
        return await self._run_subprocess(
            ["claude", "--print", prompt],
            prompt,
        )
