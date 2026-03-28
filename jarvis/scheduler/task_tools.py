"""LLM tool schemas and handler for scheduled task management (voice-driven)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.scheduler.task_runner import TaskRunner

from jarvis.scheduler.task_store import TaskDefinition

logger = logging.getLogger(__name__)

TASK_TOOLS = [
    {
        "name": "task_create",
        "description": (
            "Create a new scheduled task that Jarvis will run automatically. "
            "Use cron_expr for specific schedules (e.g. '0 7 * * *' for 7am daily) "
            "or interval_minutes for recurring intervals. Provide only one of the two."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Unique short name for the task, e.g. 'morning_news', 'school_emails'",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The prompt Jarvis will run for this task. "
                        "You can use {date} and {time} placeholders."
                    ),
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression, e.g. '0 7 * * *' (7am daily), '0 9 * * 5' (Friday 9am)",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Run every N minutes instead of a cron schedule",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether to enable the task immediately (default: true)",
                },
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "task_list",
        "description": "List all scheduled tasks with their schedule, status, and next run time.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "task_delete",
        "description": "Permanently delete a scheduled task by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The task name to delete",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "task_enable",
        "description": "Enable a previously disabled scheduled task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The task name to enable"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "task_disable",
        "description": "Disable a scheduled task without deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The task name to disable"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "task_get_recent_results",
        "description": (
            "Retrieve results from scheduled tasks that ran recently. "
            "Use this when the user asks 'what happened while I was sleeping' or "
            "'what did you find out about X'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look (default: 8)",
                }
            },
        },
    },
]


class TaskToolHandler:
    def __init__(self, runner: TaskRunner) -> None:
        self._runner = runner

    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        try:
            if tool_name == "task_create":
                return await self._create(tool_input)
            if tool_name == "task_list":
                return await self._list()
            if tool_name == "task_delete":
                return await self._delete(tool_input["name"])
            if tool_name == "task_enable":
                return await self._set_enabled(tool_input["name"], True)
            if tool_name == "task_disable":
                return await self._set_enabled(tool_input["name"], False)
            if tool_name == "task_get_recent_results":
                return await self._recent_results(tool_input.get("hours", 8))
            raise ValueError(f"Unknown task tool: {tool_name}")
        except Exception as exc:
            logger.error("Task tool '%s' error: %s", tool_name, exc)
            return {"error": str(exc)}

    async def _create(self, inp: dict[str, Any]) -> dict[str, Any]:
        if not inp.get("cron_expr") and not inp.get("interval_minutes"):
            return {"error": "Provide either cron_expr or interval_minutes"}
        task = TaskDefinition(
            name=inp["name"],
            prompt_template=inp["prompt"],
            cron_expr=inp.get("cron_expr"),
            interval_minutes=inp.get("interval_minutes"),
            enabled=inp.get("enabled", True),
            source="voice",
        )
        await self._runner.create_task(task)
        schedule = inp.get("cron_expr") or f"every {inp.get('interval_minutes')} minutes"
        return {"status": "created", "name": inp["name"], "schedule": schedule}

    async def _list(self) -> str:
        tasks = await self._runner.list_tasks_with_next_run()
        if not tasks:
            return "No scheduled tasks configured."
        lines = ["**Scheduled tasks:**"]
        for t in tasks:
            status = "enabled" if t["enabled"] else "disabled"
            lines.append(
                f"• **{t['name']}** [{status}] — {t['schedule']} — next: {t['next_run']}"
            )
        return "\n".join(lines)

    async def _delete(self, name: str) -> dict[str, Any]:
        ok = await self._runner.delete_task(name)
        return {"status": "deleted" if ok else "not_found", "name": name}

    async def _set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        if enabled:
            ok = await self._runner.enable_task(name)
        else:
            ok = await self._runner.disable_task(name)
        action = "enabled" if enabled else "disabled"
        return {"status": action if ok else "not_found", "name": name}

    async def _recent_results(self, hours: int) -> str:
        runs = await self._runner.get_recent_results(hours)
        if not runs:
            return f"No scheduled tasks ran in the last {hours} hours."
        lines = [f"**Task results from the last {hours} hours:**"]
        for run in runs:
            status = run.get("status", "?")
            name = run.get("task_name", "?")
            completed = run.get("completed_at", "?")
            output = run.get("output") or run.get("error") or "(no output)"
            lines.append(f"\n**{name}** [{status}] at {completed}:\n{output[:500]}")
        return "\n".join(lines)
