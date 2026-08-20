"""任务编排：创建、状态推进、取消、长任务入队、超时巡检。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from teamai.domain.identity import gen_id
from teamai.domain.models import AuditAction, Task, TaskStatus
from teamai.domain.ports import QueuePayload, TaskQueue
from teamai.domain.repositories import CheckpointRepository, TaskRepository
from teamai.domain.services import AuditLogWriter

logger = logging.getLogger(__name__)

# 超时巡检推进状态时的 actor。取固定串而非某个真实用户：审计里要能一眼
# 区分「人取消的」与「系统判超时的」。
_SWEEPER_ACTOR = "system:timeout-sweeper"

# 终态。进入其中任一状态即清理该任务的执行检查点（见 transition）。
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SweepReport:
    """超时巡检的结果。

    分开记成功与失败，而不是只返回处理成功的那些：巡检对每个任务单独兜异常，
    若失败只写日志不进返回值，调用方就无法区分「没有卡住的任务」和「找到了但
    全都没推进成功」—— 后者是故障，前者是正常。

    `resumed` 与 `swept` 也必须分开：现在有三种结局（续跑 / 收敛失败 / 推进
    失败），合成一个的话「10 个任务全部续跑了」与「10 个任务全部被判死」在
    调用方眼里是同一个结果，而前者是正常自愈、后者是需要人看的故障。
    """

    swept: list[Task] = field(default_factory=list)
    # 有检查点、已重新入队续跑的任务。它们仍是 RUNNING，没有状态迁移。
    resumed: list[Task] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (task_id, 错因)

    @property
    def ok(self) -> bool:
        return not self.failed


class TaskOrchestrator:
    def __init__(
        self,
        repo: TaskRepository,
        audit: AuditLogWriter,
        queue: TaskQueue,
        checkpoints: CheckpointRepository | None = None,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._queue = queue
        self._checkpoints = checkpoints

    async def create_task(
        self,
        channel_instance_id: str,
        thread_ref: str,
        requester_id: str,
        intent: str,
        *,
        tag_name: str | None = None,
        model_level: str = "light",
    ) -> Task:
        """建任务并落库。不入队 —— 是否转异步由调用方决定，见 enqueue。"""
        task = Task(
            id=gen_id("task"),
            channel_instance_id=channel_instance_id,
            thread_ref=thread_ref,
            requester_id=requester_id,
            intent=intent,
            tag_name=tag_name,
            model_level=model_level,
        )
        await self._repo.create(task)
        await self._audit.record(
            channel_instance_id,
            AuditAction.TASK_CREATE,
            user_id=requester_id,
            task_id=task.id,
            detail={"intent": intent, "tag": tag_name},
        )
        return task

    async def transition(self, task: Task, to: TaskStatus, actor: str) -> Task:
        task.transition(to, actor)
        # 进终态即清检查点，与状态更新同一事务。
        #
        # 放在这里而不是 AgentRuntime：**所有**终态迁移都经过本方法（含巡检判
        # 超时、用户取消），放 runtime 会漏掉那两条路径，留下「任务已终结、检查
        # 点还在」的孤儿 —— 而巡检看到有检查点就会去续跑一个已经结束的任务。
        #
        # PAUSED 不在终态里，故预算耗尽时检查点保留 —— 追加配额后从断点续跑。
        if to in _TERMINAL_STATUSES and self._checkpoints is not None:
            await self._checkpoints.delete(task.id)
        await self._repo.update(task)
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.TASK_TRANSITION,
            user_id=actor,
            task_id=task.id,
            detail={"to": to.value},
        )
        return task

    async def get(self, task_id: str) -> Task | None:
        return await self._repo.get(task_id)

    async def list(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]:
        return await self._repo.list_by_channel(channel_instance_id, status)

    async def cancel(self, task: Task, actor: str) -> Task:
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise ValueError(f"任务已处于终态 {task.status.value}，无法取消")
        return await self.transition(task, TaskStatus.CANCELLED, actor)

    async def _try_resume(self, task: Task, max_attempts: int) -> bool:
        """有检查点且未超上限时重新入队续跑。返回是否已接手。

        绕开「队列无 ack」的关键：检查点里含完整消息历史，而历史的第一条就是
        原始 user prompt —— 载荷可以纯从 DB 重建，Redis 里那条被 BLPOP 删掉的
        消息不再重要。
        """
        if self._checkpoints is None:
            return False
        checkpoint = await self._checkpoints.get(task.id)
        if checkpoint is None or checkpoint.attempts >= max_attempts:
            return False

        attempts = await self._checkpoints.bump_attempts(task.id)
        # prompt 传空串：原始提问已在检查点里，worker 侧的
        # `payload.prompt or task.intent` 兜底正好接住。
        await self.enqueue(task, "")
        # ⚠️ 状态留在 RUNNING —— 状态机没有 RUNNING→RUNNING 自环，走 transition()
        # 会抛 InvalidTransition。**不要**为此加自环：那会让「取消 → 重投」这类
        # 非法路径变得可能，且引入一个观测不到的中间态。
        #
        # ⚠️ 但必须刷新 updated_at，否则下一轮巡检立刻又把它捞出来 —— 表现是
        # 同一任务被无限续跑，且不报任何错。
        task.updated_at = _utcnow()
        await self._repo.update(task)
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.TASK_TRANSITION,
            user_id=_SWEEPER_ACTOR,
            task_id=task.id,
            detail={"to": "RUNNING", "event": "resumed", "attempts": attempts},
        )
        logger.info(f"任务 {task.id} 从检查点续跑（第 {attempts} 次）")
        return True

    async def sweep_stale_tasks(
        self,
        pending_timeout: timedelta,
        running_timeout: timedelta,
        now: datetime | None = None,
        max_resume_attempts: int = 0,
    ) -> SweepReport:
        """把卡死的任务收敛到 FAILED。

        两类卡死分别给阈值：PENDING 是「入队了没人取」（队列正常时秒级出队，
        久了说明 worker 全挂或载荷坏了）；RUNNING 是「取走了没结果」（长任务
        本就是小时/天级，故阈值宽得多，超过更可能是 worker 崩在半路）。

        没有这道巡检，worker 崩溃时正在执行的任务会永久停在 RUNNING：既不
        重投也不失败，发起人等不到任何回复，Admin 里也看不出它已经死了。

        单条失败不打断整轮：一个任务的状态机异常不该让其余任务继续挂着。但失败
        必须出现在返回值里 —— 只 log 不上报的话，「一个都没卡住」与「找到 5 个
        全都推进失败」对调用方是同一个结果。这不是假想：曾因两个定时任务共用
        一个 AsyncSession，巡检的每次 transition 都撞 InterfaceError，而本方法
        返回空列表、job 照报成功，故障整段隐形。
        """
        moment = now or _utcnow()
        report = SweepReport()
        for statuses, timeout, resumable in (
            # PENDING 不参与续跑：它还没开始执行，没有检查点可言。
            ((TaskStatus.PENDING,), pending_timeout, False),
            ((TaskStatus.RUNNING,), running_timeout, True),
        ):
            for task in await self._repo.list_stale(statuses, moment - timeout):
                try:
                    if (
                        resumable
                        and max_resume_attempts > 0
                        and await self._try_resume(task, max_resume_attempts)
                    ):
                        report.resumed.append(task)
                        continue
                    await self.transition(task, TaskStatus.FAILED, actor=_SWEEPER_ACTOR)
                except Exception as exc:
                    logger.warning(f"超时巡检推进失败 {task.id}: {exc}")
                    report.failed.append((task.id, str(exc)))
                    continue
                report.swept.append(task)
        return report

    async def enqueue(self, task: Task, prompt: str = "") -> None:
        """把已落库的任务投进长任务队列，交 worker 执行。

        与 create_task 分开两步、而非做成它的一个 async_execution 开关：
        调用方要的是「先建任务、再尝试入队、入队失败则就地同步执行」。若在
        create_task 内部入队，异常会在 return task 之前抛出，调用方手里既没
        task 也没 id，想降级只能重建一个，白留一条孤儿 PENDING 记录。

        prompt 单独传：tasks 表只存 intent 不存原文，worker 要靠载荷拿指令。
        model_level 则取自 task —— 两者本就该一致，分开传就意味着可以不一致。
        """
        payload = QueuePayload(
            task_id=task.id,
            channel_instance_id=task.channel_instance_id,
            model_level=task.model_level,
            prompt=prompt,
            tag_name=task.tag_name,
            thread_ref=task.thread_ref,
        )
        await self._queue.enqueue(payload)
