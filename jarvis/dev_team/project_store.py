"""Persistent storage for dev team projects and features (stored in jarvis_tasks.db)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

_CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS dev_projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    folder      TEXT NOT NULL,
    raw_request TEXT NOT NULL,
    spec        TEXT,
    arch_doc    TEXT,
    status      TEXT NOT NULL DEFAULT 'planning',
    retries     INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

_CREATE_FEATURES = """
CREATE TABLE IF NOT EXISTS dev_features (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    assigned_to TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    output      TEXT,
    error       TEXT,
    attempt     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
)
"""

# Status values for dev_projects
STATUS_PLANNING     = "planning"
STATUS_ARCHITECTING = "architecting"
STATUS_DEVELOPING   = "developing"
STATUS_TESTING      = "testing"
STATUS_RETRYING     = "retrying"
STATUS_DONE         = "done"
STATUS_FAILED       = "failed"

# Status values for dev_features
FEAT_PENDING    = "pending"
FEAT_RUNNING    = "running"
FEAT_DONE       = "done"
FEAT_FAILED     = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class DevProject:
    id: str
    folder: str
    raw_request: str
    name: str = ""
    spec: str | None = None
    arch_doc: str | None = None
    status: str = STATUS_PLANNING
    retries: int = 0
    max_retries: int = 3
    error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class DevFeature:
    id: str
    project_id: str
    name: str
    description: str
    assigned_to: str          # "claude" | "codex"
    status: str = FEAT_PENDING
    output: str | None = None
    error: str | None = None
    attempt: int = 1
    updated_at: str = field(default_factory=_now)


class ProjectStore:
    def __init__(self, db_path: str = "jarvis_tasks.db") -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_PROJECTS)
            await db.execute(_CREATE_FEATURES)
            await db.commit()

    # ── Projects ───────────────────────────────────────────────────────────────

    async def create_project(self, project: DevProject) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO dev_projects
                   (id, name, folder, raw_request, spec, arch_doc, status,
                    retries, max_retries, error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project.id, project.name, project.folder, project.raw_request,
                 project.spec, project.arch_doc, project.status,
                 project.retries, project.max_retries, project.error,
                 project.created_at, project.updated_at),
            )
            await db.commit()

    async def update_project(self, project_id: str, **kwargs: Any) -> None:
        kwargs["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [project_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE dev_projects SET {cols} WHERE id=?", vals
            )
            await db.commit()

    async def get_project(self, project_id: str) -> DevProject | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM dev_projects WHERE id=?", (project_id,)
            )
            row = await cur.fetchone()
        return _row_to_project(row) if row else None

    async def list_projects(self, limit: int = 20) -> list[DevProject]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM dev_projects ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cur.fetchall()
        return [_row_to_project(r) for r in rows]

    # ── Features ───────────────────────────────────────────────────────────────

    async def save_feature(self, feature: DevFeature) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO dev_features
                   (id, project_id, name, description, assigned_to,
                    status, output, error, attempt, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       status=excluded.status,
                       output=excluded.output,
                       error=excluded.error,
                       attempt=excluded.attempt,
                       assigned_to=excluded.assigned_to,
                       updated_at=excluded.updated_at""",
                (feature.id, feature.project_id, feature.name, feature.description,
                 feature.assigned_to, feature.status, feature.output,
                 feature.error, feature.attempt, feature.updated_at),
            )
            await db.commit()

    async def get_features(self, project_id: str) -> list[DevFeature]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM dev_features WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )
            rows = await cur.fetchall()
        return [_row_to_feature(r) for r in rows]


def _row_to_project(row: Any) -> DevProject:
    return DevProject(
        id=row["id"],
        name=row["name"] or "",
        folder=row["folder"],
        raw_request=row["raw_request"],
        spec=row["spec"],
        arch_doc=row["arch_doc"],
        status=row["status"],
        retries=row["retries"],
        max_retries=row["max_retries"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_feature(row: Any) -> DevFeature:
    return DevFeature(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        description=row["description"],
        assigned_to=row["assigned_to"],
        status=row["status"],
        output=row["output"],
        error=row["error"],
        attempt=row["attempt"],
        updated_at=row["updated_at"],
    )
