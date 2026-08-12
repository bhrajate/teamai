"""向量库适配器：内存实现的语义 + Qdrant 调用形态。

Qdrant 部分不连真服务，而是用假 client 断言**传给 SDK 的参数形态**。这样做是
因为本次修的两个缺陷都恰好是「形态不对但不报错/报错被吞」：

1. `upload_points` 的 points 必须是 `PointStruct` 而非 dict。改造前传的是 dict，
   SDK 会抛 `AttributeError: 'dict' object has no attribute 'id'`，而调用方
   （MemoryService._embed_if_available）把异常吞成一条 warning —— 于是向量写入
   **从未成功过**，且没有任何报错。这个缺陷此前被「embedder 从未注入、这段代码
   根本没被执行」掩盖着。
2. `delete` 的 `points_selector` **只接受模型对象**，传 dict 会抛
   `ValueError: Unsupported points selector type`。注意这与同一个 client 的
   `query_points(query_filter=...)` 不同 —— 那个接受 dict。两处形态不一致是
   SDK 的既有行为，都已对真 Qdrant 验证过。
"""

from __future__ import annotations

import uuid

import pytest

from teamai.domain.models import MemoryEntry
from teamai.infrastructure.vector import QdrantVectorStore, _InMemoryVectorStore


def _entry(eid: str = "mem_1", channel: str = "ch_1", content: str = "某事实") -> MemoryEntry:
    return MemoryEntry(id=eid, channel_instance_id=channel, content=content)


# ===== 内存实现（也是 Qdrant 不可用时的降级路径）=====


async def test_内存实现写入后可检索() -> None:
    store = _InMemoryVectorStore()

    ref = await store.upsert(_entry("mem_1"), [1.0, 0.0, 0.0])

    assert ref == "mem_1", "须返回可回填 embedding_ref 的引用"
    assert await store.query("ch_1", [1.0, 0.0, 0.0], 5) == ["mem_1"]


async def test_内存实现删除后不再命中() -> None:
    store = _InMemoryVectorStore()
    await store.upsert(_entry("mem_1"), [1.0, 0.0, 0.0])
    await store.upsert(_entry("mem_2"), [0.0, 1.0, 0.0])

    await store.delete("mem_1")

    assert await store.query("ch_1", [1.0, 0.0, 0.0], 5) == ["mem_2"]


async def test_内存实现删除不存在的id不报错() -> None:
    await _InMemoryVectorStore().delete("mem_missing")


async def test_内存实现按频道隔离() -> None:
    store = _InMemoryVectorStore()
    await store.upsert(_entry("mem_a", channel="ch_A"), [1.0, 0.0, 0.0])
    await store.upsert(_entry("mem_b", channel="ch_B"), [1.0, 0.0, 0.0])

    assert await store.query("ch_B", [1.0, 0.0, 0.0], 5) == ["mem_b"]


async def test_同id重复写入是覆盖而非堆积() -> None:
    store = _InMemoryVectorStore()
    await store.upsert(_entry("mem_1"), [1.0, 0.0, 0.0])
    await store.upsert(_entry("mem_1"), [0.0, 1.0, 0.0])

    assert await store.query("ch_1", [0.0, 1.0, 0.0], 5) == ["mem_1"]
    assert len(await store.query("ch_1", [1.0, 1.0, 0.0], 5)) == 1


# ===== Qdrant 调用形态 =====


class FakeQdrantClient:
    """记下每次调用的参数，不做任何真实存储。"""

    def __init__(self) -> None:
        self.uploaded: list[dict] = []
        self.deleted: list[dict] = []

    def upload_points(self, collection_name: str, points: list) -> None:
        self.uploaded.append({"collection": collection_name, "points": points})

    def delete(self, collection_name: str, points_selector) -> None:
        self.deleted.append({"collection": collection_name, "selector": points_selector})


@pytest.fixture
def qdrant() -> tuple[QdrantVectorStore, FakeQdrantClient]:
    store = QdrantVectorStore(collection="test-col", dimensions=3)
    client = FakeQdrantClient()
    # 直接注入，跳过 _ensure_client（那条路要连真服务）
    store._client = client
    return store, client


def test_point_id映射是确定的() -> None:
    """同一 entry_id 恒得同一 point，故重复 upsert 是覆盖而非堆积。"""
    assert QdrantVectorStore.point_id("mem_1") == QdrantVectorStore.point_id("mem_1")
    assert QdrantVectorStore.point_id("mem_1") != QdrantVectorStore.point_id("mem_2")
    assert QdrantVectorStore.point_id("mem_1") == uuid.uuid5(uuid.NAMESPACE_DNS, "mem_1")


async def test_upsert传的是PointStruct而非dict(qdrant) -> None:
    """回归点：传 dict 会让 SDK 抛 AttributeError，而调用方把它吞成 warning ——
    向量写入静默失败。"""
    from qdrant_client.models import PointStruct

    store, client = qdrant

    ref = await store.upsert(_entry("mem_1"), [1.0, 2.0, 3.0])

    (point,) = client.uploaded[0]["points"]
    assert isinstance(point, PointStruct), f"必须是 PointStruct，实际是 {type(point).__name__}"
    assert point.id == str(QdrantVectorStore.point_id("mem_1"))
    assert point.vector == [1.0, 2.0, 3.0]
    assert point.payload == {"entry_id": "mem_1", "channel_instance_id": "ch_1"}
    assert ref == point.id, "返回值须是 point id，供回填 embedding_ref"


async def test_delete传的是Filter模型而非dict(qdrant) -> None:
    """回归点：points_selector 传 dict 会抛 ValueError（与 query_filter 不同）。"""
    from qdrant_client.models import Filter

    store, client = qdrant

    await store.delete("mem_1")

    selector = client.deleted[0]["selector"]
    assert isinstance(selector, Filter), f"必须是 Filter，实际是 {type(selector).__name__}"
    assert client.deleted[0]["collection"] == "test-col"


async def test_delete按entry_id过滤而非按point_id(qdrant) -> None:
    """按 payload 过滤不依赖 id 推导 —— 哪天改了映射方案却漏改一边，
    按 point id 删会静默变成 no-op，恰好是本次要修的那类缺陷。"""
    store, client = qdrant

    await store.delete("mem_1")

    condition = client.deleted[0]["selector"].must[0]
    assert condition.key == "entry_id"
    assert condition.match.value == "mem_1"


async def test_client不可用时降级到内存实现() -> None:
    """QdrantVectorStore 在连不上时不该让调用方感知，写入/删除都走内存兜底。"""
    store = QdrantVectorStore(collection="test-col", dimensions=3)

    async def _fail() -> None:
        store._client = None

    store._ensure_client = _fail  # type: ignore[method-assign]

    ref = await store.upsert(_entry("mem_1"), [1.0, 0.0, 0.0])
    assert ref == "mem_1"
    assert await store.query("ch_1", [1.0, 0.0, 0.0], 5) == ["mem_1"]

    await store.delete("mem_1")
    assert await store.query("ch_1", [1.0, 0.0, 0.0], 5) == []
