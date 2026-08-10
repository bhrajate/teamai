"""预算控制：配额核算、上限触发 PAUSED 与通知、计费周期重置。"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.domain.models import AuditAction, AuditResult, BudgetQuota, BudgetState
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

    async def set_quota(self, quota: BudgetQuota) -> None:
        await self._repo.upsert(quota)
        await self._audit.record(
            quota.channel_instance_id or "",
            AuditAction.BUDGET_CHANGE,
            detail={"event": "set", "limit": quota.token_limit, "scope": quota.scope.value},
        )

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
