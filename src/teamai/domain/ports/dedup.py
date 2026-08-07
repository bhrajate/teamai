"""事件去重端口。

Slack 会重投事件（网络抖动、我方响应超时），同一事件可能到达多次。若不去重，
一条 @提及会被重复建任务、重复调 LLM、重复回复。

严格说这是传输层关切而非领域概念，放在 domain/ports 是分层规则决定的：
infrastructure 只允许依赖 domain，抽象若放在 adapters，infrastructure 就
无法实现它。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EventDeduplicator(ABC):
    """已处理事件的记账本。"""

    @abstractmethod
    async def is_duplicate(self, event_key: str) -> bool:
        """检查并登记，一步完成。

        首次见到 event_key：登记它，返回 False（调用方应继续处理）。
        已经见过：返回 True（调用方应丢弃）。

        实现必须**原子**完成「检查 + 登记」。分成两步的话，两个并发到达的
        重投事件会同时通过检查，去重形同虚设 —— Slack 重投恰恰常与首次
        请求并发。Redis 侧用 `SET NX` 天然满足。

        实现还需给记录设过期时间：Slack 的重投窗口有限，无限期保留只是
        白占内存。
        """
        ...
