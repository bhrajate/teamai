"""预算计费周期重置。

没有这条链路，任一频道耗尽配额后就永久 EXHAUSTED —— BudgetQuota.period
字段形同虚设，管理员只能手工改额度才能恢复。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from teamai.application.budget import BudgetController
from teamai.domain.models import (
    AuditAction,
    BudgetPeriod,
    BudgetQuota,
    BudgetScope,
    BudgetState,
)
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeBudgetRepository

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _quota(
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    *,
    started: datetime = T0,
    used: int = 500,
    state: BudgetState = BudgetState.ACTIVE,
    qid: str = "bq_1",
    channel: str = "ch1",
) -> BudgetQuota:
    return BudgetQuota(
        id=qid,
        scope=BudgetScope.CHANNEL,
        token_limit=1000,
        period=period,
        used_tokens=used,
        channel_instance_id=channel,
        state=state,
        period_started_at=started,
    )


# ===== 领域层判据 =====


@pytest.mark.parametrize(
    "period,days",
    [(BudgetPeriod.DAILY, 1), (BudgetPeriod.WEEKLY, 7), (BudgetPeriod.MONTHLY, 30)],
)
def test_周期长度(period: BudgetPeriod, days: int) -> None:
    assert _quota(period).period_length == timedelta(days=days)


@pytest.mark.parametrize(
    "period,days",
    [(BudgetPeriod.DAILY, 1), (BudgetPeriod.WEEKLY, 7), (BudgetPeriod.MONTHLY, 30)],
)
def test_周期走满即该重置(period: BudgetPeriod, days: int) -> None:
    q = _quota(period)
    assert q.should_reset(T0 + timedelta(days=days)) is True
    assert q.should_reset(T0 + timedelta(days=days, seconds=1)) is True


@pytest.mark.parametrize(
    "period,days",
    [(BudgetPeriod.DAILY, 1), (BudgetPeriod.WEEKLY, 7), (BudgetPeriod.MONTHLY, 30)],
)
def test_周期未满不该重置(period: BudgetPeriod, days: int) -> None:
    q = _quota(period)
    assert q.should_reset(T0) is False
    assert q.should_reset(T0 + timedelta(days=days, seconds=-1)) is False


def test_重置清零并恢复ACTIVE() -> None:
    q = _quota(used=1000, state=BudgetState.EXHAUSTED)
    later = T0 + timedelta(days=31)

    q.reset(later)

    assert q.used_tokens == 0
    assert q.state is BudgetState.ACTIVE
    assert q.period_started_at == later
    assert q.remaining == 1000


def test_重置起点取当下而非累加周期() -> None:
    """停机数个周期后不该连续补发多次重置 —— 用量早已清零，补发无意义。"""
    q = _quota(started=T0)
    much_later = T0 + timedelta(days=95)  # 跨了三个月周期

    q.reset(much_later)

    assert q.period_started_at == much_later
    assert q.should_reset(much_later) is False, "重置后立刻又该重置说明起点没推进"


def test_周期起点独立于updated_at() -> None:
    """消费会刷 updated_at，但不该顺带推迟周期 —— 否则一直在用的频道永不重置。"""
    q = _quota(started=T0, used=0)
    q.consume(100)

    assert q.period_started_at == T0, "consume 不该动周期起点"
    assert q.updated_at > T0


# ===== 用例层巡检 =====


@pytest.fixture
def audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


def _controller(repo: FakeBudgetRepository, audit_repo: FakeAuditRepository) -> BudgetController:
    return BudgetController(repo, AuditLogWriter(audit_repo))


async def test_巡检重置到期配额(audit_repo: FakeAuditRepository) -> None:
    expired = _quota(used=1000, state=BudgetState.EXHAUSTED)
    repo = FakeBudgetRepository(quotas=[expired])

    count = await _controller(repo, audit_repo).reset_expired_periods(T0 + timedelta(days=31))

    assert count == 1
    assert expired.used_tokens == 0
    assert expired.state is BudgetState.ACTIVE
    assert repo.upserts == 1


async def test_巡检跳过未到期配额(audit_repo: FakeAuditRepository) -> None:
    fresh = _quota(used=800)
    repo = FakeBudgetRepository(quotas=[fresh])

    count = await _controller(repo, audit_repo).reset_expired_periods(T0 + timedelta(days=10))

    assert count == 0
    assert fresh.used_tokens == 800, "未到期配额不该被清零"
    assert repo.upserts == 0


async def test_巡检只动到期的那些(audit_repo: FakeAuditRepository) -> None:
    expired = _quota(qid="bq_old", channel="ch1", started=T0, used=999)
    fresh = _quota(qid="bq_new", channel="ch2", started=T0 + timedelta(days=29), used=111)
    repo = FakeBudgetRepository(quotas=[expired, fresh])

    count = await _controller(repo, audit_repo).reset_expired_periods(T0 + timedelta(days=31))

    assert count == 1
    assert expired.used_tokens == 0
    assert fresh.used_tokens == 111


async def test_重置写审计留痕(audit_repo: FakeAuditRepository) -> None:
    """预算变更必须可追溯：事后要能区分「额度被清零」是重置还是人工改动。"""
    repo = FakeBudgetRepository(quotas=[_quota(used=1000, state=BudgetState.EXHAUSTED)])

    await _controller(repo, audit_repo).reset_expired_periods(T0 + timedelta(days=31))

    assert len(audit_repo.logs) == 1
    log = audit_repo.logs[0]
    assert log.action is AuditAction.BUDGET_CHANGE
    assert log.channel_instance_id == "ch1"
    assert log.detail["event"] == "period_reset"
    assert log.detail["used_before"] == 1000
    assert log.detail["state_before"] == "EXHAUSTED"


async def test_耗尽的配额重置后可继续消费(audit_repo: FakeAuditRepository) -> None:
    """端到端语义：这才是这条链路存在的理由。"""
    quota = _quota(used=1000, state=BudgetState.EXHAUSTED)
    repo = FakeBudgetRepository(quota=quota, quotas=[quota])
    controller = _controller(repo, audit_repo)

    assert await controller.check_quota("ch1") is False

    await controller.reset_expired_periods(T0 + timedelta(days=31))

    assert await controller.check_quota("ch1") is True
    assert await controller.remaining("ch1") == 1000


async def test_无配额时巡检空转(audit_repo: FakeAuditRepository) -> None:
    count = await _controller(FakeBudgetRepository(quotas=[]), audit_repo).reset_expired_periods(T0)
    assert count == 0
