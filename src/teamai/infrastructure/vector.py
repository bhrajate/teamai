"""向量库适配器：Qdrant（本地开发降级到内存实现）。"""

from __future__ import annotations

import uuid
from typing import Protocol

from teamai.config import settings
from teamai.domain.models import MemoryEntry


class VectorStore(Protocol):
    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str | None:
        """写入/覆盖向量，返回可回填到 `MemoryEntry.embedding_ref` 的引用。

        返回引用而不是让调用方自己推导：point id 的映射规则是本层的实现细节，
        用例层不该知道它（delete 也刻意不依赖它，见下）。降级到内存实现时同样
        返回一个非空值，因为「已建索引」这个语义仍然成立。
        """
        ...

    async def query(self, channel_instance_id: str, embedding: list[float], top_k: int) -> list[str]: ...

    async def delete(self, entry_id: str) -> None:
        """删掉某条记忆的向量。

        必须有这个方法：删除记忆时若只删 Postgres 行，向量会留在库里继续被
        检索命中，随后因取不到实体而被过滤掉 —— 不会泄露已删内容，但白占
        top_k 名额，删得多了检索质量静默下降。
        """
        ...


class _InMemoryVectorStore:
    """无外部依赖时的降级实现（开发/测试用）。"""

    def __init__(self) -> None:
        self._vectors: dict[str, tuple[str, list[float]]] = {}

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str | None:
        self._vectors[entry.id] = (entry.channel_instance_id, embedding)
        return entry.id

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

    async def delete(self, entry_id: str) -> None:
        self._vectors.pop(entry_id, None)


class QdrantVectorStore:
    """Qdrant 实现。Qdrant 不可用时回退到内存实现。

    `dimensions` 必须与实际 embedder 的输出维度一致。此前这里硬编码 384，
    而常用 embedding 模型是 1536（text-embedding-3-small）或 1024 —— 一旦真接上
    embedder，建集合就会与向量维度不匹配而在写入时报错。这个 bug 长期被
    「embedder 从未注入、向量路径从未运行」掩盖着，故装配时由 Embedder 声明维度。
    """

    def __init__(
        self,
        url: str | None = None,
        collection: str | None = None,
        dimensions: int = 1536,
    ) -> None:
        self._url = url or settings.qdrant_url
        self._collection = collection or settings.qdrant_collection
        self._dimensions = dimensions
        self._client = None
        self._fallback = _InMemoryVectorStore()

    @staticmethod
    def point_id(entry_id: str) -> uuid.UUID:
        """记忆 id → Qdrant point id。

        Qdrant 的 point id 只接受整数或 UUID，而我们的 id 是 `mem_<ULID>`，
        故用 uuid5 做确定性映射（同一 entry_id 恒得同一 point，重复 upsert
        即覆盖而非堆积）。
        """
        return uuid.uuid5(uuid.NAMESPACE_DNS, entry_id)

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str | None:
        """写入/覆盖一条记忆的向量。返回 point id 供调用方回填 embedding_ref。

        ⚠️ points 必须传 `PointStruct` 而不是 dict：qdrant-client 的
        `upload_points` 会直接取 `record.id`，传 dict 会抛
        `AttributeError: 'dict' object has no attribute 'id'`。改造前这里传的
        正是 dict，而调用方（MemoryService._embed_if_available）把异常吞成一条
        warning —— 于是向量写入**从未成功过**，且不报错。这个缺陷此前被
        「embedder 从未注入、这段代码根本没被执行」掩盖着。
        """
        if self._client is None:
            await self._ensure_client()
        if self._client is None:  # 降级
            return await self._fallback.upsert(entry, embedding)

        from qdrant_client.models import PointStruct

        point_id = str(self.point_id(entry.id))
        self._client.upload_points(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "entry_id": entry.id,
                        "channel_instance_id": entry.channel_instance_id,
                    },
                )
            ],
        )
        return point_id

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

    async def delete(self, entry_id: str) -> None:
        """按 payload 里的 entry_id 过滤删除，而非按 point_id 直接删。

        两种都可行，选过滤是因为它不依赖 id 推导：按 point id 删要求这里与
        upsert 侧的推导逐字一致，哪天改了映射方案却漏改一边，删除会静默变成
        no-op —— 恰好是本方法要修的那类缺陷（删了行但向量还在）。单点删除在
        这个量级上，过滤慢一点无所谓。

        ⚠️ `points_selector` 只接受模型对象，传 dict 会抛
        `ValueError: Unsupported points selector type`。这与同一个 client 的
        `query_points(query_filter=...)` 不同 —— 那个接受 dict（现有 query 方法
        就是这么写的且能用）。两处形态不一致是 SDK 的既有行为，不是这里的疏漏。
        """
        if self._client is None:
            await self._ensure_client()
        if self._client is None:  # 降级
            await self._fallback.delete(entry_id)
            return

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="entry_id", match=MatchValue(value=entry_id))]
            ),
        )

    async def _ensure_client(self) -> None:
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=self._url)
            collections = [c.name for c in client.get_collections().collections]
            if self._collection not in collections:
                client.create_collection(
                    collection_name=self._collection,
                    vectors_config={"size": self._dimensions, "distance": "Cosine"},
                )
            self._client = client
        except Exception:  # pragma: no cover - 外部服务不可用
            self._client = None


def build_vector_store(dimensions: int = 1536) -> VectorStore:
    """按 embedder 声明的维度装配。

    ⚠️ 换 embedding 模型且维度变化时，已有集合不会自动重建 —— Qdrant 的集合
    维度创建后不可改。需要手动删除集合再重启（记忆条目仍在 Postgres 里，
    但向量索引要重建），或换一个 `qdrant_collection` 名。
    """
    return QdrantVectorStore(dimensions=dimensions)
