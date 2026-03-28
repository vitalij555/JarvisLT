"""Neo4j-backed entity/relationship/fact store for long-term memory."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = {"person", "place", "event", "preference", "task", "other"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Entity:
    id: str
    name: str
    type: str
    notes: str
    created_at: str
    updated_at: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)


class EntityStore:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._driver: Any = None

    async def init(self) -> None:
        try:
            from neo4j import AsyncGraphDatabase
            self._driver = AsyncGraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
            await self._driver.verify_connectivity()
            await self._create_indexes()
            logger.info("Neo4j connected: %s", self._uri)
        except Exception as exc:
            logger.error("Neo4j connection failed: %s — long-term entity memory disabled", exc)
            self._driver = None

    async def _create_indexes(self) -> None:
        if not self._driver:
            return
        async with self._driver.session() as session:
            await session.run(
                "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)"
            )
            await session.run(
                "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)"
            )

    @property
    def available(self) -> bool:
        return self._driver is not None

    async def upsert_entity(
        self, name: str, entity_type: str, notes: str = ""
    ) -> str | None:
        if not self._driver:
            return None
        entity_type = entity_type if entity_type in VALID_ENTITY_TYPES else "other"
        now = _now()
        async with self._driver.session() as session:
            result = await session.run(
                """MERGE (e:Entity {name: $name})
                   ON CREATE SET e.id = $id, e.type = $type,
                                 e.notes = $notes, e.created_at = $now, e.updated_at = $now
                   ON MATCH SET  e.type = $type,
                                 e.notes = CASE WHEN $notes <> '' THEN $notes ELSE e.notes END,
                                 e.updated_at = $now
                   RETURN e.id AS id""",
                name=name, id=str(uuid.uuid4()), type=entity_type,
                notes=notes, now=now,
            )
            record = await result.single()
            return record["id"] if record else None

    async def add_fact(
        self, entity_name: str, key: str, value: str, source: str = "user_stated"
    ) -> None:
        if not self._driver:
            return
        now = _now()
        async with self._driver.session() as session:
            await session.run(
                """MATCH (e:Entity {name: $name})
                   MERGE (e)-[r:HAS_FACT {key: $key}]->(f:Fact)
                   SET f.value = $value, f.source = $source, f.updated_at = $now,
                       r.key = $key""",
                name=entity_name, key=key, value=value, source=source, now=now,
            )

    async def add_relationship(
        self, from_name: str, to_name: str, label: str
    ) -> None:
        if not self._driver:
            return
        label_safe = label.upper().replace(" ", "_").replace("-", "_")
        now = _now()
        async with self._driver.session() as session:
            await session.run(
                f"""MATCH (a:Entity {{name: $from_name}})
                    MATCH (b:Entity {{name: $to_name}})
                    MERGE (a)-[r:RELATED_TO {{label: $label}}]->(b)
                    SET r.created_at = $now""",
                from_name=from_name, to_name=to_name, label=label_safe, now=now,
            )

    async def search_entities(self, query: str) -> list[Entity]:
        if not self._driver:
            return []
        pattern = f"(?i).*{query}.*"
        async with self._driver.session() as session:
            result = await session.run(
                """MATCH (e:Entity)
                   WHERE e.name =~ $pattern OR e.notes =~ $pattern
                   RETURN e ORDER BY e.updated_at DESC LIMIT 20""",
                pattern=pattern,
            )
            records = await result.data()
        return [_record_to_entity(r["e"]) for r in records]

    async def get_entity_with_facts(self, name: str) -> Entity | None:
        if not self._driver:
            return None
        async with self._driver.session() as session:
            result = await session.run(
                """MATCH (e:Entity {name: $name})
                   OPTIONAL MATCH (e)-[r:HAS_FACT]->(f:Fact)
                   OPTIONAL MATCH (e)-[rel:RELATED_TO]->(other:Entity)
                   RETURN e,
                          collect(DISTINCT {key: r.key, value: f.value, source: f.source}) AS facts,
                          collect(DISTINCT {label: rel.label, name: other.name}) AS rels""",
                name=name,
            )
            record = await result.single()
        if not record:
            return None
        entity = _record_to_entity(record["e"])
        entity.facts = [f for f in record["facts"] if f.get("key")]
        entity.relationships = [r for r in record["rels"] if r.get("label")]
        return entity

    async def get_all_entities_of_type(self, entity_type: str) -> list[Entity]:
        if not self._driver:
            return []
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity {type: $type}) RETURN e ORDER BY e.name",
                type=entity_type,
            )
            records = await result.data()
        return [_record_to_entity(r["e"]) for r in records]

    async def forget_entity(self, name: str) -> bool:
        if not self._driver:
            return False
        async with self._driver.session() as session:
            result = await session.run(
                """MATCH (e:Entity {name: $name})
                   OPTIONAL MATCH (e)-[:HAS_FACT]->(f:Fact)
                   DETACH DELETE e, f
                   RETURN count(e) AS deleted""",
                name=name,
            )
            record = await result.single()
        return bool(record and record["deleted"] > 0)

    async def forget_fact(self, entity_name: str, fact_key: str) -> bool:
        if not self._driver:
            return False
        async with self._driver.session() as session:
            result = await session.run(
                """MATCH (e:Entity {name: $name})-[r:HAS_FACT {key: $key}]->(f:Fact)
                   DETACH DELETE f
                   RETURN count(f) AS deleted""",
                name=entity_name, key=fact_key,
            )
            record = await result.single()
        return bool(record and record["deleted"] > 0)

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


def _record_to_entity(node: Any) -> Entity:
    return Entity(
        id=node.get("id", ""),
        name=node.get("name", ""),
        type=node.get("type", "other"),
        notes=node.get("notes", ""),
        created_at=node.get("created_at", ""),
        updated_at=node.get("updated_at", ""),
    )
