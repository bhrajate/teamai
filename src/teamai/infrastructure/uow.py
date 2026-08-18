"""UnitOfWork 的 SQLAlchemy 实现。

包着一个 `AsyncSession`。仓储与本类必须共用**同一个** session —— 组合根
(`container.py`)负责保证这一点:一个 scope 里建一次 session,同时喂给所有
仓储和这个工作单元。喂错(各自建 session)不会报错,只会让事务边界静默失效,
故 `SQLUnitOfWork` 刻意不自己创建 session,只接收。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.ports.uow import UnitOfWork

logger = logging.getLogger(__name__)


class SQLUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__()
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """暴露给需要直接跑语句的地方(如 projector 的抢占 UPDATE)。

        不是给仓储用的 —— 仓储在构造时就拿到了同一个 session。
        """
        return self._session

    async def _do_commit(self) -> None:
        await self._session.commit()

    async def _do_rollback(self) -> None:
        try:
            await self._session.rollback()
        except Exception as exc:  # pragma: no cover - 连接已断时 rollback 也会失败
            # 回滚失败只告警:此时事务在服务端已因连接问题终止,数据不会半落。
            # 抛出去会掩盖触发回滚的那个原始异常,而后者才是要排查的。
            logger.warning(f"事务回滚失败: {exc}")


class NullUnitOfWork(UnitOfWork):
    """不做任何事的工作单元。

    给单测与「确实无需事务」的装配用(例如只读路径)。存在这个实现是为了让
    服务层可以无条件 `async with self._uow`,不必到处判 None —— 那种判断散开
    之后必然漏掉一处,而漏掉的地方就是一个静默的事务缺口。
    """

    async def _do_commit(self) -> None:
        return None

    async def _do_rollback(self) -> None:
        return None
