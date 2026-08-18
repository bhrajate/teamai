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
    OutboxOp,
)
from teamai.domain.ports import Embedder
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.uow import NullUnitOfWork
from tests.fakes import (
    FakeAuditRepository,
    FakeChannelRepository,
    FakeMemoryRepository,
    FakeOutboxRepository,
)


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
    outbox: FakeOutboxRepository | None = None,
) -> tuple[MemoryService, FakeMemoryRepository, FakeAuditRepository]:
    """装一个 MemoryService。

    `NullUnitOfWork`：这些用例验的是业务逻辑，事务边界本身由
    tests/unit/test_uow.py 打真 SQL 锁住。用 Null 实现而非 None，是因为服务层
    无条件 `async with self._uow` —— 见 infrastructure/uow.py 里 NullUnitOfWork
    的说明。

    outbox 默认新建一个，需要断言入队内容的用例显式传入自己那份。
    """
    memory_repo = repo or FakeMemoryRepository()
    audit_repo = FakeAuditRepository()
    service = MemoryService(
        memory_repo,
        FakeChannelRepository(),
        AuditLogWriter(audit_repo),
        outbox or FakeOutboxRepository(),
        NullUnitOfWork(),
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


async def test_写入只入队不碰向量() -> None:
    """写路径把向量交给 outbox，自己不调 embedder、不写向量库。

    这条替换了改造前的「装了 embedder 才写向量」。那个用例守的是「组合根真的
    注入了 embedder」这个回归点，现在由 projector 侧的用例守（写路径压根不该
    有 embedder 参与），而这里守新契约:意图落库、远程调用推迟。

    负向断言不可省 —— 少了它，写路径哪天又直接写起向量来，测试仍会绿。
    """
    vector = StubVectorStore()
    embedder = StubEmbedder()
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=vector, embedder=embedder, outbox=outbox)

    entry = await service.store("ch_1", "值得记的事实")

    assert outbox.enqueued == [(entry.id, OutboxOp.UPSERT)]
    assert vector.upserted == [], "写路径不该直接写向量"
    assert embedder.calls == [], "写路径不该调 embedding API"


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

    按相似度筛会让偏好在问到无关话题时失效。本用例同时是回落路径 `exclude_type`
    的唯一验证（无向量/embedder 时走时间倒序回落）：语义段取最新 2 条**非偏好**
    事实（偏好不混进 top_k），偏好段全量带 2 条。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 10)
    service, _, _ = _service(repo)
    await service.store(
        "ch_1", "回答要简短", source_user_id="U1", type=MemoryType.PREFERENCE
    )
    await service.store(
        "ch_1", "别用 emoji", source_user_id="U2", type=MemoryType.PREFERENCE
    )

    hits = await service.query_for_context("ch_1", "问个问题", top_k=2)

    prefs = [h for h in hits if h.type is MemoryType.PREFERENCE]
    assert len(prefs) == 2
    assert len(hits) == 4, "2 条记忆 + 2 条偏好"
    assert "回答要简短" in " ".join(p.content for p in prefs)
    assert "偏好(U1): " in " ".join(p.content for p in prefs), "偏好带「谁设的」前缀"
    semantic = [h for h in hits if h.type is not MemoryType.PREFERENCE]
    assert [h.content for h in semantic] == ["事实 9", "事实 8"], "偏好不占用 top_k 名额"


async def test_偏好连队都不入() -> None:
    """偏好是「无条件全带」的固定上下文，不该参与 top_k 竞争。

    偏好不入队而非「入队后由 projector 判掉」:入了也只会让 projector 回读一次、
    判「不该有向量」、再删一次，白跑一轮。写入侧已经知道答案就别推给下游。
    """
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)

    pref = await service.store("ch_1", "回答要简短", type=MemoryType.PREFERENCE)
    fact = await service.store("ch_1", "值得记的事实")

    assert outbox.enqueued == [(fact.id, OutboxOp.UPSERT)], "只有普通记忆入队"
    assert pref.id not in [eid for eid, _ in outbox.enqueued]


async def test_编辑为偏好时入队删向量() -> None:
    """编辑成偏好后不该有向量，必须入一条 DELETE —— 否则旧向量留着按被改掉的
    内容命中，而那比没有向量更糟。"""
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)
    entry = await service.store("ch_1", "旧内容")

    await service.edit(entry.id, content="新内容", type=MemoryType.PREFERENCE)

    assert outbox.enqueued == [
        (entry.id, OutboxOp.UPSERT),  # store 时
        (entry.id, OutboxOp.DELETE),  # 编辑成偏好后
    ]


