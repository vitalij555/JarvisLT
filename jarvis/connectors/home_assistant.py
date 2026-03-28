"""Home Assistant connector — exposes HA actions as Claude tools."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tool schemas for Claude
HA_TOOLS = [
    {
        "name": "ha_get_state",
        "description": "Get the current state of a Home Assistant entity (light, switch, sensor, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The entity ID, e.g. 'light.living_room' or 'sensor.temperature'",
                }
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "ha_call_service",
        "description": "Call a Home Assistant service to control devices (turn on/off lights, lock doors, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Service domain, e.g. 'light', 'switch', 'climate', 'media_player'",
                },
                "service": {
                    "type": "string",
                    "description": "Service name, e.g. 'turn_on', 'turn_off', 'toggle'",
                },
                "data": {
                    "type": "object",
                    "description": "Service data, e.g. {'entity_id': 'light.kitchen', 'brightness': 128}",
                },
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "ha_list_entities",
        "description": "List Home Assistant entities, optionally filtered by domain",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter by domain, e.g. 'light', 'switch'. Omit for all entities.",
                }
            },
        },
    },
]


class HomeAssistantConnector:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from homeassistant_api import Client
                self._client = Client(self.url, self.token)
            except ImportError:
                raise RuntimeError("homeassistant-api not installed. Run: pip install homeassistant-api")
        return self._client

    def get_state(self, entity_id: str) -> dict[str, Any]:
        client = self._get_client()
        try:
            state = client.get_state(entity_id=entity_id)
            return {"entity_id": entity_id, "state": state.state, "attributes": state.attributes}
        except Exception as exc:
            logger.error("HA get_state failed for %s: %s", entity_id, exc)
            return {"error": str(exc)}

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._get_client()
        try:
            client.trigger_service(domain, service, **(data or {}))
            return {"success": True, "domain": domain, "service": service}
        except Exception as exc:
            logger.error("HA call_service %s.%s failed: %s", domain, service, exc)
            return {"error": str(exc)}

    def list_entities(self, domain: str | None = None) -> list[dict[str, Any]]:
        client = self._get_client()
        try:
            entities = client.get_entities()
            result = []
            for entity_id, state in entities.items():
                if domain and not entity_id.startswith(f"{domain}."):
                    continue
                result.append({"entity_id": entity_id, "state": state.state})
            return result
        except Exception as exc:
            logger.error("HA list_entities failed: %s", exc)
            return [{"error": str(exc)}]

    def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name == "ha_get_state":
            return self.get_state(tool_input["entity_id"])
        if tool_name == "ha_call_service":
            return self.call_service(
                tool_input["domain"],
                tool_input["service"],
                tool_input.get("data"),
            )
        if tool_name == "ha_list_entities":
            return self.list_entities(tool_input.get("domain"))
        raise ValueError(f"Unknown HA tool: {tool_name}")
