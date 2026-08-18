"""记忆向量投影：消费 memory_outbox，把向量写进向量库。

由 worker 里的常驻循环驱动（不是定时任务）—— 「刚写入的记忆搜不到」在同一个
会话里就会被用户察觉：蒸馏刚提炼出一条结论，紧接着的提问就该能命中它。目标
lag 是 p99 < 5 秒（2 秒轮询 + 一次 embed 往返）。

完整设计见 `docs/plan-memory-outbox.md` §5.2 与 §5.4。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from teamai.domain.models import MemoryEntry, OutboxEntry, should_embed
from teamai.domain.ports import Embedder, MetricsSink, NullMetricsSink

logger = logging.getLogger(__name__)

# 退避上限。单次等待不超过它，免得一条坏记录把重试拖到几小时后。
MAX_BACKOFF_SECONDS = 300


def content_hash(content: str) -> str:
    """向量对应内容的指纹，写进 `MemoryEntry.embedded_hash`。

    ⚠️ 必须与对账 SQL 里的 `md5(content)` **逐字一致**，否则两边会互相拆台：
    一方判「hash 不符、该重算」不断入队，另一方判「已是最新」什么也不做，
    形成烧钱的死循环。

    这里按 UTF-8 编码，而 Postgres 的 `md5()` 按数据库编码算 —— 两者一致的前提
    是库是 UTF-8（本项目的 Postgres 镜像默认如此）。若哪天部署到非 UTF-8 的库，
    对账会把所有行判成「需重算」，表现是 reconcile 指标持续非零。
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()


@dataclass
class ProjectionReport:
    """一轮投影的结果。

    分开记四类而不是只报数字：「没有待处理记录」与「全部失败」在日志里长得一样，
    而后者是故障。与 SweepReport / DistillReport 的取舍一致。
    """

    upserted: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # 内容未变、向量已是最新，什么都没做
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (entry_id, 错因)

    @property
    def claimed(self) -> int:
        return len(self.upserted) + len(self.deleted) + len(self.skipped) + len(self.failed)


