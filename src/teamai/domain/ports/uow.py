"""工作单元(Unit of Work)端口:把一组仓储写入收进一个事务。

## 为什么需要它

改造前九个仓储共 15 处 `commit()`,每个方法各自提交。于是「写记忆」与「记下
该建向量的意图」是两次独立提交,中间崩溃就丢掉后者 —— 这正是 outbox 方案要
消除的那个窗口(见 `docs/plan-memory-outbox.md` §2 缺陷 1)。审计也一样:
`AuditLogWriter` 走独立 repo 独立 commit,「记忆写成功但审计没记上」可能发生,
而审计是排查「机器人为什么这么说」的第一手材料(缺陷 8)。

## 为什么必须可重入

`MemoryService.supersede()` 内部调 `self.store()`。若两者各开一个工作单元而
实现不可重入,内层退出时就会提交掉半个操作 —— 新条目已落库、旧条目还没标记
被取代,那一瞬间同一事实有两条并列。用引用计数让内层退出成为 no-op,只有最
外层真正提交:

    async with uow:                 # depth 0 → 1,开事务
        await memory.supersede(...) #   内部 async with uow:depth 1 → 2 → 1,不提交
    # depth 1 → 0,提交

这样每个服务方法都能独立声明自己的事务边界,不必知道自己是否被别人包着。

## 边界放在服务方法上,不放在 adapter

adapter 有四类入口(admin router、平台 router、worker job、scheduler),放在
那里要改四处且容易漏一处;放在服务方法上,`store` / `edit` / `supersede` /
`delete` 各自就是一个原子操作,语义与谁调用无关。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType


class UnitOfWork(ABC):
    """可重入的事务边界。

    子类只需实现 `_do_commit` / `_do_rollback`;引用计数与异常处理在本类,
    避免每个实现各写一遍并各错一遍。
    """

    def __init__(self) -> None:
        self._depth = 0

    @property
    def depth(self) -> int:
        """当前嵌套层数。0 表示不在事务中。测试与排查用。"""
        return self._depth

    async def __aenter__(self) -> UnitOfWork:
        self._depth += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth -= 1

        # 内层退出:什么都不做。提交与回滚都由最外层决定 —— 内层提交会撕开
        # 半个操作(见模块注释),内层回滚则会把外层已做的变更一起撤掉,而外层
        # 并不知情。
        if self._depth > 0:
            return

        if exc_type is not None:
            await self._do_rollback()
            return  # 不吞异常,照常向上抛
        await self._do_commit()

    async def commit(self) -> None:
        """显式提交。

        供确实需要在一个边界内分段落盘的场景用(例如批处理每 N 条提交一次)。
        普通写路径不该调它 —— 用 `async with` 让边界与代码块对齐更难出错。
        """
        await self._do_commit()

    async def rollback(self) -> None:
        """显式回滚。"""
        await self._do_rollback()

    @abstractmethod
    async def _do_commit(self) -> None: ...

    @abstractmethod
    async def _do_rollback(self) -> None: ...
