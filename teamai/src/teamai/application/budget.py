"""预算控制：配额核算、上限触发 PAUSED 与通知。"""

from __future__ import annotations

from teamai.domain.audit import AuditAction, AuditResult
from teamai.domain.budget import BudgetQuota, BudgetState
from teamai.infrastructure.audit_log import AuditLogWriter
from teamai.infrastructure.repositories.interface import BudgetRepository


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
