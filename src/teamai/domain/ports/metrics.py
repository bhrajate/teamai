"""指标上报端口。

## 为什么是端口而不是直接 import

指标实现（prometheus_client）在 infrastructure，而上报点在 application
（`MemoryProjector` / `MemoryReconciler`）。application 不许依赖 infrastructure
（`tests/unit/test_layering.py` 的 ALLOWED 表锁着），所以契约由 domain 声明。

这与 `Embedder` 的处置一致：指标后端是外部系统，换 prometheus 为别的不该动
application 层。

## 为什么方法这么少

只声明这两个上报点真正需要的。指标不是通用日志设施 —— 每加一个方法就是加一条
「有人会调它」的承诺，而没人调的指标比没有指标更糟（看板上一条恒为 0 的线，
分不清是「系统健康」还是「埋点没生效」）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MetricsSink(ABC):
    @abstractmethod
    def outbox_state(self, *, pending: int, dead: int, lag_seconds: float) -> None:
        """记录投影队列的当前状态。由 projector 每轮结束时调。

        主动回写而非让 `/metrics` 被抓时查库：后者让抓取端能触发数据库查询，
        而抓取频率由外部控制 —— 那是个放大面。代价是暴露的值滞后一个轮询周期。
        """
        ...

    @abstractmethod
    def projected(self, *, op: str, result: str) -> None:
        """记录一条投影结果。`result` 取 upserted / deleted / skipped / failed。"""
        ...

    @abstractmethod
    def embed_duration(self, seconds: float) -> None:
        """记录一次 embedding 调用耗时。

        必须在 projector 内部埋 —— 只有那里知道单次调用的边界。上层拿到的
        ProjectionReport 里没有耗时信息，也不该有（那会让报告结构随指标需求变化）。
        """
        ...

    @abstractmethod
    def reconciled(self, *, direction: str, count: int) -> None:
        """记录对账补出的条数。`direction` 取 upsert / delete。

        ⚠️ 这个指标**长期为 0 才是正常**。持续非零说明 projector 在漏活，
        而不是对账在干活 —— 对账是安全网，不该是常态路径。
        """
        ...


class NullMetricsSink(MetricsSink):
    """不上报的实现。给单测与「没配指标」的部署用。

    存在这个实现是为了让上报点可以无条件调 `self._metrics.xxx()`，不必到处判
    None —— 那种判断散开之后必然漏掉一处，而漏掉的那处就是一个静默失效的埋点。
    与 `NullEmbedder` / `NullUnitOfWork` 同一个理由。
    """

    def outbox_state(self, *, pending: int, dead: int, lag_seconds: float) -> None:
        return None

    def projected(self, *, op: str, result: str) -> None:
        return None

    def embed_duration(self, seconds: float) -> None:
        return None

    def reconciled(self, *, direction: str, count: int) -> None:
        return None
