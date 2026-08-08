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
        long_task_threshold: int = 3,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._queue = queue
        self._long_task_threshold = long_task_threshold

    async def create_task(
        self,
        channel_instance_id: str,
        thread_ref: str,
        requester_id: str,
        intent: str,
        *,
        tag_name: str | None = None,
        model_level: str = "light",
        async_execution: bool = False,
        prompt: str = "",
    ) -> Task:
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
        if async_execution:
            await self._enqueue(task, model_level, prompt)
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

    async def _enqueue(self, task: Task, model_level: str, prompt: str = "") -> None:
        payload = QueuePayload(
            task_id=task.id,
            channel_instance_id=task.channel_instance_id,
            model_level=model_level,
            prompt=prompt,
            tag_name=task.tag_name,
            thread_ref=task.thread_ref,
        )
        await self._queue.enqueue(payload)
