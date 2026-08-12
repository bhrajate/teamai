"""MemoryService 测试。

锁住改造前的两个故障级缺陷：

1. **向量路径是死代码。** `MemoryService` 收 `embedder` 参数，但组合根从未注入过
   （只传了 `vector_store`），于是 `_embedder` 恒为 None、Qdrant 从未被写入、
   语义检索分支永不进入。缺陷被「静默降级」的写法藏住了，故现在改成由
   `Embedder.available` 显式表态，并在此断言两条路径都真的会走。

2. **检索回落是无界全表扫描。** 回落路径调 `list_by_channel` 时既无 ORDER BY 也无
   LIMIT，调用方却在 Python 侧切前 5 条 —— 行序由数据库决定，等于随机取样，
   且随频道使用时长线性变慢。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from teamai.application.memory import MemoryService
from teamai.domain.models import AuditAction, MemoryEntry, MemoryType, Visibility
from teamai.domain.ports import Embedder
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeChannelRepository, FakeMemoryRepository


class StubEmbedder(Embedder):
    """可控的 embedder。`available` 与返回值都能按测试需要摆布。"""

    def __init__(self, *, available: bool = True, vector: list[float] | None = None) -> None:
        self._available = available
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[str] = []

    @property
    def dimensions(self) -> int:
        return 3

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self._vector)


class StubVectorStore:
    def __init__(self, hits: list[str] | None = None, *, fail: bool = False) -> None:
        self.upserted: list[str] = []
        self.queries: list[tuple[str, int]] = []
        self._hits = hits or []
        self._fail = fail

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> None:
        if self._fail:
            raise ConnectionError("Qdrant 挂了")
        self.upserted.append(entry.id)

    async def query(
        self, channel_instance_id: str, embedding: list[float], top_k: int
    ) -> list[str]:
        if self._fail:
            raise ConnectionError("Qdrant 挂了")
        self.queries.append((channel_instance_id, top_k))
        return list(self._hits)


def _service(
    repo: FakeMemoryRepository | None = None,
    *,
    vector=None,
    embedder: Embedder | None = None,
) -> tuple[MemoryService, FakeMemoryRepository, FakeAuditRepository]:
    memory_repo = repo or FakeMemoryRepository()
    audit_repo = FakeAuditRepository()
    service = MemoryService(
        memory_repo,
        FakeChannelRepository(),
        AuditLogWriter(audit_repo),
        vector_store=vector,
        embedder=embedder,
    )
    return service, memory_repo, audit_repo


async def _seed(repo: FakeMemoryRepository, count: int, channel: str = "ch_1") -> None:
    """按时间递增造若干条记忆，最后一条最新。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(count):
        await repo.store(
            MemoryEntry(
                id=f"mem_{i}",
                channel_instance_id=channel,
                content=f"事实 {i}",
                created_at=base + timedelta(minutes=i),
            )
        )


# ===== 写入 =====


async def test_存入并留审计() -> None:
    service, repo, audit_repo = _service()

    entry = await service.store("ch_1", "超时是 30 秒", source_user_id="U1")

    assert [e.content for e in repo.stored] == ["超时是 30 秒"]
    assert entry.type is MemoryType.BACKGROUND_KNOWLEDGE
    assert entry.visibility is Visibility.CHANNEL
    (log,) = audit_repo.logs
    assert log.action is AuditAction.MEMORY_STORE
    assert log.detail["entry_id"] == entry.id


async def test_审计动作可覆盖为蒸馏() -> None:
    service, _, audit_repo = _service()

    await service.store("ch_1", "某结论", action=AuditAction.MEMORY_DISTILL)

    assert audit_repo.logs[0].action is AuditAction.MEMORY_DISTILL


