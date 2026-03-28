"""ChromaDB-backed vector store for semantic search over conversation history and task results."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

COLLECTION_CONVERSATIONS = "conversation_summaries"
COLLECTION_TASKS = "task_results"


@dataclass
class SearchResult:
    text: str
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float = 0.0


class VectorStore:
    def __init__(self, chroma_path: str = ".chroma") -> None:
        self._chroma_path = chroma_path
        self._client: Any = None
        self._conv_collection: Any = None
        self._task_collection: Any = None

    def init(self) -> None:
        """Synchronous init — call once at startup (ChromaDB is sync)."""
        try:
            import chromadb
            self._client = chromadb.PersistentClient(path=self._chroma_path)
            self._conv_collection = self._client.get_or_create_collection(
                COLLECTION_CONVERSATIONS,
                metadata={"hnsw:space": "cosine"},
            )
            self._task_collection = self._client.get_or_create_collection(
                COLLECTION_TASKS,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "ChromaDB ready at '%s' — conversations: %d, tasks: %d",
                self._chroma_path,
                self._conv_collection.count(),
                self._task_collection.count(),
            )
        except Exception as exc:
            logger.error("ChromaDB init failed: %s — semantic search disabled", exc)
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous ChromaDB call in the default executor."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def store_conversation_summary(
        self,
        text: str,
        session_date: str,
        entity_ids: list[str] | None = None,
    ) -> str:
        if not self._client:
            return ""
        doc_id = f"conv_{session_date}_{int(datetime.now(timezone.utc).timestamp())}"
        metadata = {"session_date": session_date, "entity_ids": ",".join(entity_ids or [])}
        await self._run(
            self._conv_collection.upsert,
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
        )
        return doc_id

    async def store_task_result(
        self,
        text: str,
        task_name: str,
        entity_ids: list[str] | None = None,
    ) -> str:
        if not self._client:
            return ""
        run_at = datetime.now(timezone.utc).isoformat()
        doc_id = f"task_{task_name}_{int(datetime.now(timezone.utc).timestamp())}"
        metadata = {
            "task_name": task_name,
            "run_at": run_at,
            "entity_ids": ",".join(entity_ids or []),
        }
        await self._run(
            self._task_collection.upsert,
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
        )
        return doc_id

    async def search(
        self, query: str, collection: str = COLLECTION_CONVERSATIONS, n_results: int = 5
    ) -> list[SearchResult]:
        if not self._client:
            return []
        col = self._conv_collection if collection == COLLECTION_CONVERSATIONS else self._task_collection
        try:
            results = await self._run(
                col.query,
                query_texts=[query],
                n_results=min(n_results, col.count() or 1),
            )
            out = []
            for i, doc in enumerate(results["documents"][0]):
                out.append(SearchResult(
                    text=doc,
                    collection=collection,
                    metadata=results["metadatas"][0][i] if results.get("metadatas") else {},
                    distance=results["distances"][0][i] if results.get("distances") else 0.0,
                ))
            return out
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return []

    async def search_all(self, query: str, n_results: int = 5) -> list[SearchResult]:
        """Search both collections and return merged results sorted by relevance."""
        if not self._client:
            return []
        conv_results, task_results = await asyncio.gather(
            self.search(query, COLLECTION_CONVERSATIONS, n_results),
            self.search(query, COLLECTION_TASKS, n_results),
        )
        merged = conv_results + task_results
        merged.sort(key=lambda r: r.distance)
        return merged[:n_results]
