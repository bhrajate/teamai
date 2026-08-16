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

import pytest

from teamai.application.memory import MemoryService
from teamai.domain.models import (
    AuditAction,
    MemoryEntry,
    MemorySource,
    MemoryType,
)
from teamai.domain.ports import Embedder
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeChannelRepository, FakeMemoryRepository


class StubEmbedder(Embedder):
    """可控的 embedder。`available` / 返回值 / 是否抛错都能按测试需要摆布。"""

    def __init__(self, *, available: bool = True, vector: list[float] | None = None) -> None:
        self._available = available
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[str] = []
        # 置真后 embed 抛错，用于验「重算失败时旧向量被删掉」
        self.broken = False

    @property
    def dimensions(self) -> int:
        return 3

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.broken:
            raise ConnectionError("embedding 服务挂了")
        return list(self._vector)


class StubVectorStore:
    def __init__(self, hits: list[str] | None = None, *, fail: bool = False) -> None:
        self.upserted: list[str] = []
        self.deleted: list[str] = []
        self.queries: list[tuple[str, int]] = []
        # 记下每次 upsert 的内容，用于断言编辑后向量真的按新内容重算过
        self.upserted_content: list[str] = []
        self._hits = hits or []
        self._fail = fail

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str | None:
        if self._fail:
            raise ConnectionError("Qdrant 挂了")
        self.upserted.append(entry.id)
        self.upserted_content.append(entry.content)
        return f"point-{entry.id}"

    async def delete(self, entry_id: str) -> None:
        if self._fail:
            raise ConnectionError("Qdrant 挂了")
        self.deleted.append(entry_id)

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
    assert entry.is_current, "新写入的记忆必须是现行事实"
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


async def test_list默认排除已被取代的条目() -> None:
    """喂给模型的上下文绝不能含被取代的事实。"""
    repo = FakeMemoryRepository()
    await repo.store(MemoryEntry(id="mem_old", channel_instance_id="ch_1", content="旧值"))
    await repo.store(MemoryEntry(id="mem_new", channel_instance_id="ch_1", content="新值"))
    old = await repo.get("mem_old")
    old.supersede("mem_new")
    await repo.update(old)
    service, _, _ = _service(repo)

    assert [e.content for e in await service.list("ch_1")] == ["新值"]
    # 控制台排查历史时显式要全部
    both = await service.list("ch_1", current_only=False)
    assert sorted(e.content for e in both) == ["新值", "旧值"]


async def test_supersede写新条目并标记旧条目() -> None:
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(id="mem_old", channel_instance_id="ch_1", content="超时 3 秒")
    )
    service, _, audit_repo = _service(repo)

    new_entry = await service.supersede("mem_old", "ch_1", "超时 5 秒")

    assert new_entry is not None and new_entry.content == "超时 5 秒"
    old = await repo.get("mem_old")
    assert old.superseded_by == new_entry.id
    assert old.superseded_at is not None
    assert not old.is_current
    # 旧条目仍在库里，不是被删掉
    assert old.content == "超时 3 秒"
    detail = audit_repo.logs[-1].detail
    assert detail["action"] == "supersede"
    assert detail["old_entry_id"] == "mem_old"


async def test_supersede拒绝跨频道取代() -> None:
    """否则 A 频道的蒸馏结果能改写 B 频道的记忆 —— 频道隔离是正确性属性。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(id="mem_b", channel_instance_id="ch_B", content="B 频道的事实")
    )
    service, _, _ = _service(repo)

    result = await service.supersede("mem_b", "ch_A", "A 频道想改的内容")

    assert result is None
    untouched = await repo.get("mem_b")
    assert untouched.is_current, "B 频道的记忆不该被动过"


async def test_supersede对不存在的条目返回None() -> None:
    service, repo, _ = _service()

    assert await service.supersede("mem_missing", "ch_1", "内容") is None
    assert repo.stored == [], "旧条目不存在时不该留下半个新条目"


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


# ===== 删除时清向量 =====


async def test_删除同时清向量索引() -> None:
    """回归点：改造前只删 Postgres 行，向量留在库里继续被检索命中，
    随后因取不到实体被过滤 —— 不泄露内容，但白占 top_k 名额。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 3)
    vector = StubVectorStore()
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())

    await service.delete("mem_1")

    assert vector.deleted == ["mem_1"]


