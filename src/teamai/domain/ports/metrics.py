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

    @abstractmethod
    def embedder_state(self, *, available: bool) -> None:
        """记录 embedder 是否可用。装配时调一次，两个进程各报自己那份。

        与其余四个不同，这是**静态能力**而不是运行速率 —— 值只在重启时可能变。
        它仍然值得是个指标：没有 embedding 时记忆库会持续劣化（蒸馏的候选退化成
        「最近 10 条」，更早的矛盾记忆进不了比对而并列堆积），而这件事要几周才从
        回答质量上看出来。日志里那条 warning 只在启动时出现一次，滚掉之后就没人
        知道了；一条恒为 0 的时间序列才能让告警规则挂上去。

        为什么不做成「查一下就知道」：`/metrics` 被抓时去问 embedder 等于让抓取端
        触发外部调用，与 `outbox_state` 主动回写同一个理由。
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

    def embedder_state(self, *, available: bool) -> None:
        return None
