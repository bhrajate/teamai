"""SQLCheckpointRepository 的真 SQL 行为。

跑在内存 SQLite 上。三处语义必须真过一遍 SQL 才有意义：

- **覆盖写保留 attempts** —— 用 merge 会把它清零，于是反复崩溃的任务能无限续跑
- **bytes 往返** —— LargeBinary 在不同方言下的绑定行为，这里存的是消息历史
- **bump_attempts 的 RETURNING** —— 原子自增，读改写会丢计数
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models.approval import ApprovalRecord, PendingApproval
from teamai.infrastructure.db import Base
from teamai.infrastructure.repositories.checkpoint import SQLCheckpointRepository


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SQLCheckpointRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield SQLCheckpointRepository(s)
    await engine.dispose()


def _session(repo: SQLCheckpointRepository) -> AsyncSession:
    return repo._session  # noqa: SLF001  测试要直接改表核对边界行为


async def test_落库后能取回(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"hello", 100)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.task_id == "task_1"
    assert cp.messages == b"hello"
    assert cp.tokens_used == 100
    assert cp.attempts == 0


async def test_不存在时返回None(repo: SQLCheckpointRepository) -> None:
    """首次执行、以及纯文本任务从未落过检查点，都走这条路径。"""
    assert await repo.get("task_nope") is None


async def test_bytes原样往返(repo: SQLCheckpointRepository) -> None:
    """消息历史是二进制 blob，含 0x00 与非 UTF-8 字节也必须原样存取。"""
    blob = bytes(range(256)) + b'{"json": "\xe4\xb8\xad\xe6\x96\x87"}'
    await repo.upsert("task_1", blob, 0)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.messages == blob


async def test_覆盖写换内容与token(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"first", 100)
    await repo.upsert("task_1", b"second", 250)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.messages == b"second"
    assert cp.tokens_used == 250


async def test_覆盖写保留attempts(repo: SQLCheckpointRepository) -> None:
    """回归点。用 session.merge 写一个新对象会把 attempts 覆盖成 0 ——
    于是每落一个检查点就把续跑计数清零，attempts 上限形同虚设，
    一个反复崩溃的任务能无限续跑。
    """
    await repo.upsert("task_1", b"a", 10)
    await repo.bump_attempts("task_1")
    await repo.bump_attempts("task_1")

    await repo.upsert("task_1", b"b", 20)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.attempts == 2, "覆盖写把续跑计数清零了"


async def test_覆盖写保留created_at(repo: SQLCheckpointRepository) -> None:
    """created_at 记的是「检查点首次出现在何时」，与本次写入内容无关。"""
    await repo.upsert("task_1", b"a", 10)
    first = await repo.get("task_1")
    assert first is not None

    await repo.upsert("task_1", b"b", 20)
    second = await repo.get("task_1")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


async def test_bump_attempts返回自增后的值(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"a", 0)

    assert await repo.bump_attempts("task_1") == 1
    assert await repo.bump_attempts("task_1") == 2
    assert await repo.bump_attempts("task_1") == 3

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.attempts == 3


async def test_bump不存在的任务返回0(repo: SQLCheckpointRepository) -> None:
    """巡检可能在检查点刚被删掉后才 bump，不该抛异常。"""
    assert await repo.bump_attempts("task_nope") == 0


async def test_删除(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"a", 0)
    await repo.delete("task_1")
    assert await repo.get("task_1") is None


async def test_删不存在的静默返回(repo: SQLCheckpointRepository) -> None:
    """大多数任务（纯文本、单轮）从未落过检查点，而终态迁移对它们同样会走到
    这里 —— 抛异常会让正常任务的完成路径炸掉。"""
    await repo.delete("task_nope")  # 不抛即通过


async def test_待批项往返(repo: SQLCheckpointRepository) -> None:
    p = PendingApproval(
        tool_call_id="tc_1",
        tool_name="github",
        args={"action": "create_pr", "title": "修 bug"},
        required=2,
        approvals=[ApprovalRecord(user_id="U1", override_args={"title": "改过的"})],
    )
    await repo.set_pending_approval("task_1", b"msgs", p)

    got = await repo.get_pending_approval("task_1")
    assert got is not None
    assert got.tool_call_id == "tc_1"
    assert got.tool_name == "github"
    assert got.args == {"action": "create_pr", "title": "修 bug"}
    assert got.required == 2
    assert [a.user_id for a in got.approvals] == ["U1"]
    assert got.approvals[0].override_args == {"title": "改过的"}
    # 时间要能往返（JSON 里是 isoformat 字符串）
    assert got.created_at.tzinfo is not None


async def test_待批时行不存在也能建(repo: SQLCheckpointRepository) -> None:
    """待批可能发生在**第一轮**工具调用上 —— 那时还没落过任何检查点。"""
    p = PendingApproval(tool_call_id="tc_1", tool_name="github")
    await repo.set_pending_approval("task_new", b"first", p)

    cp = await repo.get("task_new")
    assert cp is not None
    assert cp.messages == b"first"
    assert await repo.get_pending_approval("task_new") is not None


async def test_待批不影响tokens与attempts(repo: SQLCheckpointRepository) -> None:
    """审批不是续跑，不该动那两个计数。"""
    await repo.upsert("task_1", b"a", 500)
    await repo.bump_attempts("task_1")

    await repo.set_pending_approval("task_1", b"b", PendingApproval("tc", "github"))

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.tokens_used == 500
    assert cp.attempts == 1
    assert cp.messages == b"b", "历史要换成待批时的"


async def test_清待批保留历史(repo: SQLCheckpointRepository) -> None:
    """恢复执行正要用那段历史，且此后崩溃仍要能续跑。"""
    await repo.set_pending_approval("task_1", b"msgs", PendingApproval("tc", "github"))

    await repo.clear_pending_approval("task_1")

    assert await repo.get_pending_approval("task_1") is None
    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.messages == b"msgs", "历史被一起清掉了"


async def test_无待批时返回None(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"a", 0)
    assert await repo.get_pending_approval("task_1") is None
    assert await repo.get_pending_approval("task_nope") is None


async def test_required为字符串时也能读成int(repo: SQLCheckpointRepository) -> None:
    """库里若被手工改成 "2"，int() 兜不住的话 required 会静默变 0，
    satisfied 恒为 True —— 工具直接放行，审批形同虚设。"""
    import json

    from teamai.infrastructure.orm.checkpoint import TaskCheckpointModel

    await repo.set_pending_approval("task_1", b"a", PendingApproval("tc", "github", required=2))
    session = _session(repo)
    m = (
        await session.execute(
            select(TaskCheckpointModel).where(TaskCheckpointModel.task_id == "task_1")
        )
    ).scalars().first()
    assert m is not None
    data = json.loads(m.pending_approval)
    data["required"] = "2"  # 字符串
    m.pending_approval = json.dumps(data)
    await session.flush()

    got = await repo.get_pending_approval("task_1")
    assert got is not None
    assert got.required == 2
    assert not got.satisfied


async def test_按updated_at筛超时待批(repo: SQLCheckpointRepository) -> None:
    """用 updated_at 而非 created_at：后者是「检查点首次出现」，而待批可能发生在
    任务跑了很久之后 —— 用它会把刚开始等的任务也判超时。"""
    from datetime import timedelta

    from teamai.infrastructure.orm.checkpoint import TaskCheckpointModel

    # 先落检查点（created_at 很早），再让它进入待批（updated_at 是现在）
    await repo.upsert("task_old", b"a", 0)
    session = _session(repo)
    m = (
        await session.execute(
            select(TaskCheckpointModel).where(TaskCheckpointModel.task_id == "task_old")
        )
    ).scalars().first()
    assert m is not None
    m.created_at = datetime.now(UTC) - timedelta(days=30)
    await session.flush()

    await repo.set_pending_approval("task_old", b"a", PendingApproval("tc", "github"))

    # created_at 在 30 天前，但刚开始等审批 → 不该被判超时
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    assert await repo.list_pending_before(cutoff) == []

    # 把 updated_at 拨回去 → 该被捞出
    m.updated_at = datetime.now(UTC) - timedelta(days=2)
    await session.flush()
    assert await repo.list_pending_before(cutoff) == ["task_old"]


async def test_无待批的任务不进超时列表(repo: SQLCheckpointRepository) -> None:
    from datetime import timedelta

    await repo.upsert("task_1", b"a", 0)  # 有检查点但不在等审批
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert await repo.list_pending_before(cutoff) == []


async def test_删检查点一并删待批(repo: SQLCheckpointRepository) -> None:
    await repo.set_pending_approval("task_1", b"a", PendingApproval("tc", "github"))
    await repo.delete("task_1")
    assert await repo.get_pending_approval("task_1") is None


async def test_任务之间互不影响(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"one", 10)
    await repo.upsert("task_2", b"two", 20)
    await repo.bump_attempts("task_1")

    await repo.delete("task_1")

    assert await repo.get("task_1") is None
    cp2 = await repo.get("task_2")
    assert cp2 is not None
    assert cp2.messages == b"two"
    assert cp2.attempts == 0