async def test_向量库不可用时删除仍成功() -> None:
    """Postgres 行是权威源，它已经删了。为向量库故障而报错会让用户以为没删掉。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 2)
    service, _, audit_repo = _service(
        repo, vector=StubVectorStore(fail=True), embedder=StubEmbedder()
    )

    await service.delete("mem_0")  # 不该抛

    assert [e.id for e in repo.stored] == ["mem_1"]
    assert audit_repo.logs[-1].action is AuditAction.MEMORY_DELETE


# ===== embedding_ref 回填 =====


async def test_建索引后回填embedding_ref() -> None:
    """回归点：这个字段此前声明了、mapper 两侧也在传，但没有任何代码写入它 ——
    于是清理脚本里 `embedding_ref IS NULL` 恒为真。"""
    service, repo, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder())

    entry = await service.store("ch_1", "某事实")

    assert entry.embedding_ref == f"point-{entry.id}"
    assert repo.stored[0].embedding_ref == f"point-{entry.id}"


async def test_没有向量库时embedding_ref为空() -> None:
    service, repo, _ = _service()

    entry = await service.store("ch_1", "某事实")

    assert entry.embedding_ref is None
    assert repo.stored[0].embedding_ref is None


# ===== 编辑 =====


async def test_编辑内容并留审计() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 2)
    service, _, audit_repo = _service(repo)

    edited = await service.edit("mem_0", content="改过的内容", actor="admin")

    assert edited is not None and edited.content == "改过的内容"
    assert (await repo.get("mem_0")).content == "改过的内容"
    log = audit_repo.logs[-1]
    assert log.action is AuditAction.MEMORY_EDIT
    assert log.user_id == "admin"
    assert log.detail["content_changed"] is True


async def test_编辑保留id与创建时间() -> None:
    """与「删一条 + 建一条」的关键区别：id 与 created_at 不变，审计里看得出
    是同一条的演进。

    edit 是「这条写错了」的路径，改完仍是同一条事实 —— 与 supersede（「事实
    变了」，新写一条并把旧的标记为已取代）是两回事。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(
            id="mem_x",
            channel_instance_id="ch_1",
            content="原内容",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    service, _, _ = _service(repo)

    edited = await service.edit("mem_x", content="新内容", type=MemoryType.DECISION)

    assert edited is not None
    assert edited.id == "mem_x"
    assert edited.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert edited.type is MemoryType.DECISION
    assert edited.is_current, "编辑不该把条目标记为已取代"


async def test_编辑不存在的条目返回None() -> None:
    service, _, audit_repo = _service()

    assert await service.edit("mem_missing", content="x") is None
    assert audit_repo.logs == []


async def test_审计只留内容摘要不留全文() -> None:
    """审计表不该变成内容的第二份副本。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(id="mem_l", channel_instance_id="ch_1", content="长" * 300)
    )
    service, _, audit_repo = _service(repo)

    await service.edit("mem_l", content="短的")

    assert len(audit_repo.logs[-1].detail["old_content"]) == 50


async def test_改内容触发向量重算() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())

    await service.edit("mem_0", content="全新的内容")

    assert vector.upserted_content[-1] == "全新的内容", "必须按新内容重算"


async def test_只改类型不重算向量() -> None:
    """向量对应的是文本，类型变了不影响它 —— 白跑一次 embedding 是纯浪费。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())
    before = len(vector.upserted)

    await service.edit("mem_0", type=MemoryType.FACT)

    assert len(vector.upserted) == before


async def test_内容未实际变化时不重算() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())
    before = len(vector.upserted)

    await service.edit("mem_0", content="事实 0")  # 与原内容相同

    assert len(vector.upserted) == before


async def test_重算失败时删掉旧向量而非保留() -> None:
    """本次最要紧的一条：留着旧向量比没有索引更糟 ——
    检索会持续按已被改掉的内容命中它。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    embedder = StubEmbedder()
    service, _, _ = _service(repo, vector=vector, embedder=embedder)
    # 先建好索引，再让 embedding 坏掉
    await service.edit("mem_0", content="第一次改")
    assert vector.upserted_content[-1] == "第一次改"
    embedder.broken = True

    edited = await service.edit("mem_0", content="第二次改")

    assert edited is not None and edited.embedding_ref is None
    assert vector.deleted == ["mem_0"], "旧向量必须被删掉"


async def test_embedder不可用时编辑也清掉旧向量() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder(available=False))

    await service.edit("mem_0", content="改了")

    assert vector.deleted == ["mem_0"]


# ===== source 字段 =====


async def test_默认来源为人工写入() -> None:
    service, _, _ = _service()

    entry = await service.store("ch_1", "管理台写的")

    assert entry.source is MemorySource.MANUAL


async def test_蒸馏来源可显式指定() -> None:
    service, _, audit_repo = _service()

    entry = await service.store("ch_1", "蒸馏出的", source=MemorySource.DISTILLED)

    assert entry.source is MemorySource.DISTILLED
    assert audit_repo.logs[0].detail["source"] == "DISTILLED"


async def test_编辑蒸馏产出后来源变为EDITED() -> None:
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(
            id="mem_d",
            channel_instance_id="ch_1",
            content="模型提取的",
            source=MemorySource.DISTILLED,
        )
    )
    service, _, _ = _service(repo)

    edited = await service.edit("mem_d", content="人改过的")

    assert edited is not None and edited.source is MemorySource.EDITED


@pytest.mark.parametrize("original", [MemorySource.MANUAL, MemorySource.EDITED])
async def test_编辑人工写入不改变来源(original: MemorySource) -> None:
    """人改人写的东西仍然是人写的；改第二次也不必再变。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(id="mem_m", channel_instance_id="ch_1", content="原", source=original)
    )
    service, _, _ = _service(repo)

    edited = await service.edit("mem_m", content="新")

    assert edited is not None and edited.source is original
