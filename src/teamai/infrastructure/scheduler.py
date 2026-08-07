"""APScheduler 封装：定时/周期任务。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


class Scheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def add_cron(self, job_id: str, cron_expr: str, coro: Callable[[], Awaitable[Any]]) -> None:
        """按 cron 表达式注册任务。coro 将在事件循环中执行。"""

        async def _wrapper() -> None:
            try:
                await coro()
            except Exception as exc:  # pragma: no cover
                self._scheduler.add_job(
                    lambda: None,
                    trigger="date",
                    next_run_time=None,
                )
                raise exc

        self._scheduler.add_job(
            _wrapper,
            trigger=CronTrigger.from_crontab(cron_expr),
            id=job_id,
            replace_existing=True,
        )

    def add_interval(self, job_id: str, minutes: int, coro: Callable[[], Awaitable[Any]]) -> None:
        self._scheduler.add_job(
            coro,
            trigger="interval",
            minutes=minutes,
            id=job_id,
            replace_existing=True,
        )

    def remove(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)


# 全局调度器实例
scheduler = Scheduler()


async def run_in_loop(coro: Awaitable[Any]) -> None:
    """供后台线程/worker 在事件循环中执行异步协程。"""
    await coro
