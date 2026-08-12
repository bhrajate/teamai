"""交互记录：仓储真 SQL 行为 + 服务层的留痕与清理。

这张表补的是审计的缺口 —— `audit_logs` 只记动作枚举加小字典，还原不出「模型
当时看到了什么」。故这里的重点断言是：全文与分拆 token 存得进、读得出，
`context_refs` 存的是引用而非内容快照，以及保留期清理真的会删。

仓储部分跑在内存 SQLite 上：生产走 asyncpg，但「倒序取最近 N 条」「按时间删」
这类语义与方言无关，用 SQLite 验足够且不必起容器（与 test_budget_configure 同思路）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.application.interaction import InteractionService
from teamai.domain.models import AgentInteraction, InteractionResult
from teamai.domain.repositories import InteractionRepository
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.interaction import AgentInteractionModel  # noqa: F401  注册表
from teamai.infrastructure.repositories.interaction import SQLInteractionRepository

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> SQLInteractionRepository:
    return SQLInteractionRepository(session)


def _interaction(
    iid: str = "itr_1",
    *,
    task_id: str = "task_1",
    channel: str = "ch_1",
    created_at: datetime | None = None,
    **kwargs,
) -> AgentInteraction:
    return AgentInteraction(
        id=iid,
        task_id=task_id,
        channel_instance_id=channel,
        thread_ref="1700000000.1",
        user_prompt="帮我看下告警",
        system_prompt="（系统提示词）",
        model_level="light",
        created_at=created_at or NOW,
        **kwargs,
    )


# ===== 仓储 =====


async def test_写入后读回全部字段(repo: InteractionRepository) -> None:
    await repo.record(
        _interaction(
            requester_id="U1",
            model_id="anthropic:claude-3-5-haiku",
            response="已查看",
            tokens_in=120,
            tokens_out=45,
            context_refs={"memory_entry_ids": ["mem_1", "mem_2"], "thread_history_count": 3},
        )
    )

    got = await repo.get("itr_1")

    assert got is not None
    assert got.user_prompt == "帮我看下告警"
    assert got.system_prompt == "（系统提示词）"
    assert got.response == "已查看"
    assert got.model_id == "anthropic:claude-3-5-haiku"
    assert (got.tokens_in, got.tokens_out) == (120, 45)
    assert got.tokens_total == 165
    assert got.context_refs["memory_entry_ids"] == ["mem_1", "mem_2"]
    assert got.result is InteractionResult.DONE


async def test_失败结果与错因存得下(repo: InteractionRepository) -> None:
    await repo.record(
        _interaction(result=InteractionResult.FAILED, error="工具调用超时")
    )

    got = await repo.get("itr_1")

    assert got is not None
    assert got.result is InteractionResult.FAILED
    assert got.error == "工具调用超时"


async def test_按频道倒序且受limit约束(repo: InteractionRepository) -> None:
    """这张表全量增长，无界查询会随使用时长拖慢控制台。"""
    for i in range(5):
        await repo.record(
            _interaction(f"itr_{i}", created_at=NOW + timedelta(minutes=i))
        )

    rows = await repo.list_by_channel("ch_1", limit=2)

    assert [r.id for r in rows] == ["itr_4", "itr_3"]


async def test_按频道隔离(repo: InteractionRepository) -> None:
    await repo.record(_interaction("itr_a", channel="ch_A"))
    await repo.record(_interaction("itr_b", channel="ch_B"))

    rows = await repo.list_by_channel("ch_B")

    assert [r.id for r in rows] == ["itr_b"]


async def test_按任务正序(repo: InteractionRepository) -> None:
    """同一任务的多次调用要能顺着读下来（重试、多阶段）。"""
    for i in range(3):
        await repo.record(
            _interaction(f"itr_{i}", task_id="task_x", created_at=NOW + timedelta(minutes=i))
        )
    await repo.record(_interaction("itr_other", task_id="task_y"))

    rows = await repo.list_by_task("task_x")

    assert [r.id for r in rows] == ["itr_0", "itr_1", "itr_2"]


async def test_坏JSON不让整条记录读不出来(repo: SQLInteractionRepository, session) -> None:
    """其余字段（提示词、响应、token）仍有价值，引用关系降级为空即可。"""
    await repo.record(_interaction())
    model = await session.get(AgentInteractionModel, "itr_1")
    model.context_refs = "{不是合法 JSON"
    await session.commit()

    got = await repo.get("itr_1")

    assert got is not None
    assert got.context_refs == {}
    assert got.user_prompt == "帮我看下告警"


async def test_清理只删过期的(repo: InteractionRepository) -> None:
    await repo.record(_interaction("itr_old", created_at=NOW - timedelta(days=100)))
    await repo.record(_interaction("itr_new", created_at=NOW))

    deleted = await repo.purge_before(NOW - timedelta(days=90))

    assert deleted == 1
    assert [r.id for r in await repo.list_by_channel("ch_1")] == ["itr_new"]


# ===== 服务层 =====


class BrokenRepo(InteractionRepository):
    async def record(self, interaction: AgentInteraction) -> None:
        raise ConnectionError("库挂了")

    async def get(self, interaction_id: str) -> AgentInteraction | None:
        return None

    async def list_by_channel(self, channel_instance_id: str, limit: int = 50) -> list:
        return []

    async def list_by_task(self, task_id: str) -> list:
        return []

    async def purge_before(self, cutoff: datetime) -> int:
        return 0


async def test_服务写入带上下文引用(repo: InteractionRepository) -> None:
    service = InteractionService(repo)

    written = await service.record(
        task_id="task_1",
        channel_instance_id="ch_1",
        thread_ref="t1",
        user_prompt="问题",
        system_prompt="系统",
        model_level="full",
        context_refs={"memory_entry_ids": ["mem_1"]},
    )

    assert written is not None
    assert written.id.startswith("itr_")
    got = await repo.get(written.id)
    assert got is not None and got.context_refs["memory_entry_ids"] == ["mem_1"]


async def test_留痕失败不外抛() -> None:
    """用户已拿到回答，却因审计写库出错而收到「任务执行失败」是更糟的结果。"""
    service = InteractionService(BrokenRepo())

    result = await service.record(
        task_id="task_1",
        channel_instance_id="ch_1",
        thread_ref="t1",
        user_prompt="问题",
        system_prompt="系统",
        model_level="light",
    )

    assert result is None


async def test_保留期清理按天算(repo: InteractionRepository) -> None:
    await repo.record(_interaction("itr_old", created_at=NOW - timedelta(days=91)))
    await repo.record(_interaction("itr_keep", created_at=NOW - timedelta(days=89)))
    service = InteractionService(repo, retention_days=90)

    deleted = await service.purge_expired(now=NOW)

    assert deleted == 1
    assert [r.id for r in await repo.list_by_channel("ch_1")] == ["itr_keep"]


async def test_保留期为零表示不清理(repo: InteractionRepository) -> None:
    """留给「合规要求永久留存」的部署。"""
    await repo.record(_interaction("itr_ancient", created_at=NOW - timedelta(days=9999)))
    service = InteractionService(repo, retention_days=0)

    assert await service.purge_expired(now=NOW) == 0
    assert len(await repo.list_by_channel("ch_1")) == 1
