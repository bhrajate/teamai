"""CheckpointRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.approval import ApprovalRecord, PendingApproval
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.infrastructure.orm.checkpoint import TaskCheckpointModel


def _dump_pending(p: PendingApproval) -> str:
    """PendingApproval → JSON 字符串。

    手写而非 dataclasses.asdict：datetime 不是 JSON 原生类型，asdict 出来的
    嵌套 dict 还要再遍历一遍改它，不如直接写清楚。
    """
    return json.dumps(
        {
            "tool_call_id": p.tool_call_id,
            "tool_name": p.tool_name,
            "args": p.args,
            "required": p.required,
            "approvals": [
                {
                    "user_id": a.user_id,
                    "at": a.at.isoformat(),
                    "override_args": a.override_args,
                }
                for a in p.approvals
            ],
            "created_at": p.created_at.isoformat(),
        },
        ensure_ascii=False,
    )


def _load_pending(raw: str) -> PendingApproval:
    data = json.loads(raw)
    return PendingApproval(
        tool_call_id=data["tool_call_id"],
        tool_name=data["tool_name"],
        args=data.get("args") or {},
        # int() 兜一层：库里若因手工改动成了字符串，required 静默变 0 会让
        # satisfied 恒为 True —— 工具直接放行，审批形同虚设。
        required=int(data.get("required", 1)),
        approvals=[
            ApprovalRecord(
                user_id=a["user_id"],
                at=datetime.fromisoformat(a["at"]),
                override_args=a.get("override_args"),
            )
            for a in data.get("approvals", [])
        ],
        created_at=datetime.fromisoformat(data["created_at"]),
    )


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

    # ---- 工具审批 ----

    async def set_pending_approval(
        self, task_id: str, messages: bytes, pending: PendingApproval
    ) -> None:
        """记下待批项 + 当前历史。行不存在时一并建出来。

        待批可能发生在**第一轮**工具调用上 —— 那时还没落过任何检查点，故这里
        必须能建新行，不能假定行已存在。
        """
        now = datetime.now(UTC)
        blob = _dump_pending(pending)
        stmt = select(TaskCheckpointModel).where(TaskCheckpointModel.task_id == task_id)
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is None:
            self._session.add(
                TaskCheckpointModel(
                    task_id=task_id,
                    messages=messages,
                    tokens_used=0,
                    attempts=0,
                    pending_approval=blob,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.messages = messages
            existing.pending_approval = blob
            existing.updated_at = now
            # tokens_used 与 attempts 不动：前者由检查点回调维护，后者是续跑
            # 计数。审批不是续跑，不该让它影响那两个。
        await self._session.flush()

    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        stmt = select(TaskCheckpointModel.pending_approval).where(
            TaskCheckpointModel.task_id == task_id
        )
        raw = (await self._session.execute(stmt)).scalars().first()
        return _load_pending(raw) if raw else None

    async def clear_pending_approval(self, task_id: str) -> None:
        """只清待批项，**留着 messages** —— 恢复执行正要用它。"""
        await self._session.execute(
            update(TaskCheckpointModel)
            .where(TaskCheckpointModel.task_id == task_id)
            .values(pending_approval=None, updated_at=datetime.now(UTC))
        )
        await self._session.flush()

    async def list_pending_before(self, cutoff: datetime) -> list[str]:
        """在 cutoff 之前就开始等审批的 task_id。

        按 updated_at 筛而非 created_at：created_at 是「检查点首次出现」，
        而待批可能发生在任务跑了很久之后 —— 用它会把刚开始等的任务也判超时。
        updated_at 在 set_pending_approval 时被刷新，正是「开始等」的时刻。
        """
        stmt = select(TaskCheckpointModel.task_id).where(
            TaskCheckpointModel.pending_approval.is_not(None),
            TaskCheckpointModel.updated_at < cutoff,
        )
        return list((await self._session.execute(stmt)).scalars().all())
