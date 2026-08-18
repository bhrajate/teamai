"""记忆向量对账：找出「向量状态与记忆状态不符」的行，重新入队。

## 为什么 outbox 之外还要这个

outbox 保证「我发出的意图最终会执行」，不保证「执行结果后来没被别人改掉」。
三种情况它覆盖不到：

1. 本方案上线**之前**写入的存量行 —— 它们不在 outbox 里。改造前的缺陷（写向量
   失败只打 warning、`_InMemoryVectorStore` 制造假成功）留下的偏差全在这一类。
2. projector 自己会有 bug，死信会被人工重置，`op` 语义可能被误用。
3. **向量库侧被外部改动** —— 重建集合、误删、把备份恢复到旧时点。这时 Postgres 的
   `embedding_ref` 与实际不符，而只有对账能发现。

## 只入队，不直接投影

命中的行补写一条 outbox 记录，交给 projector。这样投影逻辑只有一处 —— 对账若自己
调 embedder + 向量库，两边的决策规则就要各写一遍，而它们必须一致（不一致会让两方
互相拆台：一方判「该有向量」不断建，另一方判「不该有」不断删）。

## 判据在哪里

两个谓词落在 `MemoryRepository.find_vector_drift()` —— 本服务只负责「把它找出来的
东西入队」。谓词是 `domain/models/memory.should_embed()` 的 SQL 等价形式，逐字对应
`docs/plan-memory-outbox.md` §5.1 的不变量。

本服务此前直接写 SQL 并 import `infrastructure.orm`，被分层测试拦住 —— 拦得对：
`md5()` 的方言差异、「哪几列参与判断」都是持久化细节，application 层不该知道。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from teamai.domain.models import OutboxOp
from teamai.domain.ports import MetricsSink, NullMetricsSink
from teamai.domain.repositories import MemoryRepository, OutboxRepository

logger = logging.getLogger(__name__)

# 单轮最多补多少条。有上界：首次上线时存量偏差可能成千上万，一次全塞进 outbox 会
# 让 projector 那一批的 lag 指标瞬间爆表、且挤掉正常写入的投影。分几轮补完即可 ——
# 对账是安全网，不赶时间。
DEFAULT_LIMIT = 500


@dataclass
class ReconcileReport:
    """一轮对账的结果。

    分方向记而不只报总数：`missing` 持续非零说明 projector 在漏活或存量没补完，
    `stale` 持续非零说明有人在绕过投影改向量状态 —— 两者的排查方向完全不同。
    """

    # 该有向量却没有（或 hash 不符）→ 补 UPSERT
    missing: list[str]
    # 不该有向量却有 → 补 DELETE
    stale: list[str]

    @property
    def total(self) -> int:
        return len(self.missing) + len(self.stale)


class MemoryReconciler:
    def __init__(
        self,
        repo: MemoryRepository,
        outbox: OutboxRepository,
        *,
        limit: int = DEFAULT_LIMIT,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._repo = repo
        self._outbox = outbox
        self._limit = limit
        self._metrics = metrics or NullMetricsSink()

    async def run_once(self) -> ReconcileReport:
        missing, stale = await self._repo.find_vector_drift(self._limit)

        for entry_id in missing:
            await self._outbox.enqueue(entry_id, OutboxOp.UPSERT)
        for entry_id in stale:
            await self._outbox.enqueue(entry_id, OutboxOp.DELETE)

        # 即便为 0 也上报：Counter 的 inc(0) 让这条时间序列存在，于是看板上能区分
        # 「系统健康（线在，恒为 0）」与「埋点没生效（线不存在）」。
        self._metrics.reconciled(direction="upsert", count=len(missing))
        self._metrics.reconciled(direction="delete", count=len(stale))

        if missing or stale:
            # info 而非 debug：长期为 0 才是正常。持续非零说明 projector 在漏活，
            # 而不是对账在干活 —— 对账是安全网，不该是常态路径。
            logger.info(f"记忆向量对账：补 UPSERT {len(missing)} 条、补 DELETE {len(stale)} 条")
        return ReconcileReport(missing=missing, stale=stale)
