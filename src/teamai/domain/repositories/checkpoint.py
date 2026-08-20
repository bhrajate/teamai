"""Agent 检查点仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from teamai.domain.models.approval import PendingApproval
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

    # ---- 工具审批 ----
    #
    # 与检查点同一张表：两者都是同一任务的执行期状态，主键都是 task_id，
    # 终态时一起清。理由见 orm/checkpoint.py。

    @abstractmethod
    async def set_pending_approval(
        self, task_id: str, messages: bytes, pending: PendingApproval
    ) -> None:
        """记下待批的工具调用，连同当前消息历史。

        ``messages`` 一并写：恢复执行要靠它 —— 审批通过后是「拿这段历史 +
        批准结果继续跑」，而不是从头重跑。

        与 :meth:`upsert` 分开而非加一个可选参数：那个是「跑到干净边界了」，
        这个是「卡住等人」，两者对任务状态的含义相反（前者 RUNNING 继续，
        后者转 WAITING_INPUT）。合成一个方法会让调用点必须先判断自己在哪种
        情形，而判断依据只有传不传那个参数 —— 等于把语义藏进参数的有无里。
        """
        ...

    @abstractmethod
    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        """取待批项。None 表示该任务没有在等审批。"""
        ...

    @abstractmethod
    async def clear_pending_approval(self, task_id: str) -> None:
        """清掉待批项，保留检查点本身。

        审批有结果（批准/拒绝/超时）后调用：待批状态结束，但消息历史仍要留着 ——
        恢复执行正要用它，且此后崩溃仍可续跑。
        """
        ...

    @abstractmethod
    async def list_pending_before(self, cutoff: datetime) -> list[str]:
        """返回在 ``cutoff`` 之前就开始等审批的 task_id。

        供超时巡检用。只返回 id 不返回完整对象：巡检拿到 id 后要连任务一起取，
        带回完整载荷等于白读一遍那些 blob（每个可能几十 KB）。
        """
        ...
