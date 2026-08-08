"""TaskRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.task import Task, TaskStatus
from teamai.domain.repositories.task import TaskRepository
from teamai.infrastructure.orm.task import TaskModel


def _task_to_model(task: Task) -> TaskModel:
    return TaskModel(
        id=task.id,
        channel_instance_id=task.channel_instance_id,
        thread_ref=task.thread_ref,
        requester_id=task.requester_id,
        intent=task.intent,
        tag_name=task.tag_name,
        model_level=task.model_level,
        status=task.status,
        current_stage=task.current_stage,
        owner_id=task.owner_id,
        canceled_by=task.canceled_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _model_to_task(m: TaskModel) -> Task:
    return Task(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        thread_ref=m.thread_ref,
        requester_id=m.requester_id,
        intent=m.intent,
        tag_name=m.tag_name,
        model_level=m.model_level,
        status=m.status,
        current_stage=m.current_stage,
        owner_id=m.owner_id,
        canceled_by=m.canceled_by,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SQLTaskRepository(TaskRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, task: Task) -> None:
        self._session.add(_task_to_model(task))

    async def update(self, task: Task) -> None:
        await self._session.merge(_task_to_model(task))

    async def get(self, task_id: str) -> Task | None:
        m = await self._session.get(TaskModel, task_id)
        return _model_to_task(m) if m else None

    async def list_by_channel(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]:
        stmt = select(TaskModel).where(TaskModel.channel_instance_id == channel_instance_id)
        if status is not None:
            stmt = stmt.where(TaskModel.status == status)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_task(r) for r in rows]