async def test_装了embedder才写向量() -> None:
    """回归点：改造前 embedder 从未注入，Qdrant 一次都没被写过。"""
    vector = StubVectorStore()
    embedder = StubEmbedder()
    service, _, _ = _service(vector=vector, embedder=embedder)

    entry = await service.store("ch_1", "值得记的事实")

    assert vector.upserted == [entry.id]
    assert embedder.calls == ["值得记的事实"]


async def test_embedder不可用时跳过向量写入() -> None:
    """未配 embedding 凭据时装的是 NullEmbedder，不该白发一次请求。"""
    vector = StubVectorStore()
    embedder = StubEmbedder(available=False)
    service, repo, _ = _service(vector=vector, embedder=embedder)

    await service.store("ch_1", "某事实")

    assert vector.upserted == []
    assert embedder.calls == []
    assert len(repo.stored) == 1, "记忆本身仍要落库"


async def test_向量写入失败不影响记忆落库() -> None:
    """反过来（为向量失败而丢弃记忆）损失更大：检索仍能经时间倒序拿到它。"""
    service, repo, _ = _service(vector=StubVectorStore(fail=True), embedder=StubEmbedder())

    await service.store("ch_1", "某事实")

    assert len(repo.stored) == 1


# ===== 检索 =====


async def test_语义检索命中优先() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 5)
    vector = StubVectorStore(hits=["mem_2"])
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())

    hits = await service.query_for_context("ch_1", "问个问题", top_k=3)

    assert [h.id for h in hits] == ["mem_2"]
    assert vector.queries == [("ch_1", 3)]


async def test_无embedder时回落到时间倒序且有界() -> None:
    """回归点：此前这里是无 ORDER BY / 无 LIMIT 的全表查询，等于随机取样。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 30)
    service, _, _ = _service(repo)

    hits = await service.query_for_context("ch_1", "问个问题", top_k=3)

    assert len(hits) == 3, "必须受 top_k 约束"
    assert [h.content for h in hits] == ["事实 29", "事实 28", "事实 27"], "应取最新的"


async def test_向量库异常时降级到时间倒序() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 5)
    service, _, _ = _service(repo, vector=StubVectorStore(fail=True), embedder=StubEmbedder())

    hits = await service.query_for_context("ch_1", "问个问题", top_k=2)

    assert [h.content for h in hits] == ["事实 4", "事实 3"]


async def test_偏好不受top_k裁剪全部带上() -> None:
    """偏好是「怎么回答」的约束（语气、格式、禁忌），与当前问题的语义相关度无关。

    按相似度筛会让偏好在问到无关话题时失效。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 10)
    service, _, _ = _service(repo)
    await service.set_preference("ch_1", "U1", "回答要简短")
    await service.set_preference("ch_1", "U2", "别用 emoji")

    hits = await service.query_for_context("ch_1", "问个问题", top_k=2)

    prefs = [h for h in hits if h.type is MemoryType.PREFERENCE]
    assert len(prefs) == 2
    assert len(hits) == 4, "2 条记忆 + 2 条偏好"
    assert "回答要简短" in prefs[0].content


async def test_频道隔离() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 3, channel="ch_A")
    await _seed(repo, 2, channel="ch_B")
    service, _, _ = _service(repo)

    hits = await service.query_for_context("ch_B", "问个问题", top_k=10)

    assert all(h.channel_instance_id == "ch_B" for h in hits)


# ===== 列出与删除 =====


async def test_list默认有上界() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 300)
    service, _, _ = _service(repo)

    assert len(await service.list("ch_1")) == 200
    assert len(await service.list("ch_1", limit=5)) == 5


async def test_删除留审计() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 2)
    service, _, audit_repo = _service(repo)

    await service.delete("mem_0", actor="admin")

    assert [e.id for e in repo.stored] == ["mem_1"]
    assert audit_repo.logs[-1].action is AuditAction.MEMORY_DELETE


async def test_删除不存在的条目静默返回() -> None:
    service, _, audit_repo = _service()

    await service.delete("mem_missing")

    assert audit_repo.logs == []
