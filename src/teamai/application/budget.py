"""预算控制：配额核算、上限触发 PAUSED 与通知、计费周期重置。"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.domain.identity import gen_id
from teamai.domain.models import (
    AuditAction,
    AuditResult,
    BudgetPeriod,
    BudgetQuota,
    BudgetScope,
    BudgetState,
)
from teamai.domain.repositories import BudgetRepository
from teamai.domain.services import AuditLogWriter


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetController:
    def __init__(self, repo: BudgetRepository, audit: AuditLogWriter) -> None:
        self._repo = repo
        self._audit = audit

    async def check_quota(self, channel_instance_id: str) -> bool:
        quota = await self._repo.get_for_channel(channel_instance_id)
        if quota is None:
            return True  # 未配置配额视为不限
        return quota.state is BudgetState.ACTIVE and quota.remaining > 0

    async def remaining(self, channel_instance_id: str) -> int:
        quota = await self._repo.get_for_channel(channel_instance_id)
        if quota is None:
            return 10_000_000  # 未配置配额给足空间，仍受 UsageLimits 约束
        return quota.remaining

    async def consume(self, channel_instance_id: str, tokens: int) -> bool:
        quota = await self._repo.get_for_channel(channel_instance_id)
        if quota is None or tokens <= 0:
            return True
        ok = quota.consume(tokens)
        await self._repo.upsert(quota)
        if not ok:
            await self._audit.record(
                channel_instance_id,
                AuditAction.BUDGET_CHANGE,
                detail={"event": "exhausted", "used": quota.used_tokens, "limit": quota.token_limit},
                result=AuditResult.PAUSED,
            )
        return ok

    async def configure_channel_quota(
        self,
        channel_instance_id: str,
        token_limit: int,
        period: BudgetPeriod,
        *,
        actor: str | None = None,
    ) -> BudgetQuota:
        """设定频道配额：已有则原地改，没有才新建。

        ⚠️ 必须复用既有配额的 id。`BudgetRepository.upsert` 走 `session.merge`，
        按主键匹配，故传一个新 id 进去是 INSERT 而非 UPDATE；而
        `get_for_channel` 用 `.first()` 且无 ORDER BY —— 表里一旦有两行同频道
        配额，返回哪行取决于数据库，管理员调完上限很可能仍读回旧行，
        看起来「改了没生效」。`budget_quotas` 上没有 channel_instance_id
        唯一约束，拦不住这种重复。

        用量与周期起点一并保留：调上限是「改额度」，不该顺手把本周期的
        消耗记录抹掉。反过来，若新上限已高于当前用量，则把 EXHAUSTED 放回
        ACTIVE —— 管理员调高上限的意图正是让频道重新可用，否则还得等下个
        周期的定时重置。
        """
        existing = await self._repo.get_for_channel(channel_instance_id)

        if existing is None:
            quota = BudgetQuota(
                id=gen_id("bq"),
                scope=BudgetScope.CHANNEL,
                token_limit=token_limit,
                period=period,
                channel_instance_id=channel_instance_id,
            )
            event = "create"
        else:
            quota = existing
            quota.token_limit = token_limit
            quota.period = period
            quota.updated_at = _utcnow()
            if quota.state is BudgetState.EXHAUSTED and quota.remaining > 0:
                quota.state = BudgetState.ACTIVE
            event = "update"

        await self._repo.upsert(quota)
        await self._audit.record(
            channel_instance_id,
            AuditAction.BUDGET_CHANGE,
            user_id=actor,
            detail={
                "event": event,
                "limit": quota.token_limit,
                "period": quota.period.value,
                "scope": quota.scope.value,
            },
        )
        return quota

    async def get_quota(self, channel_instance_id: str) -> BudgetQuota | None:
        return await self._repo.get_for_channel(channel_instance_id)

    async def reset_expired_periods(self, now: datetime | None = None) -> int:
        """把已走完计费周期的配额清零并翻页，返回重置条数。

        由 worker 的定时任务驱动（见 app/worker/main.py 的 register_jobs）。
        没有这一步，任一频道一旦耗尽配额就永久 EXHAUSTED —— 预算的「周期」
        字段形同虚设。

        逐条 upsert 而非批量 UPDATE：条数是每频道一条的量级，且每条重置都要
        留一条审计。单条失败不影响其余，故不整体包事务。
        """
        moment = now or _utcnow()
        reset_count = 0
        for quota in await self._repo.list_all():
            if not quota.should_reset(moment):
                continue
            used_before, was = quota.used_tokens, quota.state
            quota.reset(moment)
            await self._repo.upsert(quota)
            await self._audit.record(
                quota.channel_instance_id or "",
                AuditAction.BUDGET_CHANGE,
                detail={
                    "event": "period_reset",
                    "period": quota.period.value,
                    "used_before": used_before,
                    "state_before": was.value,
                },
            )
            reset_count += 1
        return reset_count
