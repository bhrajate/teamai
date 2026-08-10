"""任务编排：创建、状态推进、取消、长任务入队。"""

from __future__ import annotations

from teamai.domain.identity import gen_id
from teamai.domain.models import AuditAction, Task, TaskStatus
from teamai.domain.ports import QueuePayload, TaskQueue
from teamai.domain.repositories import TaskRepository
from teamai.domain.services import AuditLogWriter


class TaskOrchestrator:
    def __init__(
        self,
        repo: TaskRepository,
        audit: AuditLogWriter,
        queue: TaskQueue,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._queue = queue

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
