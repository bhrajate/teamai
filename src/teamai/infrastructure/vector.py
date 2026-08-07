"""向量库适配器：Qdrant（本地开发降级到内存实现）。"""

from __future__ import annotations

import uuid
from typing import Protocol

from teamai.config import settings
from teamai.domain.memory import MemoryEntry


class VectorStore(Protocol):
    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> None: ...

    async def query(self, channel_instance_id: str, embedding: list[float], top_k: int) -> list[str]: ...


class _InMemoryVectorStore:
    """无外部依赖时的降级实现（开发/测试用）。"""

    def __init__(self) -> None:
        self._vectors: dict[str, tuple[str, list[float]]] = {}

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> None:
        self._vectors[entry.id] = (entry.channel_instance_id, embedding)

    async def query(self, channel_instance_id: str, embedding: list[float], top_k: int) -> list[str]:
        def _cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = sum(x * x for x in a) ** 0.5 or 1.0
            nb = sum(x * x for x in b) ** 0.5 or 1.0
            return dot / (na * nb)

        scored = [
            (entry_id, _cosine(embedding, vec))
            for entry_id, (ch, vec) in self._vectors.items()
            if ch == channel_instance_id
        ]
        scored.sort(key=lambda p: p[1], reverse=True)
        return [eid for eid, _ in scored[:top_k]]


class QdrantVectorStore:
    """Qdrant 实现。Qdrant 不可用时回退到内存实现。"""

    def __init__(self, url: str | None = None, collection: str | None = None) -> None:
        self._url = url or settings.qdrant_url
        self._collection = collection or settings.qdrant_collection
        self._client = None
        self._fallback = _InMemoryVectorStore()

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> None:
        if self._client is None:
            await self._ensure_client()
        if self._client is None:  # 降级
            await self._fallback.upsert(entry, embedding)
            return
        self._client.upload_points(
            collection_name=self._collection,
            points=[
                {
                    "id": uuid.uuid5(uuid.NAMESPACE_DNS, entry.id),
                    "vector": embedding,
                    "payload": {"entry_id": entry.id, "channel_instance_id": entry.channel_instance_id},
                }
            ],
        )

    async def query(self, channel_instance_id: str, embedding: list[float], top_k: int) -> list[str]:
        if self._client is None:
            await self._ensure_client()
        if self._client is None:  # 降级
            return await self._fallback.query(channel_instance_id, embedding, top_k)
        hits = self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            query_filter={
                "must": [{"key": "channel_instance_id", "match": {"value": channel_instance_id}}]
            },
            limit=top_k,
        ).points
        return [h.payload["entry_id"] for h in hits]

    async def _ensure_client(self) -> None:
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=self._url)
            collections = [c.name for c in client.get_collections().collections]
            if self._collection not in collections:
                client.create_collection(
                    collection_name=self._collection,
                    vectors_config={"size": 384, "distance": "Cosine"},
                )
            self._client = client
        except Exception:  # pragma: no cover - 外部服务不可用
            self._client = None


def build_vector_store() -> VectorStore:
    return QdrantVectorStore()
