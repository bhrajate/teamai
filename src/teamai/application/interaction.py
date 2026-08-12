"""Agent 交互记录服务：留痕与保留期清理。

补的是审计的一个缺口：`audit_logs` 记「发生了什么动作」（九个枚举 + 小字典），
不存实际组装出的提示词与模型响应。于是任务回答错了、越权调了工具、token 烧
超了，都无法还原当时的输入 —— 这个缺口比「没有聊天记录」严重得多，且它不是
靠镜像聊天记录能补上的（模型看到的是提示词，不是原始消息流）。

同时这张表是几件事的共同基础：按频道核算成本（含降级后的实际模型）、控制台
展示某任务的完整往返、以及端到端评测集的语料来源。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from teamai.domain.identity import gen_id
from teamai.domain.models import AgentInteraction, InteractionResult
from teamai.domain.repositories import InteractionRepository

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InteractionService:
    def __init__(self, repo: InteractionRepository, retention_days: int = 90) -> None:
        self._repo = repo
        self._retention_days = retention_days

    async def record(
        self,
        *,
        task_id: str,
        channel_instance_id: str,
        thread_ref: str,
        user_prompt: str,
        system_prompt: str,
        model_level: str,
        requester_id: str | None = None,
        model_id: str = "",
        response: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        result: InteractionResult = InteractionResult.DONE,
        error: str | None = None,
        context_refs: dict[str, object] | None = None,
    ) -> AgentInteraction | None:
        """写一条交互记录。

        返回 None 表示写入失败。**不外抛异常**：留痕失败不该让一个已经跑完的
        任务变成失败 —— 用户已经拿到回答，却因为审计写库出错而收到「任务执行
        失败」，是更糟的结果。失败记 error 级日志，由运维告警接住。
        """
        interaction = AgentInteraction(
            id=gen_id("itr"),
            task_id=task_id,
            channel_instance_id=channel_instance_id,
            thread_ref=thread_ref,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            model_level=model_level,
            requester_id=requester_id,
            model_id=model_id,
            response=response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            result=result,
            error=error,
            context_refs=context_refs or {},
        )
        try:
            await self._repo.record(interaction)
        except Exception as exc:
            logger.error(f"交互记录写入失败 task={task_id}: {exc}")
            return None
        return interaction

    async def list_by_channel(
        self, channel_instance_id: str, limit: int = 50
    ) -> list[AgentInteraction]:
        return await self._repo.list_by_channel(channel_instance_id, limit=limit)

    async def list_by_task(self, task_id: str) -> list[AgentInteraction]:
        return await self._repo.list_by_task(task_id)

    async def purge_expired(self, now: datetime | None = None) -> int:
        """按保留期清理，返回删除行数。由 worker 定时任务驱动。

        没有这一步，这张表（含提示词与响应全文）会无限增长 —— 既是存储负担，
        更是合规负担：保留期是对外承诺的一部分，不执行等于没承诺。
        retention_days <= 0 视为不清理，留给「合规要求永久留存」的部署。
        """
        if self._retention_days <= 0:
            return 0
        cutoff = (now or _utcnow()) - timedelta(days=self._retention_days)
        return await self._repo.purge_before(cutoff)
