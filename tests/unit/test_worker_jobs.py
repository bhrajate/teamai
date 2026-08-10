"""worker 的定时任务注册。

register_jobs 曾长期是个空函数：scheduler 起了却没挂任何 job。这里既验证两个
job 真被注册，也钉住那个静默失效的坑 —— 传 lambda 而非 functools.partial 时，
APScheduler 会把它丢进线程池、拿到一个从未被 await 的协程对象就丢掉，job 体
不执行且不报错。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from apscheduler.util import iscoroutinefunction_partial

import app.worker.main as W
from teamai.domain.models import Task, TaskStatus
from teamai.infrastructure.scheduler import Scheduler

JOB_IDS = {"budget-period-reset", "stale-task-sweep"}


class FakeBudget:
    def __init__(self, count: int = 0, raises: Exception | None = None) -> None:
        self.count = count
        self.raises = raises
        self.calls = 0

    async def reset_expired_periods(self, now: object = None) -> int:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.count


class FakeOrchestrator:
    def __init__(self, swept: list[Task] | None = None, raises: Exception | None = None) -> None:
        self.swept = swept or []
        self.raises = raises
        self.calls: list[tuple[timedelta, timedelta]] = []

    async def sweep_stale_tasks(
        self, pending_timeout: timedelta, running_timeout: timedelta, now: object = None
    ) -> list[Task]:
        self.calls.append((pending_timeout, running_timeout))
        if self.raises is not None:
            raise self.raises
        return self.swept


def _container(budget: FakeBudget | None = None, orch: FakeOrchestrator | None = None) -> SimpleNamespace:
    return SimpleNamespace(budget=budget or FakeBudget(), orchestrator=orch or FakeOrchestrator())


def _patch_job_scope(monkeypatch: pytest.MonkeyPatch, budget: FakeBudget, orch: FakeOrchestrator) -> None:
    """替掉 open_job_scope，让 job 拿到替身而非真去开 DB session。

    真实现每次运行开一个独立 session（见 container.open_job_scope），单测里不连库，
    故整体替换这个上下文管理器。
    """

    @asynccontextmanager
    async def fake_scope(container: object):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(budget=budget, orchestrator=orch)

    monkeypatch.setattr(W, "open_job_scope", fake_scope)


@pytest.fixture
def fresh_scheduler(monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    """给每个测试一个全新 Scheduler，替掉模块级那个全局单例。

    不复用全局实例：APScheduler 在 start() 之前把 job 堆在 _pending_jobs 里，
    replace_existing 要等启动才生效，且 Scheduler.remove 走的 get_job 对 pending
    项的清理并不可靠 —— 共用一个实例会让这些测试互相污染、结果依赖执行顺序。
    """
    sched = Scheduler()
    monkeypatch.setattr(W, "scheduler", sched)
    return sched


def _jobs(sched: Scheduler) -> list:  # type: ignore[type-arg]
    return [j for j in sched._scheduler.get_jobs() if j.id in JOB_IDS]


# ===== 注册 =====


def test_注册了两个定时任务(fresh_scheduler: Scheduler) -> None:
    W.register_jobs(_container())

    registered = {j.id for j in _jobs(fresh_scheduler)}
    assert registered == JOB_IDS, f"定时任务与预期不符: {registered}"


def test_注册的job必须是可被await的形态(fresh_scheduler: Scheduler) -> None:
    """这条锁住 partial vs lambda。

    APScheduler 用 iscoroutinefunction_partial 决定该 await 还是丢线程池：它会
    拆开 partial 看到协程函数（True），但对 `lambda: job(container)` 只看到普通
    函数（False），于是把它丢进线程池、拿到一个从未被 await 的协程对象就丢掉，
    job 体静默不执行、连报错都没有。若有人把 partial 改回 lambda，这里会红。
    """
    W.register_jobs(_container())

    for job in _jobs(fresh_scheduler):
        assert iscoroutinefunction_partial(job.func), (
            f"{job.id} 的 func 不会被 await —— 须用 functools.partial 而非 lambda"
        )


def test_按配置的间隔注册(fresh_scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(W.settings, "jobs_sweep_interval_minutes", 7)

    W.register_jobs(_container())

    intervals = {j.trigger.interval for j in _jobs(fresh_scheduler)}
    assert intervals == {timedelta(minutes=7)}


async def test_启动后恰好两个job且可重复注册(fresh_scheduler: Scheduler) -> None:
    """按 _main() 的真实顺序验证：register_jobs 之后才 start()。

    replace_existing 只在 job 真正进 jobstore 时才生效（start 之前都堆在
    _pending_jobs），故「幂等」这件事必须在启动后才能断言。
    """
    W.register_jobs(_container())
    W.register_jobs(_container())  # 模拟重复调用

    fresh_scheduler.start()
    try:
        ids = sorted(j.id for j in _jobs(fresh_scheduler))
        assert ids == sorted(JOB_IDS), f"启动后 job 有重复或缺失: {ids}"
    finally:
        fresh_scheduler.shutdown()


# ===== job 体行为 =====


async def test_预算重置job调用控制器(monkeypatch: pytest.MonkeyPatch) -> None:
    budget, orch = FakeBudget(count=3), FakeOrchestrator()
    _patch_job_scope(monkeypatch, budget, orch)

    await W.reset_budget_periods(_container())

    assert budget.calls == 1


async def test_预算重置job吞掉异常(monkeypatch: pytest.MonkeyPatch) -> None:
    """定时任务抛异常没人接，只会污染 scheduler 日志，故在 job 内兜住。"""
    budget = FakeBudget(raises=RuntimeError("数据库炸了（测试注入）"))
    _patch_job_scope(monkeypatch, budget, FakeOrchestrator())

    await W.reset_budget_periods(_container())  # 不该抛

    assert budget.calls == 1


async def test_巡检job传入配置的阈值(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(W.settings, "jobs_pending_timeout_minutes", 15)
    monkeypatch.setattr(W.settings, "jobs_running_timeout_minutes", 600)
    orch = FakeOrchestrator()
    _patch_job_scope(monkeypatch, FakeBudget(), orch)

    await W.sweep_stale_tasks(_container())

    assert orch.calls == [(timedelta(minutes=15), timedelta(minutes=600))]


async def test_巡检job吞掉异常(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = FakeOrchestrator(raises=RuntimeError("查询炸了（测试注入）"))
    _patch_job_scope(monkeypatch, FakeBudget(), orch)

    await W.sweep_stale_tasks(_container())  # 不该抛

    assert len(orch.calls) == 1


async def test_巡检job记录被收掉的任务(monkeypatch: pytest.MonkeyPatch) -> None:
    task = Task(
        id="task_x",
        channel_instance_id="ch1",
        thread_ref="ts1",
        requester_id="U1",
        intent="bugfix",
    )
    task.status = TaskStatus.FAILED
    orch = FakeOrchestrator(swept=[task])
    _patch_job_scope(monkeypatch, FakeBudget(), orch)

    await W.sweep_stale_tasks(_container())

    assert len(orch.calls) == 1


async def test_两个job各自独立scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """同间隔的两个 job 会同时触发，各自必须开自己的 session。

    共用容器那个共享 session 时，SQLAlchemy 抛
    「concurrent operations are not permitted」/「This transaction is closed」——
    实测过：写库成功但紧随的审计写入失败，留下没有留痕的状态变更。
    """
    opened = 0

    @asynccontextmanager
    async def counting_scope(container: object):  # type: ignore[no-untyped-def]
        nonlocal opened
        opened += 1
        yield SimpleNamespace(budget=FakeBudget(), orchestrator=FakeOrchestrator())

    monkeypatch.setattr(W, "open_job_scope", counting_scope)

    c = _container()
    await asyncio.gather(W.reset_budget_periods(c), W.sweep_stale_tasks(c))

    assert opened == 2, "两个 job 必须各开一个 scope，不能共用"
