"""LLM tool schemas and handler for long-term memory operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)

MEMORY_TOOLS = [
    {
        "name": "memory_remember",
        "description": (
            "Store a fact about a person, place, event, or preference in long-term memory. "
            "Use this when the user tells you something important to remember for future conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Name of the entity (person, place, event, etc.), e.g. 'Sofia', 'Dentist appointment'",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "place", "event", "preference", "task", "other"],
                    "description": "Category of the entity",
                },
                "fact_key": {
                    "type": "string",
                    "description": "Short label for the fact, e.g. 'school', 'date', 'music_preference'",
                },
                "fact_value": {
                    "type": "string",
                    "description": "The value to remember, e.g. 'Vilnius Gymnasium No.5', '2025-05-03', 'jazz'",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional extra context about the entity, e.g. 'User\\'s daughter'",
                },
            },
            "required": ["entity_name", "entity_type", "fact_key", "fact_value"],
        },
    },
    {
        "name": "memory_recall",
        "description": (
            "Look up what Jarvis knows about a topic, person, or subject in long-term memory. "
            "Returns stored facts and relationships."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "What to search for, e.g. 'Sofia', 'dentist', 'school'",
                }
            },
            "required": ["topic"],
        },
    },
    {
        "name": "memory_add_relationship",
        "description": "Record that two entities are related, e.g. 'Sofia' is 'daughter_of' 'User'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "Source entity name",
                },
                "to_name": {
                    "type": "string",
                    "description": "Target entity name",
                },
                "label": {
                    "type": "string",
                    "description": "Relationship label, e.g. 'daughter_of', 'attends', 'works_at'",
                },
            },
            "required": ["from_name", "to_name", "label"],
        },
    },
    {
        "name": "memory_list_entities",
        "description": "List all known entities of a given type from long-term memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "place", "event", "preference", "task", "other"],
                    "description": "Entity type to list",
                }
            },
            "required": ["entity_type"],
        },
    },
    {
        "name": "memory_search_history",
        "description": (
            "Search past conversations and task results semantically. "
            "Use this when the user asks about something said or discovered in a previous session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for, e.g. 'dentist appointment', 'news about AI last week'",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_forget",
        "description": (
            "Delete a fact or an entire entity from long-term memory. "
            "If fact_key is provided, only that fact is deleted. "
            "If only entity_name is provided, the entire entity and all its facts are deleted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Name of the entity to forget",
                },
                "fact_key": {
                    "type": "string",
                    "description": "Specific fact to delete (optional — omit to delete the whole entity)",
                },
            },
            "required": ["entity_name"],
        },
    },
]


class MemoryToolHandler:
    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        try:
            if tool_name == "memory_remember":
                return await self._manager.remember_entity(
                    name=tool_input["entity_name"],
                    entity_type=tool_input["entity_type"],
                    fact_key=tool_input["fact_key"],
                    fact_value=tool_input["fact_value"],
                    notes=tool_input.get("notes", ""),
                )
            if tool_name == "memory_recall":
                return await self._manager.recall_about(tool_input["topic"])
            if tool_name == "memory_add_relationship":
                return await self._manager.remember_relationship(
                    from_name=tool_input["from_name"],
                    to_name=tool_input["to_name"],
                    label=tool_input["label"],
                )
            if tool_name == "memory_list_entities":
                return await self._manager.list_entities(tool_input["entity_type"])
            if tool_name == "memory_search_history":
                return await self._manager.search_history(tool_input["query"])
            if tool_name == "memory_forget":
                return await self._manager.forget(
                    entity_name=tool_input["entity_name"],
                    fact_key=tool_input.get("fact_key"),
                )
            raise ValueError(f"Unknown memory tool: {tool_name}")
        except Exception as exc:
            logger.error("Memory tool '%s' error: %s", tool_name, exc)
            return {"error": str(exc)}
