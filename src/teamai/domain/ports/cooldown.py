"""主动介入的冷却端口。

与 `EventDeduplicator` 分开而非复用：那个端口的 TTL 在构造时由
`event_dedup.ttl_seconds` 固定，用于「同一事件的重投窗口」，全局一个值；
而 Ambient 的冷却是「同一条件多久内不重复打扰」，每条规则各有阈值，
必须按调用传。语义与生命周期都不同，故给独立端口。

实现同样用 Redis 的 `SET NX EX`（见 infrastructure/cooldown.py）。
"""

from __future__ import annotations

from typing import Protocol


class AmbientCooldown(Protocol):
    """记录「某个提醒刚发过」，冷却期内再问即返回 True。"""

    async def is_cooling(self, key: str, ttl_seconds: int) -> bool:
        """冷却期内返回 True（此次不该发）；否则占位并返回 False（此次该发）。

        占位与查询必须是同一个原子操作：worker 多副本时两个进程会同时扫到
        同一个任务，先查再写会让两边都判定「该发」而重复打扰。
        """
        ...