async def test_偏好不进语义命中() -> None:
    """即便向量库残留偏好 id（合表前蒸馏写的），偏好也只出现在偏好段、不占 top_k。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(
            id="mem_pref", channel_instance_id="ch_1",
            content="回答要简短", type=MemoryType.PREFERENCE,
        )
    )
    await repo.store(
        MemoryEntry(id="mem_fact", channel_instance_id="ch_1", content="超时是 30 秒")
    )
    vector = StubVectorStore(hits=["mem_pref", "mem_fact"])
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())

    hits = await service.query_for_context("ch_1", "问个问题", top_k=2)

    semantic = [h for h in hits if h.type is not MemoryType.PREFERENCE]
    assert [h.id for h in semantic] == ["mem_fact"], "残留偏好向量被 _semantic_hits 过滤"
    prefs = [h for h in hits if h.type is MemoryType.PREFERENCE]
    assert [h.id for h in prefs] == ["mem_pref"], "偏好经显式段返回"


async def test_只改type为偏好时入队删向量() -> None:
    """向量该不该存在由 type 决定，而 type 可改 —— 只改 type 不改 content 时
    也必须入队，否则旧向量残留在库里白占 top_k 名额。

    这是 `should_embed` 收口成一个函数要防的那条路径（见它的注释）。"""
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)
    entry = await service.store("ch_1", "回答要简短")

    await service.edit(entry.id, type=MemoryType.PREFERENCE)

    assert outbox.enqueued[-1] == (entry.id, OutboxOp.DELETE), "改成偏好后入队删向量"


async def test_只改type改回普通记忆时入队补建() -> None:
    """反方向：偏好当初连队都没入，改回普通记忆若不补入队，这条记忆永远进不了
    语义检索，只能靠时间倒序回落偶然捞到。"""
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)
    entry = await service.store("ch_1", "超时是 30 秒", type=MemoryType.PREFERENCE)
    assert outbox.enqueued == [], "偏好没入队"

    await service.edit(entry.id, type=MemoryType.FACT)

    assert outbox.enqueued == [(entry.id, OutboxOp.UPSERT)], "改回普通记忆后入队补建"


async def test_只改content不跨偏好边界时入队重算() -> None:
    """同类型内改内容：入一条 UPSERT，不该因为跨偏好边界的判定而入成 DELETE。"""
    outbox = FakeOutboxRepository()
    service, _, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)
    entry = await service.store("ch_1", "超时是 30 秒", type=MemoryType.FACT)

    await service.edit(entry.id, content="超时是 5 秒", type=MemoryType.FACT)

    assert outbox.enqueued == [
        (entry.id, OutboxOp.UPSERT),  # store
        (entry.id, OutboxOp.UPSERT),  # 改内容后重算
    ]


async def test_find_similar候选含偏好() -> None:
    """蒸馏比对候选必须含偏好：否则模型看不到已有偏好，每窗口都对「团队偏好」
    内容判 ADD|PREFERENCE，偏好确定性堆积。"""
    repo = FakeMemoryRepository()
    await repo.store(
        MemoryEntry(
            id="mem_pref", channel_instance_id="ch_1",
            content="回答要简短", type=MemoryType.PREFERENCE,
        )
    )
    await repo.store(
        MemoryEntry(id="mem_fact", channel_instance_id="ch_1", content="超时是 30 秒")
    )
    vector = StubVectorStore(hits=["mem_fact"])  # 向量命中只有事实，偏好无向量
    service, _, _ = _service(repo, vector=vector, embedder=StubEmbedder())

    candidates = await service.find_similar("ch_1", "回答要简短", top_k=3)

    candidate_ids = [c.id for c in candidates]
    assert "mem_fact" in candidate_ids
    assert "mem_pref" in candidate_ids, "偏好必须出现在蒸馏候选里，模型才能判 NOOP/UPDATE"


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


async def test_删除同时入队清向量() -> None:
    """回归点：改造前只删 Postgres 行、清向量失败仅打一条 warning，向量留在库里
    白占 top_k 名额。现在入队与删行同事务 —— 入队丢不了，删向量由 projector 重试
    到成功。"""
    repo = FakeMemoryRepository()
    await _seed(repo, 3)
    outbox = FakeOutboxRepository()
    service, _, _ = _service(repo, vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)

    await service.delete("mem_1")

    assert outbox.enqueued == [("mem_1", OutboxOp.DELETE)]


async def test_向量库不可用时删除仍成功() -> None:
    """Postgres 行是权威源。为向量库故障而报错会让用户以为没删掉。

    改造后这条更强了:写路径压根不碰向量库，故它可用与否与删除成败无关。
    向量的最终清除由 projector 的退避重试保证。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 2)
    service, _, audit_repo = _service(
        repo, vector=StubVectorStore(fail=True), embedder=StubEmbedder()
    )

    await service.delete("mem_0")  # 不该抛

    assert [e.id for e in repo.stored] == ["mem_1"]
    assert audit_repo.logs[-1].action is AuditAction.MEMORY_DELETE


