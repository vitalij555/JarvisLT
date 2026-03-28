"""LLM tool schemas and handler for the local dev team."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis.dev_team.pipeline import DevTeamPipeline
from jarvis.dev_team.project_store import ProjectStore

logger = logging.getLogger(__name__)

DEV_TEAM_TOOLS = [
    {
        "name": "dev_team_build",
        "description": (
            "Ask the local AI dev team to build a software tool or project in a specific folder. "
            "The team works in the background and notifies you via voice when done. "
            "Use this when the user says 'build me X', 'create a tool to Y', "
            "'ask the team to implement Z', 'write a script that does W', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "What to build — describe it in natural language",
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "Absolute path to the target folder where the project will be built. "
                        "The folder will be created if it does not exist."
                    ),
                },
            },
            "required": ["request", "folder"],
        },
    },
    {
        "name": "dev_team_status",
        "description": (
            "Check the status of the dev team's current or recent projects. "
            "Use when the user asks 'how is the build going?', 'is it done?', "
            "'what projects has the team worked on?', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional: check a specific project by ID prefix",
                },
            },
        },
    },
]


class DevTeamToolHandler:
    def __init__(
        self,
        config: dict,
        pending_queue: asyncio.Queue,
    ) -> None:
        db_path = config.get("db_path", "jarvis_tasks.db")
        self._store = ProjectStore(db_path)
        self._pipeline = DevTeamPipeline(self._store, pending_queue, config)
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self._store.init()
            self._initialized = True

    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        await self._ensure_init()
        try:
            if tool_name == "dev_team_build":
                return await self._build(
                    tool_input["request"], tool_input["folder"]
                )
            if tool_name == "dev_team_status":
                return await self._status(tool_input.get("project_id"))
            raise ValueError(f"Unknown tool: {tool_name}")
        except Exception as exc:
            logger.error("DevTeam tool %s error: %s", tool_name, exc)
            return {"error": str(exc)}

    async def _build(self, request: str, folder: str) -> dict:
        project_id = await self._pipeline.start_project(request, folder)
        return {
            "status": "started",
            "project_id": project_id,
            "message": (
                "Got it. I've handed the project to the dev team. "
                "I'll let you know when they're done."
            ),
        }

    async def _status(self, project_id_prefix: str | None) -> str:
        if project_id_prefix:
            projects = await self._store.list_projects(limit=50)
            project = next(
                (p for p in projects if p.id.startswith(project_id_prefix)), None
            )
            if not project:
                return f"No project found matching ID '{project_id_prefix}'."
            features = await self._store.get_features(project.id)
            feat_lines = "\n".join(
                f"  [{f.status}] {f.name} (assigned to {f.assigned_to}, attempt {f.attempt})"
                for f in features
            )
            return (
                f"**{project.name or project.id[:8]}** — {project.status.upper()}\n"
                f"Folder: {project.folder}\n"
                f"Retries: {project.retries}/{project.max_retries}\n"
                f"Features:\n{feat_lines or '  (none yet)'}\n"
                + (f"Error: {project.error}" if project.error else "")
            )

        projects = await self._store.list_projects(limit=10)
        if not projects:
            return "No dev team projects yet. Say 'build me X in folder Y' to start one."

        lines = ["**Dev Team Projects:**\n"]
        status_icon = {
            "planning": "📋", "architecting": "🏗️", "developing": "⚙️",
            "testing": "🧪", "retrying": "🔄", "done": "✅", "failed": "❌",
        }
        for p in projects:
            icon = status_icon.get(p.status, "•")
            lines.append(
                f"{icon} **{p.name or p.id[:8]}** [{p.status}]\n"
                f"   Folder: {p.folder} | ID: {p.id[:8]}\n"
            )
        return "\n".join(lines)
