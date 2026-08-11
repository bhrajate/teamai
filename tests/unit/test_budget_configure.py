"""频道配额的设定语义 —— 对真 SQL 跑，不用 fake 仓储。

这组必须打到真仓储：本文件防的 bug 就出在 ORM 语义上 ——
`BudgetRepository.upsert` 走 `session.merge`，按主键匹配，故传一个新 id
进去是 INSERT 而非 UPDATE。而 `budget_quotas` 上没有 channel_instance_id
唯一约束，`get_for_channel` 又用 `.first()` 且无 ORDER BY，于是：

    管理员把上限从 1000 调到 2000 → 表里多出一行 → 读回来仍是旧行 1000
    → 页面显示「没改动」，且此后哪行生效取决于数据库

fake 仓储通常是 dict[id] 或 dict[channel_id]，两种都复现不出这个问题，
所以这里起内存 SQLite。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.application.budget import BudgetController
from teamai.domain.models.budget import BudgetPeriod, BudgetState
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.budget import BudgetQuotaModel
from teamai.infrastructure.repositories.budget import SQLBudgetRepository
from tests.fakes import FakeAuditRepository

CH = "ch_1"


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
def audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest.fixture
def repo(session: AsyncSession) -> SQLBudgetRepository:
    return SQLBudgetRepository(session)


@pytest.fixture
def controller(repo: SQLBudgetRepository, audit_repo: FakeAuditRepository) -> BudgetController:
    return BudgetController(repo, AuditLogWriter(audit_repo))


async def _row_count(session: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(BudgetQuotaModel)
        .where(BudgetQuotaModel.channel_instance_id == CH)
    )
    return (await session.execute(stmt)).scalar() or 0


async def test_首次设定新建一行(controller: BudgetController, session: AsyncSession) -> None:
    quota = await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)

    assert quota.token_limit == 1000
    assert quota.used_tokens == 0
    assert await _row_count(session) == 1


async def test_二次设定不新增行(controller: BudgetController, session: AsyncSession) -> None:
    """核心回归：改上限必须是 UPDATE。多出一行会让此后读到哪行都不确定。"""
    first = await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)
    second = await controller.configure_channel_quota(CH, 2000, BudgetPeriod.MONTHLY)

    assert await _row_count(session) == 1
    assert second.id == first.id, "复用了新 id，会 INSERT 出第二行"
    assert second.token_limit == 2000


async def test_改上限不清零用量(
    controller: BudgetController, repo: SQLBudgetRepository
) -> None:
    """调额度是「改上限」，不该顺手抹掉本周期已记录的消耗。"""
    await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)
    await controller.consume(CH, 400)

    await controller.configure_channel_quota(CH, 2000, BudgetPeriod.MONTHLY)

    quota = await repo.get_for_channel(CH)
    assert quota is not None
    assert quota.used_tokens == 400
    assert quota.token_limit == 2000
    assert quota.remaining == 1600


async def test_改上限后读回的是新值(
    controller: BudgetController, repo: SQLBudgetRepository
) -> None:
    """站在页面视角：保存后刷新，必须看到刚填的值。"""
    await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)
    await controller.configure_channel_quota(CH, 2000, BudgetPeriod.MONTHLY)

    quota = await repo.get_for_channel(CH)
    assert quota is not None
    assert quota.token_limit == 2000


async def test_调高上限解除耗尽(
    controller: BudgetController, repo: SQLBudgetRepository
) -> None:
    """管理员调高上限的意图就是让频道重新可用，否则还得等下个周期的定时重置。"""
    await controller.configure_channel_quota(CH, 100, BudgetPeriod.MONTHLY)
    await controller.consume(CH, 100)

    exhausted = await repo.get_for_channel(CH)
    assert exhausted is not None and exhausted.state is BudgetState.EXHAUSTED

    await controller.configure_channel_quota(CH, 500, BudgetPeriod.MONTHLY)

    quota = await repo.get_for_channel(CH)
    assert quota is not None
    assert quota.state is BudgetState.ACTIVE
    assert await controller.check_quota(CH) is True


async def test_上限仍低于用量则维持耗尽(
    controller: BudgetController, repo: SQLBudgetRepository
) -> None:
    """调高了但还是不够用，不能假装恢复 —— 那会让频道刚放行又立刻耗尽。"""
    await controller.configure_channel_quota(CH, 100, BudgetPeriod.MONTHLY)
    await controller.consume(CH, 100)

    await controller.configure_channel_quota(CH, 100, BudgetPeriod.MONTHLY)

    quota = await repo.get_for_channel(CH)
    assert quota is not None
    assert quota.state is BudgetState.EXHAUSTED


async def test_改周期保留周期起点(
    controller: BudgetController, repo: SQLBudgetRepository
) -> None:
    """period_started_at 若被刷新，正在计费的周期会被无声延长。"""
    first = await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)
    started = first.period_started_at

    await controller.configure_channel_quota(CH, 1000, BudgetPeriod.DAILY)

    quota = await repo.get_for_channel(CH)
    assert quota is not None
    assert quota.period is BudgetPeriod.DAILY
    # 比裸时间戳而非 datetime 本身：SQLite 不存时区，DateTime(timezone=True)
    # 回来是 naive，与写入的 aware 值直接相等会失败（Postgres 上不会）。
    # 这里要验的是「起点没被刷新」，与时区表示无关。
    assert quota.period_started_at.replace(tzinfo=None) == started.replace(tzinfo=None)


async def test_留痕区分新建与更新(
    controller: BudgetController, audit_repo: FakeAuditRepository
) -> None:
    await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY, actor="U1")
    await controller.configure_channel_quota(CH, 2000, BudgetPeriod.WEEKLY, actor="U2")

    events = [log.detail.get("event") for log in audit_repo.logs]
    assert events == ["create", "update"]
    assert audit_repo.logs[0].user_id == "U1"
    assert audit_repo.logs[1].detail["limit"] == 2000
    assert audit_repo.logs[1].detail["period"] == "WEEKLY"


async def test_频道间互不干扰(controller: BudgetController, repo: SQLBudgetRepository) -> None:
    await controller.configure_channel_quota(CH, 1000, BudgetPeriod.MONTHLY)
    await controller.configure_channel_quota("ch_2", 5000, BudgetPeriod.DAILY)
    await controller.configure_channel_quota(CH, 2000, BudgetPeriod.MONTHLY)

    a = await repo.get_for_channel(CH)
    b = await repo.get_for_channel("ch_2")
    assert a is not None and b is not None
    assert (a.token_limit, b.token_limit) == (2000, 5000)
