"""Memory manager — facade over entity store (Neo4j) and vector store (ChromaDB)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from jarvis.memory.entity_store import EntityStore
from jarvis.memory.vector_store import VectorStore

if TYPE_CHECKING:
    from jarvis.llm.memory import Turn

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, config: dict[str, Any]) -> None:
        neo4j_cfg = config.get("neo4j", {})
        password = os.environ.get("NEO4J_PASSWORD") or neo4j_cfg.get("password", "jarvispass")
        self._entities = EntityStore(
            uri=neo4j_cfg.get("uri", "bolt://localhost:7687"),
            user=neo4j_cfg.get("user", "neo4j"),
            password=password,
        )
        chroma_path = config.get("memory_db", {}).get("chroma_path", ".chroma")
        self._vectors = VectorStore(chroma_path=chroma_path)

        # LLM client reference — set in Phase 3 for summarisation
        self._llm: Any | None = None

    async def init(self) -> None:
        await self._entities.init()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._vectors.init)

    def set_llm(self, llm: Any) -> None:
        self._llm = llm

    # ------------------------------------------------------------------
    # Entity operations (backed by Neo4j)
    # ------------------------------------------------------------------

    async def remember_entity(
        self,
        name: str,
        entity_type: str,
        fact_key: str,
        fact_value: str,
        notes: str = "",
    ) -> dict[str, Any]:
        entity_id = await self._entities.upsert_entity(name, entity_type, notes)
        if entity_id is None:
            return {"status": "error", "message": "Neo4j unavailable"}
        await self._entities.add_fact(name, fact_key, fact_value)
        return {"status": "remembered", "entity": name, "fact": f"{fact_key}={fact_value}"}

    async def remember_relationship(
        self, from_name: str, to_name: str, label: str
    ) -> dict[str, Any]:
        # Ensure both entities exist (as 'other' type if not already present)
        await self._entities.upsert_entity(from_name, "other")
        await self._entities.upsert_entity(to_name, "other")
        await self._entities.add_relationship(from_name, to_name, label)
        return {"status": "relationship_added", "from": from_name, "to": to_name, "label": label}

    async def recall_about(self, topic: str) -> str:
        entities = await self._entities.search_entities(topic)
        lines: list[str] = []

        for entity in entities[:5]:
            full = await self._entities.get_entity_with_facts(entity.name)
            if not full:
                continue
            lines.append(f"**{full.name}** ({full.type})" + (f" — {full.notes}" if full.notes else ""))
            for fact in full.facts:
                lines.append(f"  • {fact['key']}: {fact['value']}")
            for rel in full.relationships:
                lines.append(f"  → {rel['label']} → {rel['name']}")

        # Phase 3 will add vector search results here
        if self._vectors:
            vector_results = await self._vectors.search_all(topic, n_results=3)
            if vector_results:
                lines.append("\n**From conversation history:**")
                for r in vector_results:
                    lines.append(f"  [{r.metadata.get('session_date', 'past')}] {r.text[:200]}")

        if not lines:
            return f"I don't have any information about '{topic}' in long-term memory."
        return "\n".join(lines)

    async def list_entities(self, entity_type: str) -> str:
        entities = await self._entities.get_all_entities_of_type(entity_type)
        if not entities:
            return f"No {entity_type} entities in memory."
        lines = [f"**{e.name}**" + (f" — {e.notes}" if e.notes else "") for e in entities]
        return f"Known {entity_type}s:\n" + "\n".join(lines)

    async def forget(
        self, entity_name: str, fact_key: str | None = None
    ) -> dict[str, Any]:
        if fact_key:
            ok = await self._entities.forget_fact(entity_name, fact_key)
            return {"status": "forgotten" if ok else "not_found", "entity": entity_name, "fact": fact_key}
        ok = await self._entities.forget_entity(entity_name)
        return {"status": "forgotten" if ok else "not_found", "entity": entity_name}

    # ------------------------------------------------------------------
    # Task output storage (used by TaskRunner — Phase 3 adds vector store)
    # ------------------------------------------------------------------

    async def search_history(self, query: str) -> str:
        if not self._vectors.available:
            return "Semantic search unavailable (ChromaDB not initialised)."
        results = await self._vectors.search_all(query, n_results=5)
        if not results:
            return f"No matching history found for '{query}'."
        lines = [f"**Search results for '{query}':**"]
        for r in results:
            date = r.metadata.get("session_date") or r.metadata.get("run_at", "past")
            label = r.metadata.get("task_name", "conversation")
            lines.append(f"\n[{date} | {label}]\n{r.text[:300]}")
        return "\n".join(lines)

    async def store_task_output(self, task_name: str, output_text: str) -> None:
        if self._vectors.available:
            await self._vectors.store_task_result(output_text, task_name)

    # ------------------------------------------------------------------
    # Conversation summarisation (Phase 3)
    # ------------------------------------------------------------------

    async def summarize_and_store(self, turns: list[Any]) -> None:
        if not self._llm or not self._vectors or not turns:
            return
        from datetime import datetime, timezone
        conversation = "\n".join(
            f"{t['role'].upper()}: {t['content']}" for t in turns[-20:]
        )
        prompt = (
            "Summarise the key facts, decisions, and topics from this conversation "
            "in 3-5 bullet points. Identify any people, places, events, or preferences mentioned.\n\n"
            + conversation
        )
        try:
            from jarvis.llm.memory import ConversationMemory
            summary = await self._llm.chat_async(prompt, ConversationMemory(max_turns=0, persist_path=None))
            session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await self._vectors.store_conversation_summary(summary, session_date)
            logger.debug("Stored conversation summary (%d chars)", len(summary))
        except Exception as exc:
            logger.warning("Failed to summarise conversation: %s", exc)

    async def close(self) -> None:
        await self._entities.close()
