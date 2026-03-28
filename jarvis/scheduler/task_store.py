"""Persistent storage for scheduled task definitions and run history."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

DB_PATH = "jarvis_tasks.db"

_CREATE_DEFINITIONS = """
CREATE TABLE IF NOT EXISTS task_definitions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    cron_expr       TEXT,
    interval_minutes INTEGER,
    prompt_template TEXT NOT NULL,
    delivery        TEXT NOT NULL DEFAULT 'store',
    enabled         INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'config',
    created_at      TEXT NOT NULL
)
"""

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS task_runs (
    id              TEXT PRIMARY KEY,
    task_name       TEXT NOT NULL,
    scheduled_for   TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    output          TEXT,
    error           TEXT
)
"""


@dataclass
class TaskDefinition:
    name: str
    prompt_template: str
    cron_expr: str | None = None
    interval_minutes: int | None = None
    delivery: str = "store"
    enabled: bool = True
    source: str = "config"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: _now())


@dataclass
class TaskRun:
    task_name: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    scheduled_for: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    status: str = "running"
    output: str | None = None
    error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_DEFINITIONS)
            await db.execute(_CREATE_RUNS)
            await db.commit()

    # --- Task definitions ---

    async def save_task(self, task: TaskDefinition) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO task_definitions
                   (id, name, cron_expr, interval_minutes, prompt_template,
                    delivery, enabled, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       cron_expr=excluded.cron_expr,
                       interval_minutes=excluded.interval_minutes,
                       prompt_template=excluded.prompt_template,
                       delivery=excluded.delivery,
                       enabled=excluded.enabled,
                       source=excluded.source""",
                (task.id, task.name, task.cron_expr, task.interval_minutes,
                 task.prompt_template, task.delivery, int(task.enabled),
                 task.source, task.created_at),
            )
            await db.commit()

    async def delete_task(self, name: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM task_definitions WHERE name=?", (name,))
            await db.commit()
            return cur.rowcount > 0

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE task_definitions SET enabled=? WHERE name=?",
                (int(enabled), name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_tasks(self) -> list[TaskDefinition]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM task_definitions ORDER BY name")
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def get_task(self, name: str) -> TaskDefinition | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM task_definitions WHERE name=?", (name,)
            )
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    # --- Task runs ---

    async def start_run(self, run: TaskRun) -> None:
        run.started_at = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO task_runs
                   (id, task_name, scheduled_for, started_at, status)
                   VALUES (?,?,?,?,?)""",
                (run.id, run.task_name, run.scheduled_for, run.started_at, run.status),
            )
            await db.commit()

    async def complete_run(self, run: TaskRun) -> None:
        run.completed_at = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE task_runs
                   SET completed_at=?, status=?, output=?, error=?
                   WHERE id=?""",
                (run.completed_at, run.status, run.output, run.error, run.id),
            )
            await db.commit()

    async def get_recent_runs(self, hours: int = 8) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM task_runs
                   WHERE completed_at >= datetime('now', ?)
                   ORDER BY completed_at DESC""",
                (f"-{hours} hours",),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _row_to_task(row: Any) -> TaskDefinition:
    return TaskDefinition(
        id=row["id"],
        name=row["name"],
        cron_expr=row["cron_expr"],
        interval_minutes=row["interval_minutes"],
        prompt_template=row["prompt_template"],
        delivery=row["delivery"],
        enabled=bool(row["enabled"]),
        source=row["source"],
        created_at=row["created_at"],
    )
