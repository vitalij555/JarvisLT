"""Worker that calls the `claude` CLI in file-writing mode (--dangerously-skip-permissions).

Distinct from ClaudeWorker (--print, read-only output). Use this when the worker
must create or modify files on disk — e.g. architects and developers in dev_team.
"""

from __future__ import annotations

import logging

from jarvis.outsourcing.workers.base import BaseWorker, WorkerResult

logger = logging.getLogger(__name__)


class ClaudeFileWorker(BaseWorker):
    """Invokes `claude --dangerously-skip-permissions -p <prompt>` in an optional cwd.

    The `-p` flag makes Claude Code non-interactive (prints final response to stdout).
    `--dangerously-skip-permissions` allows tool use (Write, Edit, Bash) without prompts,
    so the agent can create and modify files in the working directory.
    """

    @property
    def name(self) -> str:
        return "claude_file"

    async def is_available(self) -> bool:
        return await self._check_command_exists("claude")

    async def run(self, prompt: str, cwd: str | None = None) -> WorkerResult:
        logger.info("ClaudeFileWorker: running prompt (%d chars) in cwd=%s", len(prompt), cwd)
        return await self._run_subprocess(
            ["claude", "--dangerously-skip-permissions", "-p", prompt],
            prompt,
            cwd=cwd,
        )