class MemoryProjector:
    """投影器。轮询循环在 `run_forever`，是本项目里投影的唯一驱动处。

    ⚠️ 收 `scope_factory` 而不是直接收仓储：**每轮投影必须换一个 session。**
    仓储绑在 session 上，若构造时就固定，`run_forever` 那个长期存活的循环就会
    让同一个 AsyncSession 活到进程退出 —— 而它不允许并发使用（投影循环与定时
    任务跑在同一个事件循环上），且长事务会让连接一直挂在池子里。

    `scope_factory()` 返回一个异步上下文管理器，yield 出的对象需有
    `outbox_repo` 与 `memory_repo` 两个属性（`container.JobScope` 就是）。
    vector_store 与 embedder 走 HTTP、与 session 无关，故构造时固定。
    """

    def __init__(
        self,
        scope_factory: Callable[[], AbstractAsyncContextManager[Any]],
        vector_store,
        embedder: Embedder,
        *,
        batch_size: int = 32,
        lease_seconds: int = 300,
        max_attempts: int = 11,
        poll_interval_seconds: float = 2.0,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._scope_factory = scope_factory
        self._vector = vector_store
        self._embedder = embedder
        # 无条件可调，不必到处判 None（散开的 None 判断必然漏掉一处，
        # 而漏掉的那处就是一个静默失效的埋点）
        self._metrics = metrics or NullMetricsSink()
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval_seconds
        # 持租者标识，只为排查「这批是谁领走的」。进程名+pid 足够区分同机多实例。
        self._who = f"{socket.gethostname()}:{os.getpid()}"[:64]

    async def run_once(self) -> ProjectionReport:
        """领一批、逐条投影。开一个 scope，返回本轮结果。"""
        report = ProjectionReport()

        if not self._embedder.available:
            # 未配置 embedding 凭据。不领取、不置死信 —— 记录留在队列里，凭据补上
            # 之后照常处理。lag 指标会把「语义检索实际关闭」这件事诚实地暴露出来，
            # 那比悄悄把它们判成失败好。
            return report

        async with self._scope_factory() as scope:
            outbox, repo = scope.outbox_repo, scope.memory_repo
            try:
                claimed = await outbox.claim(
                    limit=self._batch_size,
                    lease_seconds=self._lease_seconds,
                    claimed_by=self._who,
                )
            except Exception as exc:
                logger.error(f"领取 outbox 记录失败: {exc}")
                return report

            for record in claimed:
                try:
                    await self._project(record, report, outbox, repo)
                except Exception as exc:
                    # 单条失败不打断整批：一次 embedding 限流不该让其余记录跟着停。
                    logger.warning(f"投影记忆 {record.entry_id} 失败: {exc}")
                    report.failed.append((record.entry_id, str(exc)))
                    self._metrics.projected(op=record.op.value, result="failed")
                    await self._mark_failed(record, str(exc), outbox)

            # 队列状态在同一个 scope 里读，免得再开一次 session。放在处理之后：
            # 要报的是「还剩多少」，处理之前读到的是「本轮开始时剩多少」。
            try:
                stats = await outbox.stats()
                self._metrics.outbox_state(
                    pending=stats.pending, dead=stats.dead, lag_seconds=stats.lag_seconds
                )
            except Exception as exc:
                logger.debug(f"读取 outbox 统计失败: {exc}")
        return report

    async def _project(
        self, record: OutboxEntry, report: ProjectionReport, outbox, repo
    ) -> None:
        """投影一条记录。

        ⚠️ **只看 `memory_entries` 的当前状态，不看 `record.op`。** 按 op 行事会让
        滞后的 UPSERT 拿旧内容覆盖新向量 —— 而 edit / supersede 会让同一条记忆在
        短时间内变化多次，这种滞后是常态而非例外。op 只是可观测信息。

        决策表（docs/plan-memory-outbox.md §5.2）：

        | 回读结果 | 动作 |
        |---|---|
        | 行不存在 | 删向量 |
        | 存在但不该有向量（偏好 / 已被取代） | 删向量 |
        | 存在且应当有向量 | embed 当前 content → upsert → 回填 |
        """
        entry = await repo.get(record.entry_id)

        if entry is None or not should_embed(entry.type) or not entry.is_current:
            await self._drop(entry, record.entry_id, repo)
            report.deleted.append(record.entry_id)
            self._metrics.projected(op=record.op.value, result="deleted")
            await outbox.complete(record.id)
            return

        expected = content_hash(entry.content)
        if entry.embedding_ref is not None and entry.embedded_hash == expected:
            # 已是最新。连续 edit 会给同一条记忆入多条队，后处理的那些走到这里 ——
            # 不是错误，只是省掉一次多余的 embed 调用。
            report.skipped.append(entry.id)
            self._metrics.projected(op=record.op.value, result="skipped")
            await outbox.complete(record.id)
            return

        started = time.monotonic()
        embedding = await self._embedder.embed(entry.content)
        # 失败的调用也计时：限流时的耗时分布恰恰是要看的东西，只记成功会让
        # 直方图看起来一切正常。
        self._metrics.embed_duration(time.monotonic() - started)
        if not embedding:
            # embedder 把异常咽成空列表（读路径要那个语义），这里必须把它变回失败，
            # 否则这条记录会被当成投影成功而删除，向量永久缺失。
            raise RuntimeError("embedding 返回空向量")

        ref = await self._vector.upsert(entry, embedding)
        entry.embedding_ref = ref
        entry.embedded_hash = expected
        await repo.update(entry)
        report.upserted.append(entry.id)
        self._metrics.projected(op=record.op.value, result="upserted")
        await outbox.complete(record.id)

    async def _drop(self, entry: MemoryEntry | None, entry_id: str, repo) -> None:
        """删向量，并清掉记忆行上的标记（如果行还在）。

        清标记必须在删向量**成功之后**：反过来的话，删失败时库里说「没有向量」而
        实际还在，对账也就查不出来 —— 那正是改造前 `supersede` 的毛病。
        """
        await self._vector.delete(entry_id)
        if entry is not None and (entry.embedding_ref is not None or entry.embedded_hash is not None):
            entry.embedding_ref = None
            entry.embedded_hash = None
            await repo.update(entry)

    async def _mark_failed(self, record: OutboxEntry, error: str, outbox) -> None:
        """记一次失败并安排退避。指数退避，封顶 MAX_BACKOFF_SECONDS。"""
        backoff = min(2 ** record.attempts, MAX_BACKOFF_SECONDS)
        try:
            await outbox.fail(
                record.id,
                error,
                max_attempts=self._max_attempts,
                backoff_seconds=backoff,
            )
        except Exception as exc:
            # 记失败本身失败了。租约会过期，这条记录自然回到可取状态 —— 所以这里
            # 只告警：唯一的代价是它下次重试不带退避。
            logger.warning(f"记录 outbox 失败状态时出错（{record.id}）: {exc}")

    async def run_forever(self, stop: asyncio.Event) -> None:
        """常驻循环。有活连续处理，空转则等一个轮询间隔。

        `stop` 而非 `while True`：worker 退出时要能立刻打断等待，否则最坏要等满
        一个轮询间隔。用 `wait_for` 包住 `stop.wait()` 就同时拿到了「定时」与
        「可打断」两个语义。
        """
        logger.info(f"记忆投影器启动（{self._who}，轮询 {self._poll_interval}s）")
        while not stop.is_set():
            try:
                report = await self.run_once()
                if report.claimed:
                    logger.info(
                        f"投影 {report.claimed} 条："
                        f"写入 {len(report.upserted)}、删除 {len(report.deleted)}、"
                        f"跳过 {len(report.skipped)}、失败 {len(report.failed)}"
                    )
            except Exception as exc:
                # 循环本身绝不能退出：数据库短暂不可用时它该等着，而不是让整个
                # 投影链路停到下次重启。
                logger.error(f"投影循环异常: {exc}")

            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
        logger.info("记忆投影器已停止")
