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
        """按固定间隔注册。

        coro 必须是协程函数本身或 functools.partial 包装 —— 不能是返回协程的
        lambda：APScheduler 用 iscoroutinefunction_partial 决定该 await 还是丢
        线程池，它会拆开 partial，但对 lambda 只看到普通函数，于是拿到一个从未
        被 await 的协程对象就丢掉，job 体静默不执行。
        """
        self._scheduler.add_job(
            coro,
            trigger="interval",
            minutes=minutes,
            id=job_id,
            replace_existing=True,
            # APScheduler 默认 misfire_grace_time=1s：job 到点时若事件循环正忙
            # 超过 1 秒，这次就被直接丢弃而非迟跑。worker 的循环里跑着 Agent，
            # 卡住一两秒是常态，实测日志里已出现 "Run time of job was missed"。
            # 对周期性巡检而言迟跑无害、被丢才有害，故把宽容期放到整个间隔。
            misfire_grace_time=int(minutes * 60),
            # 停机期间堆积的多次触发合并为一次：这些 job 都是「扫一遍当前状态」
            # 的幂等操作，补跑 N 次与跑 1 次结果相同，只是白耗时间。
            coalesce=True,
        )

    def remove(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)


# 全局调度器实例
scheduler = Scheduler()


async def run_in_loop(coro: Awaitable[Any]) -> None:
    """供后台线程/worker 在事件循环中执行异步协程。"""
    await coro