# ===== embedding_ref / embedded_hash 由 projector 回填 =====
#
# 「建索引后回填 embedding_ref」那条用例移交给 tests/unit/test_projector.py ——
# 回填现在发生在投影侧。这里只守写路径的契约:写完之后这两个字段仍是空的。


async def test_写入后向量标记仍为空() -> None:
    """写路径不回填 embedding_ref / embedded_hash —— 它们由 projector 写。

    调用方不该依赖这两个字段判断「这条能不能被语义检索到」:那是暂态，由对账
    保证最终收敛（见 docs/plan-memory-outbox.md §5.1）。
    """
    service, repo, _ = _service(vector=StubVectorStore(), embedder=StubEmbedder())

    entry = await service.store("ch_1", "某事实")

    assert entry.embedding_ref is None
    assert entry.embedded_hash is None
    assert repo.stored[0].embedding_ref is None


async def test_没有向量库时也照常入队() -> None:
    """向量库缺席不影响写入 —— 意图先落库，配置补上后由对账/投影追平。"""
    outbox = FakeOutboxRepository()
    service, repo, _ = _service(outbox=outbox)

    entry = await service.store("ch_1", "某事实")

    assert entry.embedding_ref is None
    assert outbox.enqueued == [(entry.id, OutboxOp.UPSERT)]
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


async def test_改内容触发入队重算() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    outbox = FakeOutboxRepository()
    service, _, _ = _service(repo, vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)

    await service.edit("mem_0", content="全新的内容")

    assert outbox.enqueued == [("mem_0", OutboxOp.UPSERT)]


async def test_只改类型不入队() -> None:
    """类型变了但没跨偏好边界:向量对应的是文本，文本没变就不必重算。

    `_seed` 造的是 BACKGROUND_KNOWLEDGE，改成 FACT 两者都 should_embed，
    边界没跨 —— 入队等于让 projector 白跑一次 embedding。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    outbox = FakeOutboxRepository()
    service, _, _ = _service(repo, vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)

    await service.edit("mem_0", type=MemoryType.FACT)

    assert outbox.enqueued == []


async def test_内容未实际变化时不入队() -> None:
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    outbox = FakeOutboxRepository()
    service, _, _ = _service(repo, vector=StubVectorStore(), embedder=StubEmbedder(), outbox=outbox)

    await service.edit("mem_0", content="事实 0")  # 与原内容相同

    assert outbox.enqueued == []


async def test_embedder坏掉不影响编辑落库() -> None:
    """「重算失败就删旧向量」这条语义移交 projector —— 写路径压根不调 embedder，
    所以它坏没坏与编辑成败无关。

    改造前这里是最要紧的一条断言（留着旧向量比没有索引更糟，检索会持续按已被
    改掉的内容命中）。那个保证现在由 projector 承担:它 embed 失败时删掉旧向量
    再退避重试，对应用例在 tests/unit/test_projector.py。这里只守「写路径与
    embedder 解耦」。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    embedder = StubEmbedder()
    embedder.broken = True
    outbox = FakeOutboxRepository()
    service, _, _ = _service(repo, vector=StubVectorStore(), embedder=embedder, outbox=outbox)

    edited = await service.edit("mem_0", content="改了")

    assert edited is not None and edited.content == "改了"
    assert outbox.enqueued == [("mem_0", OutboxOp.UPSERT)]
    assert embedder.calls == [], "写路径不该调 embedding API"


async def test_embedder不可用时编辑照常入队() -> None:
    """凭据没配也要让意图落库 —— 配上之后由投影/对账追平。

    改造前这里断言的是「清掉旧向量」，那是同步双写时代的补救动作；现在
    未配置 embedder 只意味着投影会一直排队，而 lag 指标会把这件事暴露出来。
    """
    repo = FakeMemoryRepository()
    await _seed(repo, 1)
    vector = StubVectorStore()
    outbox = FakeOutboxRepository()
    service, _, _ = _service(
        repo, vector=vector, embedder=StubEmbedder(available=False), outbox=outbox
    )

    await service.edit("mem_0", content="改了")

    assert outbox.enqueued == [("mem_0", OutboxOp.UPSERT)]
    assert vector.deleted == [], "写路径不碰向量库"


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
