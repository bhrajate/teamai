"""向量库适配器：Qdrant（本地开发降级到内存实现）。"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from teamai.config import settings
from teamai.domain.models import MemoryEntry

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    """向量索引。

    ⚠️ **写操作（upsert / delete）失败必须抛异常，不得静默降级。** projector 靠
    异常决定是否退避重试，吞掉异常就等于丢掉那次投影 —— 而 outbox 记录会被当成
    处理成功而删除，那条记忆的向量永久缺失。

    这与读操作（query）相反：检索失败回落到时间倒序是正确的降级，因为那只影响
    单次回答的质量，不影响数据。理由与完整设计见 `docs/plan-memory-outbox.md`。
    """

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str | None:
        """写入/覆盖向量，返回可回填到 `MemoryEntry.embedding_ref` 的引用。

        返回引用而不是让调用方自己推导：point id 的映射规则是本层的实现细节，
        用例层不该知道它（delete 也刻意不依赖它，见下）。

        失败抛异常。
        """
        ...

    async def query(self, channel_instance_id: str, embedding: list[float], top_k: int) -> list[str]: ...

    async def delete(self, entry_id: str) -> None:
        """删掉某条记忆的向量。

        必须有这个方法：删除记忆时若只删 Postgres 行，向量会留在库里继续被
        检索命中，随后因取不到实体而被过滤掉 —— 不会泄露已删内容，但白占
        top_k 名额，删得多了检索质量静默下降。

        失败抛异常。
        """
        ...


# ⚠️ 这里曾有一个 `_InMemoryVectorStore` 降级实现，Qdrant 不可用时接管全部读写。
# 它已被删除，别再加回来 —— 它是本次改造要修的缺陷之一：
#
# - 向量写进进程内存，重启即蒸发；
# - 而 `upsert` 返回一个非空值，于是 `embedding_ref` 被填上，那一行**看起来
#   已建索引** —— 任何基于 `embedding_ref IS NULL` 的补齐都会跳过它；
# - web 与 worker 是两个进程，各持一份互不可见的字典。
#
# 「制造假成功」比失败更糟。现在 Qdrant 不可用时 upsert/delete 抛异常，由
# projector 退避重试，lag 指标把积压暴露出来。检索侧（query）本来就有时间倒序
# 回落，不受影响。


class QdrantVectorStore:
    """Qdrant 实现。

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

        Qdrant 不可用时**抛异常**，由 projector 退避重试。
        """
        client = await self._require_client()

        from qdrant_client.models import PointStruct

        point_id = str(self.point_id(entry.id))
        client.upload_points(
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
        """检索。与写操作不同，这里**不抛**：Qdrant 不可用时返回空，由
        `MemoryService._semantic_hits` 回落到时间倒序。

        读写两种降级取向相反是有意的：检索失败只影响单次回答的质量，写失败会丢
        数据。见类文档。
        """
        if self._client is None:
            await self._ensure_client()
        if self._client is None:
            return []
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

        Qdrant 不可用时**抛异常**，由 projector 退避重试。
        """
        client = await self._require_client()

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="entry_id", match=MatchValue(value=entry_id))]
            ),
        )

    async def _require_client(self):
        """取 client，取不到就抛。写路径专用。

        与 `_ensure_client` 的分工：那个把失败咽下去（`_client` 留 None），供读
        路径判断「要不要回落」；这个是写路径要的语义 —— 连不上就是失败，必须让
        projector 知道，否则那次投影会被当成成功而丢掉。
        """
        if self._client is None:
            await self._ensure_client()
        if self._client is None:
            raise ConnectionError(f"Qdrant 不可用（{self._url}），无法写入向量")
        return self._client

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
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.warning(f"Qdrant 连接失败（{self._url}）: {exc}")
            self._client = None


def build_vector_store(dimensions: int = 1536) -> VectorStore:
    """按 embedder 声明的维度装配。

    ⚠️ 换 embedding 模型且维度变化时，已有集合不会自动重建 —— Qdrant 的集合
    维度创建后不可改。需要手动删除集合再重启（记忆条目仍在 Postgres 里，
    但向量索引要重建），或换一个 `qdrant_collection` 名。
    """
    return QdrantVectorStore(dimensions=dimensions)
