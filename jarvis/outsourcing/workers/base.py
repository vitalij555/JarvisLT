"""Base classes for CLI worker agents (claude, codex)."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # seconds


@dataclass
class WorkerResult:
    success: bool
    output: str = ""
    error: str = ""
    skipped: bool = False
    worker_name: str = ""


class BaseWorker(ABC):
    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def is_available(self) -> bool: ...

    @abstractmethod
    async def run(self, prompt: str) -> WorkerResult: ...

    async def _run_subprocess(self, args: list[str], prompt: str) -> WorkerResult:
        """Common subprocess runner with timeout handling."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return WorkerResult(
                    success=False,
                    error=f"Timed out after {self._timeout}s",
                    worker_name=self.name,
                )

            if proc.returncode != 0:
                return WorkerResult(
                    success=False,
                    error=stderr.decode(errors="replace")[:1000],
                    worker_name=self.name,
                )
            return WorkerResult(
                success=True,
                output=stdout.decode(errors="replace"),
                worker_name=self.name,
            )
        except FileNotFoundError:
            return WorkerResult(
                success=False,
                skipped=True,
                error=f"{self.name} CLI not found in PATH",
                worker_name=self.name,
            )
        except Exception as exc:
            logger.error("Worker %s subprocess error: %s", self.name, exc)
            return WorkerResult(
                success=False,
                error=str(exc),
                worker_name=self.name,
            )

    async def _check_command_exists(self, cmd: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False
