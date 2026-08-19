"""向量库适配器：Qdrant 调用形态 + 读写两种相反的降级取向。

⚠️ 本文件曾有一整段「内存实现」用例。`_InMemoryVectorStore` 已被删除（见
`infrastructure/vector.py` 里那段注释），因为它制造假成功：向量写进进程内存、
重启即蒸发，而 `upsert` 返回非空值使 `embedding_ref` 被填上，那一行看起来已建
索引。别再把它加回来，也别把这些用例恢复。

现在的契约是**读写降级方向相反**：
- 写（upsert/delete）失败**抛异常** —— projector 靠它决定退避重试。吞掉就等于
  丢掉那次投影，而 outbox 记录会被当成成功而删除，向量永久缺失。
- 读（query）失败**返回空** —— 由 MemoryService 回落到时间倒序。检索失败只影响
  单次回答质量，不影响数据。

Qdrant 部分不连真服务，而是用假 client 断言**传给 SDK 的参数形态**。这样做是
因为此前修的两个缺陷都恰好是「形态不对但不报错/报错被吞」：

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
from types import SimpleNamespace

import pytest

from teamai.domain.models import MemoryEntry
from teamai.infrastructure.vector import QdrantVectorStore


def _entry(eid: str = "mem_1", channel: str = "ch_1", content: str = "某事实") -> MemoryEntry:
    return MemoryEntry(id=eid, channel_instance_id=channel, content=content)


# ===== Qdrant 调用形态 =====


class FakeQdrantClient:
    """记下每次调用的参数，不做任何真实存储。"""

    def __init__(self, points: list | None = None) -> None:
        self.uploaded: list[dict] = []
        self.deleted: list[dict] = []
        self.queried: list[dict] = []
        self._points = points or []

    def upload_points(self, collection_name: str, points: list) -> None:
        self.uploaded.append({"collection": collection_name, "points": points})

    def delete(self, collection_name: str, points_selector) -> None:
        self.deleted.append({"collection": collection_name, "selector": points_selector})

    def query_points(self, collection_name: str, query, query_filter, limit: int):
        self.queried.append(
            {
                "collection": collection_name,
                "query": query,
                "filter": query_filter,
                "limit": limit,
            }
        )
        return SimpleNamespace(points=self._points)


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


async def test_query返回带相似度的元组() -> None:
    """`query` 的返回是 `(entry_id, 相似度)`，不是裸 id。

    分数是手工写入冲突检查的判据（`MemoryService.find_conflicts` 按阈值判「像到
    该拦下来」）。这条用例锁的是形态：漏了分数不会在这一层报错，会在上层表现为
    「阈值怎么调都不起作用」—— 因为解包出来的第二个值根本不是分数。
    """
    store = QdrantVectorStore(collection="test-col", dimensions=3)
    store._client = FakeQdrantClient(
        points=[
            SimpleNamespace(payload={"entry_id": "mem_1"}, score=0.93),
            SimpleNamespace(payload={"entry_id": "mem_2"}, score=0.71),
        ]
    )

    hits = await store.query("ch_1", [1.0, 0.0, 0.0], 5)

    assert hits == [("mem_1", 0.93), ("mem_2", 0.71)]


async def test_query按频道过滤且带上限() -> None:
    """频道隔离在这一层就要成立 —— 它是 Design-claude-tag.md §5 的正确性属性。"""
    store = QdrantVectorStore(collection="test-col", dimensions=3)
    client = FakeQdrantClient(points=[])
    store._client = client

    await store.query("ch_1", [1.0, 0.0, 0.0], 7)

    call = client.queried[0]
    assert call["limit"] == 7
    assert call["filter"]["must"][0]["key"] == "channel_instance_id"
    assert call["filter"]["must"][0]["match"]["value"] == "ch_1"


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


# ===== 不可用时的降级：写抛、读返空 =====


@pytest.fixture
def unavailable() -> QdrantVectorStore:
    """连不上 Qdrant 的 store。"""
    store = QdrantVectorStore(collection="test-col", dimensions=3)

    async def _fail() -> None:
        store._client = None

    store._ensure_client = _fail  # type: ignore[method-assign]
    return store


async def test_不可用时upsert抛异常(unavailable: QdrantVectorStore) -> None:
    """必须抛：projector 靠异常决定退避重试。

    改造前这里降级到进程内存字典并返回非空引用 —— 于是那一行看起来已建索引，
    而向量在重启后蒸发，且任何基于 `embedding_ref IS NULL` 的补齐都会跳过它。
    """
    with pytest.raises(ConnectionError, match="Qdrant 不可用"):
        await unavailable.upsert(_entry("mem_1"), [1.0, 0.0, 0.0])


async def test_不可用时delete抛异常(unavailable: QdrantVectorStore) -> None:
    with pytest.raises(ConnectionError, match="Qdrant 不可用"):
        await unavailable.delete("mem_1")


async def test_不可用时query返回空而不抛(unavailable: QdrantVectorStore) -> None:
    """读路径取向与写相反：检索失败只影响单次回答质量，回落到时间倒序即可。

    这条与上面两条一起，锁住「读写降级方向相反」这个刻意的不对称 —— 少了它，
    后来者很可能为了「一致」把 query 也改成抛，那会让向量库故障直接变成回答失败。
    """
    assert await unavailable.query("ch_1", [1.0, 0.0, 0.0], 5) == []
