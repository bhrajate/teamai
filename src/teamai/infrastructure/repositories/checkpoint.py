"""CheckpointRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.infrastructure.orm.checkpoint import TaskCheckpointModel


def _to_domain(m: TaskCheckpointModel) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id=m.task_id,
        messages=m.messages,
        tokens_used=m.tokens_used,
        attempts=m.attempts,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SQLCheckpointRepository(CheckpointRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        stmt = select(TaskCheckpointModel).where(TaskCheckpointModel.task_id == task_id)
        m = (await self._session.execute(stmt)).scalars().first()
        return _to_domain(m) if m else None

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        """覆盖写内容，保留 attempts 与 created_at。

        先查再决定 insert / update，而不是无脑 `merge` 一个新对象：merge 会把
        attempts 与 created_at 一并覆盖成默认值，于是每落一个检查点都把续跑
        计数清零 —— 一个反复崩溃的任务就能无限续跑，而 attempts 上限形同虚设。
        """
        now = datetime.now(UTC)
        stmt = select(TaskCheckpointModel).where(TaskCheckpointModel.task_id == task_id)
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is None:
            self._session.add(
                TaskCheckpointModel(
                    task_id=task_id,
                    messages=messages,
                    tokens_used=tokens_used,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.messages = messages
            existing.tokens_used = tokens_used
            existing.updated_at = now
        # 只 flush 不 commit：事务边界由用例层（UoW）声明，
        # 见 tests/unit/test_repository_commit.py 的约束说明。
        await self._session.flush()

    async def delete(self, task_id: str) -> None:
        await self._session.execute(
            delete(TaskCheckpointModel).where(TaskCheckpointModel.task_id == task_id)
        )
        await self._session.flush()

    async def bump_attempts(self, task_id: str) -> int:
        """一条 UPDATE 原子自增，返回自增后的值；无该行时返回 0。

        不用「读-改-写」：巡检可能与 gateway 的检查点写入并发，读改写会丢计数，
        而丢计数意味着反复崩溃的任务可以无限续跑。
        """
        stmt = (
            update(TaskCheckpointModel)
            .where(TaskCheckpointModel.task_id == task_id)
            .values(
                attempts=TaskCheckpointModel.attempts + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(TaskCheckpointModel.attempts)
        )
        attempts = (await self._session.execute(stmt)).scalars().first()
        await self._session.flush()
        return int(attempts or 0)
