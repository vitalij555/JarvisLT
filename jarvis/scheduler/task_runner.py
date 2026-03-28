"""Scheduled task runner using APScheduler with a headless LLM session."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jarvis.llm.memory import ConversationMemory
from jarvis.scheduler.task_store import TaskDefinition, TaskRun, TaskStore

if TYPE_CHECKING:
    from jarvis.llm.claude_client import LLMClient

logger = logging.getLogger(__name__)


class HeadlessSession:
    """Runs a single LLM prompt+tool-loop without any audio, reusing the live LLMClient."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def run(self, prompt: str) -> str:
        memory = ConversationMemory(max_turns=10, persist_path=None)
        try:
            return await self._llm.chat_async(prompt, memory)
        except Exception as exc:
            logger.error("HeadlessSession error: %s", exc)
            raise


class TaskRunner:
    def __init__(
        self,
        static_tasks: dict[str, Any],
        llm: LLMClient,
        db_path: str = "jarvis_tasks.db",
        memory_manager: Any | None = None,
    ) -> None:
        self._static_tasks = static_tasks
        self._llm = llm
        self._memory_manager = memory_manager
        self._store = TaskStore(db_path)
        self._session = HeadlessSession(llm)
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        await self._store.init()

        # Load and schedule static tasks from config
        for name, cfg in (self._static_tasks or {}).items():
            if not isinstance(cfg, dict):
                continue
            task = TaskDefinition(
                name=name,
                prompt_template=cfg.get("prompt", ""),
                cron_expr=cfg.get("cron"),
                interval_minutes=cfg.get("interval_minutes"),
                enabled=cfg.get("enabled", True),
                source="config",
            )
            await self._store.save_task(task)
            if task.enabled:
                self._schedule_task(task)

        # Load and schedule persisted voice-created tasks
        for task in await self._store.list_tasks():
            if task.source == "voice" and task.enabled:
                self._schedule_task(task)

        self._scheduler.start()
        logger.info("TaskRunner started — %d jobs scheduled", len(self._scheduler.get_jobs()))

    def _schedule_task(self, task: TaskDefinition) -> None:
        if task.cron_expr:
            try:
                trigger = CronTrigger.from_crontab(task.cron_expr)
            except Exception as exc:
                logger.error("Invalid cron '%s' for task '%s': %s", task.cron_expr, task.name, exc)
                return
        elif task.interval_minutes:
            trigger = IntervalTrigger(minutes=task.interval_minutes)
        else:
            logger.warning("Task '%s' has no schedule — skipping", task.name)
            return

        self._scheduler.add_job(
            self._run_task,
            trigger=trigger,
            args=[task.name],
            id=f"task_{task.name}",
            name=task.name,
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info("Scheduled task '%s' (%s)", task.name,
                    task.cron_expr or f"every {task.interval_minutes}m")

    async def _run_task(self, task_name: str) -> None:
        task = await self._store.get_task(task_name)
        if not task or not task.enabled:
            return

        now = datetime.now(timezone.utc)
        prompt = task.prompt_template.format(
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M"),
        )

        run = TaskRun(task_name=task_name, scheduled_for=now.isoformat())
        await self._store.start_run(run)
        logger.info("Running scheduled task '%s'", task_name)

        try:
            output = await self._session.run(prompt)
            run.status = "success"
            run.output = output
            logger.info("Task '%s' completed (%d chars)", task_name, len(output))
        except Exception as exc:
            run.status = "error"
            run.error = str(exc)
            logger.error("Task '%s' failed: %s", task_name, exc)

        await self._store.complete_run(run)

        if run.status == "success" and self._memory_manager and run.output:
            try:
                await self._memory_manager.store_task_output(task_name, run.output)
            except Exception as exc:
                logger.warning("Failed to store task output in memory: %s", exc)

    # --- Runtime task management (used by task_tools in Phase 4) ---

    async def create_task(self, task: TaskDefinition) -> None:
        await self._store.save_task(task)
        if task.enabled:
            self._schedule_task(task)

    async def delete_task(self, name: str) -> bool:
        job_id = f"task_{name}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        return await self._store.delete_task(name)

    async def enable_task(self, name: str) -> bool:
        ok = await self._store.set_enabled(name, True)
        if ok:
            task = await self._store.get_task(name)
            if task:
                self._schedule_task(task)
        return ok

    async def disable_task(self, name: str) -> bool:
        ok = await self._store.set_enabled(name, False)
        job_id = f"task_{name}"
        if ok and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        return ok

    async def list_tasks_with_next_run(self) -> list[dict[str, Any]]:
        tasks = await self._store.list_tasks()
        result = []
        for t in tasks:
            job = self._scheduler.get_job(f"task_{t.name}")
            next_run = str(job.next_run_time) if job and job.next_run_time else "not scheduled"
            result.append({
                "name": t.name,
                "schedule": t.cron_expr or f"every {t.interval_minutes}m",
                "enabled": t.enabled,
                "source": t.source,
                "next_run": next_run,
            })
        return result

    async def get_recent_results(self, hours: int = 8) -> list[dict[str, Any]]:
        return await self._store.get_recent_runs(hours)

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
