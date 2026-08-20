"""Agent 检查点仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.checkpoint import TaskCheckpoint


class CheckpointRepository(ABC):
    @abstractmethod
    async def get(self, task_id: str) -> TaskCheckpoint | None:
        """取某任务的最新检查点。无则 None（首次执行，或纯文本任务从未落过）。"""
        ...

    @abstractmethod
    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        """覆盖写检查点。**保留 attempts 与 created_at** —— 它们记录的是这个
        任务被续跑过几次、检查点首次出现在何时，与本次写入的内容无关。

        不收整个 TaskCheckpoint 而只收三个值：调用方（gateway 的回调）手上
        没有 attempts，让它构造完整对象就得先读一次，白多一趟往返。
        """
        ...

    @abstractmethod
    async def delete(self, task_id: str) -> None:
        """删检查点。任务进终态时由 orchestrator 在同一事务内调用。

        不存在时静默返回 —— 大多数任务（纯文本、单轮）从未落过检查点，
        而终态迁移对它们同样会走到这里。
        """
        ...

    @abstractmethod
    async def bump_attempts(self, task_id: str) -> int:
        """续跑计数 +1，返回自增后的值。

        用一条 UPDATE 原子自增而非「读-改-写」：巡检可能与别的写入并发，
        读改写会丢计数，而丢计数意味着一个反复崩溃的任务可以无限续跑。
        """
        ...
