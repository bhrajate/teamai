"""MemoryProjector：状态式决策表、回填、退避、以及 embedder 缺席时的行为。

最要紧的一组是决策表（`test_决策_*`）：projector **只看 memory_entries 的当前
状态，不看 outbox 记录里的 op**。按 op 行事会让滞后的 UPSERT 拿旧内容覆盖新
向量，而 edit / supersede 会让同一条记忆短时间内变化多次 —— 这种滞后是常态。
每条都带负向断言（没调 embed / 没调 delete），少了它们，逻辑退回按 op 行事时
测试仍会绿。

设计见 docs/plan-memory-outbox.md §5.2。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from teamai.application.projector import MemoryProjector, content_hash
from teamai.domain.models import MemoryEntry, MemoryType, OutboxOp
from tests.fakes import FakeMemoryRepository, FakeOutboxRepository


class StubEmbedder:
    """可控 embedder。`available` / 返回值 / 是否抛错都能按需摆布。"""

    def __init__(self, *, available: bool = True, vector=None, broken: bool = False) -> None:
        self._available = available
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.broken = broken
        self.calls: list[str] = []

    @property
    def dimensions(self) -> int:
        return 3

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.broken:
            # 真实 embedder 把异常咽成空列表（读路径要那个语义）
            return []
        return list(self._vector)


class StubVectorStore:
    def __init__(self, *, fail_upsert: bool = False, fail_delete: bool = False) -> None:
        self.upserted: list[tuple[str, str]] = []  # (entry_id, content)
        self.deleted: list[str] = []
        self._fail_upsert = fail_upsert
        self._fail_delete = fail_delete

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str:
        if self._fail_upsert:
            raise ConnectionError("Qdrant 不可用")
        self.upserted.append((entry.id, entry.content))
        return f"point-{entry.id}"

    async def query(self, channel_instance_id: str, embedding, top_k: int) -> list[str]:
        return []

    async def delete(self, entry_id: str) -> None:
        if self._fail_delete:
            raise ConnectionError("Qdrant 不可用")
        self.deleted.append(entry_id)


def _entry(**kw) -> MemoryEntry:
    base = dict(
        id="mem_1",
        channel_instance_id="ch_1",
        content="订单服务的超时阈值是 30 秒",
        type=MemoryType.FACT,
    )
    base.update(kw)
    return MemoryEntry(**base)  # type: ignore[arg-type]


@dataclass
class _FakeScope:
    """投影器要的两个仓储。形状对齐 container.JobScope 的对应字段。"""

    outbox_repo: object
    memory_repo: object


def _projector(repo, outbox, vector, embedder, **kw) -> MemoryProjector:
    """装一个投影器。

    scope 工厂每次都返回**同一对**仓储实例 —— 这与生产不同（那边每轮换 session），
    但测试要在调用之后检查仓储里的状态，换实例就查不到了。工厂被调用几次这件事
    由 `test_每轮开一个新scope` 单独守。
    """

    @asynccontextmanager
    async def _factory():
        yield _FakeScope(outbox_repo=outbox, memory_repo=repo)

    return MemoryProjector(_factory, vector, embedder, **kw)


async def _setup(entry: MemoryEntry | None, op: OutboxOp = OutboxOp.UPSERT, **stubs):
    """造一条 outbox 记录 + 可选的记忆行，返回 (projector, repo, outbox, vector, embedder)。"""
    repo = FakeMemoryRepository()
    if entry is not None:
        await repo.store(entry)
    outbox = FakeOutboxRepository()
    await outbox.enqueue(entry.id if entry else "mem_gone", op)
    vector = stubs.pop("vector", None) or StubVectorStore()
    embedder = stubs.pop("embedder", None) or StubEmbedder()
    return _projector(repo, outbox, vector, embedder, **stubs), repo, outbox, vector, embedder


# ===== 决策表：只看当前状态，不看 op =====


@pytest.mark.parametrize("op", [OutboxOp.UPSERT, OutboxOp.DELETE])
async def test_决策_行不存在则删向量(op: OutboxOp) -> None:
    """记忆已被物理删除。回读为空正是「删向量」的信号 —— 这也是 delete 路径
    必须让入队与删行同事务的原因。

    两种 op 都参数化：这条同时证明「不看 op」—— 入的是 UPSERT 也照样删。
    """
    proj, _, outbox, vector, embedder = await _setup(None, op)

    report = await proj.run_once()

    assert vector.deleted == ["mem_gone"]
    assert embedder.calls == [], "行都没了，不该去 embed"
    assert report.deleted == ["mem_gone"]
    assert outbox.entries == [], "处理成功即删记录"


@pytest.mark.parametrize("op", [OutboxOp.UPSERT, OutboxOp.DELETE])
async def test_决策_偏好则删向量(op: OutboxOp) -> None:
    """偏好在检索时被无条件全量带上，建向量只会白占 top_k 名额。

    入的是 UPSERT 也要删 —— 这条覆盖「edit 把普通记忆改成偏好」的路径。
    """
    proj, _, _, vector, embedder = await _setup(
        _entry(type=MemoryType.PREFERENCE, embedding_ref="point-mem_1"), op
    )

    report = await proj.run_once()

    assert vector.deleted == ["mem_1"]
    assert embedder.calls == []
    assert report.deleted == ["mem_1"]


@pytest.mark.parametrize("op", [OutboxOp.UPSERT, OutboxOp.DELETE])
async def test_决策_已被取代则删向量(op: OutboxOp) -> None:
    """过期事实的向量留着比没有更糟 —— 检索会按已作废的内容命中。"""
    proj, _, _, vector, embedder = await _setup(
        _entry(superseded_by="mem_2", embedding_ref="point-mem_1"), op
    )

    report = await proj.run_once()

    assert vector.deleted == ["mem_1"]
    assert embedder.calls == []
    assert report.deleted == ["mem_1"]


async def test_决策_应当有向量则按当前内容写入并回填() -> None:
    proj, repo, outbox, vector, embedder = await _setup(_entry())

    report = await proj.run_once()

    assert vector.upserted == [("mem_1", "订单服务的超时阈值是 30 秒")]
    assert embedder.calls == ["订单服务的超时阈值是 30 秒"]
    assert report.upserted == ["mem_1"]
    stored = await repo.get("mem_1")
    assert stored is not None
    assert stored.embedding_ref == "point-mem_1"
    assert stored.embedded_hash == content_hash("订单服务的超时阈值是 30 秒")
    assert outbox.entries == []


async def test_决策_按回读到的内容而非入队时的内容() -> None:
    """核心防线：入队后内容又被改过，投影必须用**当前**内容。

    这正是「不按 op、回读当前行」换来的性质。若实现改成从 outbox 载荷里取内容，
    这条会红。
    """
    repo = FakeMemoryRepository()
    await repo.store(_entry(content="旧内容"))
    outbox = FakeOutboxRepository()
    await outbox.enqueue("mem_1", OutboxOp.UPSERT)
    # 入队之后内容被改（模拟一次 edit）
    entry = await repo.get("mem_1")
    assert entry is not None
    entry.content = "新内容"
    await repo.update(entry)

    vector, embedder = StubVectorStore(), StubEmbedder()
    await _projector(repo, outbox, vector, embedder).run_once()

    assert vector.upserted == [("mem_1", "新内容")]
    assert embedder.calls == ["新内容"]


# ===== hash 短路 =====


async def test_hash相同则跳过不重复embed() -> None:
    """连续 edit 会给同一条记忆入多条队，后处理的那些走到这里。

    这是 `embedded_hash` 存在的直接收益 —— 没有它，每条重复记录都要付一次
    embedding 调用。
    """
    content = "订单服务的超时阈值是 30 秒"
    proj, _, outbox, vector, embedder = await _setup(
        _entry(embedding_ref="point-mem_1", embedded_hash=content_hash(content))
    )

    report = await proj.run_once()

    assert embedder.calls == [], "hash 相同就不该再 embed"
    assert vector.upserted == []
    assert report.skipped == ["mem_1"]
    assert outbox.entries == [], "跳过也算处理成功"


async def test_有ref但hash不符则重算() -> None:
    """内容漂移：向量存在但对应的是旧文本。只看 embedding_ref 判不出这种情况，
    这正是 embedded_hash 必须与它并存的理由。"""
    proj, repo, _, vector, embedder = await _setup(
        _entry(content="改过的内容", embedding_ref="point-mem_1", embedded_hash="stale")
    )

    await proj.run_once()

    assert vector.upserted == [("mem_1", "改过的内容")]
    stored = await repo.get("mem_1")
    assert stored is not None and stored.embedded_hash == content_hash("改过的内容")


async def test_有hash但无ref则重算() -> None:
    """反向：hash 在但向量丢了（例如向量库被重建过）。只看 hash 判不出向量丢失。"""
    content = "订单服务的超时阈值是 30 秒"
    proj, _, _, vector, _ = await _setup(
        _entry(embedding_ref=None, embedded_hash=content_hash(content))
    )

    await proj.run_once()

    assert vector.upserted == [("mem_1", content)]


# ===== 删向量时清标记 =====


async def test_删向量成功后才清标记() -> None:
    proj, repo, _, _, _ = await _setup(
        _entry(superseded_by="mem_2", embedding_ref="point-mem_1", embedded_hash="h")
    )

    await proj.run_once()

    stored = await repo.get("mem_1")
    assert stored is not None
    assert stored.embedding_ref is None
    assert stored.embedded_hash is None


async def test_删向量失败则保留标记并重试() -> None:
    """必须保留：清了标记而向量实际还在，对账就查不出来 —— 那正是改造前
    supersede 的毛病（先清 embedding_ref，删失败只打 warning）。"""
    proj, repo, outbox, _, _ = await _setup(
        _entry(superseded_by="mem_2", embedding_ref="point-mem_1", embedded_hash="h"),
        vector=StubVectorStore(fail_delete=True),
    )

    report = await proj.run_once()

    assert len(report.failed) == 1
    stored = await repo.get("mem_1")
    assert stored is not None
    assert stored.embedding_ref == "point-mem_1", "删失败就不该清标记"
    assert outbox.entries[0].attempts == 1, "记一次失败，等退避后重试"


# ===== 失败与退避 =====


async def test_embedder返回空视为失败而非成功() -> None:
    """真实 embedder 把异常咽成空列表。若这里当成功处理，outbox 记录会被删除，
    向量永久缺失 —— 这是最容易漏的一处。"""
    proj, _, outbox, vector, _ = await _setup(_entry(), embedder=StubEmbedder(broken=True))

    report = await proj.run_once()

    assert len(report.failed) == 1
    assert vector.upserted == []
    assert outbox.entries[0].attempts == 1
    assert "空向量" in (outbox.entries[0].last_error or "")


async def test_向量库异常时记失败并保留记录() -> None:
    proj, _, outbox, _, _ = await _setup(_entry(), vector=StubVectorStore(fail_upsert=True))

    report = await proj.run_once()

    assert len(report.failed) == 1
    assert len(outbox.entries) == 1, "记录必须留着重试"
    assert "Qdrant" in (outbox.entries[0].last_error or "")


async def test_退避是指数且有上限() -> None:
    """`attempts` 已经很大时不该算出天文数字的等待 —— 一条坏记录会把重试拖到
    几小时后，而队列里别的记录不受影响，故障看起来就像「投影停了」。"""
    from teamai.application.projector import MAX_BACKOFF_SECONDS

    repo = FakeMemoryRepository()
    await repo.store(_entry())
    outbox = FakeOutboxRepository()
    record = await outbox.enqueue("mem_1", OutboxOp.UPSERT)
    record.attempts = 20  # 2**20 秒 ≈ 12 天

    proj = _projector(repo, outbox, StubVectorStore(fail_upsert=True), StubEmbedder())
    captured: list[int] = []
    original = outbox.fail

    async def _spy(outbox_id, error, *, max_attempts, backoff_seconds):
        captured.append(backoff_seconds)
        await original(outbox_id, error, max_attempts=max_attempts, backoff_seconds=backoff_seconds)

    outbox.fail = _spy  # type: ignore[method-assign]
    await proj.run_once()

    assert captured == [MAX_BACKOFF_SECONDS]


async def test_单条失败不打断整批() -> None:
    """一次 embedding 限流不该让其余记录跟着停。"""
    repo = FakeMemoryRepository()
    await repo.store(_entry(id="mem_ok"))
    # mem_gone 不存在 → 走删向量路径，而向量库的 delete 会失败
    outbox = FakeOutboxRepository()
    await outbox.enqueue("mem_gone", OutboxOp.DELETE)
    await outbox.enqueue("mem_ok", OutboxOp.UPSERT)

    proj = _projector(repo, outbox, StubVectorStore(fail_delete=True), StubEmbedder())
    report = await proj.run_once()

    assert len(report.failed) == 1
    assert report.upserted == ["mem_ok"], "前一条失败后仍要处理后一条"


# ===== embedder 缺席 =====


async def test_embedder不可用时不领取也不置死信() -> None:
    """凭据没配时记录留在队列里，补上之后照常处理。

    判成失败会让它们在几次重试后进死信 —— 而「没配凭据」不是数据问题，等配置
    补上就好。lag 指标会诚实地把「语义检索实际关闭」暴露出来。
    """
    proj, _, outbox, vector, embedder = await _setup(
        _entry(), embedder=StubEmbedder(available=False)
    )

    report = await proj.run_once()

    assert report.claimed == 0
    assert vector.upserted == [] and vector.deleted == []
    assert embedder.calls == []
    assert len(outbox.entries) == 1
    assert outbox.entries[0].failed_at is None, "不该置死信"


# ===== 常驻循环 =====


async def test_领取失败时本轮空转而不抛() -> None:
    """数据库不可用时 run_once 该返回空报告，让 run_forever 继续下一轮。

    抛出去会让 run_forever 的 except 接住 —— 结果一样，但那条路径每轮都记
    error 级日志。这里返回空报告、由 claim 内部记一次 error，噪音更少。
    """
    repo = FakeMemoryRepository()
    outbox = FakeOutboxRepository()

    async def _boom(**kw):
        raise ConnectionError("数据库连接断了")

    outbox.claim = _boom  # type: ignore[method-assign]
    proj = _projector(repo, outbox, StubVectorStore(), StubEmbedder())

    report = await proj.run_once()

    assert report.claimed == 0
    assert report.failed == []


async def test_每轮开一个新scope() -> None:
    """投影器收 scope 工厂而非仓储实例，正是为了每轮换一个 session。

    绑死在一个 session 上会有两个后果：`run_forever` 那个长期存活的循环让同一个
    AsyncSession 活到进程退出（而它不允许并发使用，投影循环与定时任务跑在同一个
    事件循环上），且长事务让连接一直挂在池子里。

    这条守「工厂被调用的次数 == 轮数」，且退出时 scope 真的被关掉。
    """
    opened = {"n": 0}
    closed = {"n": 0}

    @asynccontextmanager
    async def _factory():
        opened["n"] += 1
        try:
            yield _FakeScope(outbox_repo=FakeOutboxRepository(), memory_repo=FakeMemoryRepository())
        finally:
            closed["n"] += 1

    proj = MemoryProjector(_factory, StubVectorStore(), StubEmbedder())

    await proj.run_once()
    await proj.run_once()
    await proj.run_once()

    assert opened["n"] == 3
    assert closed["n"] == 3, "每轮退出时 scope 必须被关掉，否则连接泄漏"


async def test_embedder不可用时不开scope() -> None:
    """连 scope 都不该开 —— 开了就是白建一次 session。"""
    opened = {"n": 0}

    @asynccontextmanager
    async def _factory():
        opened["n"] += 1
        yield _FakeScope(outbox_repo=FakeOutboxRepository(), memory_repo=FakeMemoryRepository())

    proj = MemoryProjector(_factory, StubVectorStore(), StubEmbedder(available=False))

    await proj.run_once()

    assert opened["n"] == 0


async def test_run_forever_可被stop立刻打断() -> None:
    """worker 退出时要能立刻停，而不是等满一个轮询间隔。"""
    repo = FakeMemoryRepository()
    outbox = FakeOutboxRepository()
    proj = _projector(
        repo, outbox, StubVectorStore(), StubEmbedder(), poll_interval_seconds=30.0
    )
    stop = asyncio.Event()

    task = asyncio.create_task(proj.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    # 轮询间隔是 30 秒，若不可打断这里会超时
    await asyncio.wait_for(task, timeout=1.0)


async def test_run_forever_单轮异常不退出循环() -> None:
    """数据库短暂不可用时该等着，而不是让整个投影链路停到下次重启。"""
    repo = FakeMemoryRepository()
    outbox = FakeOutboxRepository()
    proj = _projector(
        repo, outbox, StubVectorStore(), StubEmbedder(), poll_interval_seconds=0.01
    )

    calls = {"n": 0}

    async def _boom(**kw):
        calls["n"] += 1
        raise RuntimeError("数据库连接断了")

    outbox.claim = _boom  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(proj.run_forever(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert calls["n"] >= 2, "抛异常后应继续下一轮，而不是退出"
