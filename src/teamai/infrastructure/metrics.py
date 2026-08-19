"""指标定义与暴露。本项目的第一个可观测面。

## 为什么需要跨进程

指标由 **worker** 更新（projector 每轮回写 Gauge），由 **web** 暴露（`/metrics`）。
两者是不同进程，所以必须用 prometheus_client 的 multiprocess 模式:各进程把样本
写进 `PROMETHEUS_MULTIPROC_DIR` 下的 mmap 文件，web 侧的 collector 汇总它们。

未设 `PROMETHEUS_MULTIPROC_DIR` 时退化为**进程内**指标 —— `/metrics` 只反映 web
自己那一份，projector 的 lag 一律是 0。这不会报错，所以部署时若忘了配这个变量，
症状是「指标看起来一切正常」。故 `build_metrics_asgi_app` 在缺失时打一条 warning。

⚠️ **那个环境变量必须在本模块被 import 之前就已设好。** prometheus_client 在建
Gauge 那一刻（即本模块的模块级语句执行时）就决定了它是「写 mmap 文件」还是「纯
进程内」，之后再 setenv 无效 —— 表现是 `/metrics` 返回 200 而 body 为空。所以它
只能来自环境或 `.env`，不能在代码里按需设置。`test_运行时才设目录会得到空指标`
把这个后果固化成了断言。

## Gauge 为什么由 projector 主动回写，而不是 /metrics 被抓时查库

后者让抓取端能触发数据库查询 —— 抓取频率由外部控制，那是个放大面。代价是 web
暴露的值滞后一个轮询周期（默认 2 秒），对告警足够。

## 哪个指标最该接告警

`teamai_memory_reconcile_total` —— **它长期为 0 才是正常**。持续非零说明 projector
在漏活，而不是对账在干活；对账是安全网，不该是常态路径。

其次是 `teamai_memory_outbox_lag_seconds`（投影积压多久）与
`teamai_memory_outbox_dead`（死信堆积）。
"""

from __future__ import annotations

import logging
import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess
from prometheus_client.exposition import make_asgi_app

from teamai.domain.ports import MetricsSink

logger = logging.getLogger(__name__)

MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"


# ⚠️ multiprocess 模式下 Gauge 必须显式声明 multiprocess_mode。默认是 'all'，
# 它把每个进程的样本各自暴露成一条带 pid 标签的时间序列 —— 对「队列里还剩多少条」
# 这种全局量来说是错的（会看到 N 份重复）。取 'liveall' 让最新写入的那份生效。
_GAUGE_MODE = "liveall"

outbox_pending = Gauge(
    "teamai_memory_outbox_pending",
    "记忆向量投影队列里待处理的条数（不含死信）",
    multiprocess_mode=_GAUGE_MODE,
)

outbox_dead = Gauge(
    "teamai_memory_outbox_dead",
    "记忆向量投影的死信条数（重试超限，需人工处理）",
    multiprocess_mode=_GAUGE_MODE,
)

outbox_lag_seconds = Gauge(
    "teamai_memory_outbox_lag_seconds",
    # 取最老而非平均：平均值会被大量刚入队的记录稀释，而要答的问题是
    # 「最坏情况下一条记忆多久能被搜到」
    "最老待处理条目的等待时长（秒）",
    multiprocess_mode=_GAUGE_MODE,
)

projected_total = Counter(
    "teamai_memory_projected_total",
    "记忆向量投影的处理条数",
    ["op", "result"],
)

embed_seconds = Histogram(
    "teamai_memory_embed_seconds",
    "单次 embedding 调用耗时（秒）",
    # 默认桶上界是 10 秒，而 embedding 供应商限流时单次可能几十秒 —— 那些会全部
    # 落进 +Inf，看不出「慢到什么程度」。加两个大桶。
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

reconcile_total = Counter(
    "teamai_memory_reconcile_total",
    "对账补出的条数。长期为 0 才是正常 —— 持续非零说明投影在漏活",
    ["direction"],
)

embedder_available = Gauge(
    "teamai_embedder_available",
    # 为 0 时记忆库会持续劣化：蒸馏的候选退化成「最近 10 条」，更早的矛盾记忆
    # 进不了比对而并列堆积，而这件事要几周才从回答质量上看出来。
    "embedder 是否可用（0 表示装的是 NullEmbedder，语义检索与去重均降级）",
    # ⚠️ 这个用 'min' 而不是 _GAUGE_MODE('liveall')。两个进程各报自己那份，
    # 而它们的配置**可能不同**（worker 配了、web 没配，或反过来）—— 'liveall' 会
    # 让后写入的那份盖掉另一份，于是「有一个进程降级了」这件事被抹掉。
    # 取 min 让任一进程降级都显示为 0，符合「告警要报最坏的那个」。
    multiprocess_mode="min",
)


def build_metrics_asgi_app():
    """给 web 挂 `/metrics` 用的 ASGI app。

    multiprocess 模式下必须建一个**空的** registry 再挂 MultiProcessCollector，
    不能用默认 registry —— 后者已经注册了本模块这些指标的进程内版本，两者叠加会
    让同名指标重复暴露，Prometheus 侧报 duplicate。
    """
    path = os.environ.get(MULTIPROC_ENV)
    if not path:
        logger.warning(
            f"未设 {MULTIPROC_ENV}，/metrics 只反映本进程 —— "
            f"worker 侧的投影指标（lag / pending / 死信）将一律为 0。"
            f"生产部署必须设置它，且两个进程要指向同一个目录。"
        )
        return make_asgi_app()

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=path)
    return make_asgi_app(registry=registry)


class PrometheusMetricsSink(MetricsSink):
    """MetricsSink 的 prometheus_client 实现。

    每个方法都吞掉异常并只打 debug：指标上报绝不能让业务路径失败。埋点坏了的
    代价是看板缺一条线，而抛出去会让投影整条记录进入退避重试 —— 后者严重得多。

    debug 而非 warning：这类失败往往是持续的（例如 mmap 目录没权限），
    warning 会把日志刷满而信息量只有第一条。
    """

    def outbox_state(self, *, pending: int, dead: int, lag_seconds: float) -> None:
        try:
            outbox_pending.set(pending)
            outbox_dead.set(dead)
            outbox_lag_seconds.set(lag_seconds)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"上报 outbox 状态失败: {exc}")

    def projected(self, *, op: str, result: str) -> None:
        try:
            projected_total.labels(op=op, result=result).inc()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"上报投影结果失败: {exc}")

    def embed_duration(self, seconds: float) -> None:
        try:
            embed_seconds.observe(seconds)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"上报 embedding 耗时失败: {exc}")

    def reconciled(self, *, direction: str, count: int) -> None:
        try:
            reconcile_total.labels(direction=direction).inc(count)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"上报对账结果失败: {exc}")

    def embedder_state(self, *, available: bool) -> None:
        try:
            embedder_available.set(1 if available else 0)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"上报 embedder 可用性失败: {exc}")


def mark_process_exit() -> None:
    """进程退出时清掉本进程的 Gauge 样本文件。

    不清的话，worker 重启后旧 pid 的样本会留在目录里 —— 对 'liveall' 模式的
    Gauge 来说那些死进程的值会继续被暴露，表现是「投影明明停了 lag 却不涨」。
    """
    path = os.environ.get(MULTIPROC_ENV)
    if not path:
        return
    try:
        multiprocess.mark_process_dead(os.getpid(), path)
    except Exception as exc:  # pragma: no cover - 退出路径尽力而为
        logger.warning(f"清理指标样本失败: {exc}")
