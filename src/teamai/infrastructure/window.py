"""MessageWindow 的 Redis 实现，内存兜底。

数据结构用两个键：
- `window:<channel>` —— LIST，按序存该频道待蒸馏的对话行；
- `window:index` —— ZSET，member 是 channel_id、score 是该窗口首次写入的时间戳。

要 ZSET 是因为 `due_channels` 必须能只挑出到期的频道。没有它就只能 SCAN 全部
`window:*` 键再逐个 LLEN —— 频道多了之后每轮巡检都要扫一遍键空间。

原文只在这里停留（分钟级），蒸馏完即弃：聊天原文不进关系库，理由见
docs/Design-conversation-context.md §2。因此这里也不做持久化保证 —— Redis 重启
丢掉一窗未蒸馏的对话，代价是少沉淀几条记忆，可以接受。
"""

from __future__ import annotations

import logging
import time

from teamai.domain.ports import MessageWindow

logger = logging.getLogger(__name__)

_LIST_PREFIX = "window:"
_INDEX_KEY = "window:index"

# 单个窗口的硬上限，防止某个频道刷屏把内存吃满。超过就丢最旧的 ——
# 蒸馏取的是「近期讨论的结论」，最旧的那些价值本就最低。
MAX_WINDOW_SIZE = 200


class InMemoryMessageWindow(MessageWindow):
    """单进程兜底。多副本下各自攒窗，同一频道可能被蒸馏多次（产出重复记忆）。

    仅用于无 Redis 的开发场景。
    """

    def __init__(self) -> None:
        self._windows: dict[str, list[str]] = {}
        self._first_at: dict[str, float] = {}

    async def append(self, channel_instance_id: str, line: str) -> int:
        window = self._windows.setdefault(channel_instance_id, [])
        window.append(line)
        self._first_at.setdefault(channel_instance_id, time.time())
        if len(window) > MAX_WINDOW_SIZE:
            del window[: len(window) - MAX_WINDOW_SIZE]
        return len(window)

    async def due_channels(self, max_size: int, max_idle_seconds: int) -> list[str]:
        now = time.time()
        return [
            ch
            for ch, window in self._windows.items()
            if window
            and (len(window) >= max_size or now - self._first_at.get(ch, now) >= max_idle_seconds)
        ]

    async def drain(self, channel_instance_id: str) -> list[str]:
        lines = self._windows.pop(channel_instance_id, [])
        self._first_at.pop(channel_instance_id, None)
        return lines


class RedisMessageWindow(MessageWindow):
    def __init__(self, redis=None) -> None:
        from teamai.infrastructure.redis_client import RedisClientProvider

        self._redis = redis or RedisClientProvider()
        self._fallback = InMemoryMessageWindow()

    @staticmethod
    def _list_key(channel_instance_id: str) -> str:
        return f"{_LIST_PREFIX}{channel_instance_id}"

    async def append(self, channel_instance_id: str, line: str) -> int:
        key = self._list_key(channel_instance_id)
        try:
            client = self._redis.client()
            pipe = client.pipeline()
            pipe.rpush(key, line)
            # NX：只在该频道尚无窗口时记首次写入时间。用 GT/普通 ZADD 会让每条
            # 消息都刷新 score，于是「持续有人说话」的频道永远不满足静置条件、
            # 只能等窗口攒满 —— 而热闹频道恰恰是最该及时蒸馏的。
            pipe.zadd(_INDEX_KEY, {channel_instance_id: time.time()}, nx=True)
            pipe.ltrim(key, -MAX_WINDOW_SIZE, -1)
            pipe.llen(key)
            results = await pipe.execute()
            return int(results[-1])
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.debug(f"窗口写入 Redis 失败，降级到内存: {exc}")
            return await self._fallback.append(channel_instance_id, line)

    async def due_channels(self, max_size: int, max_idle_seconds: int) -> list[str]:
        try:
            client = self._redis.client()
            members = await client.zrange(_INDEX_KEY, 0, -1, withscores=True)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"读取窗口索引失败，降级到内存: {exc}")
            return await self._fallback.due_channels(max_size, max_idle_seconds)

        now = time.time()
        due: list[str] = []
        for raw, score in members:
            channel_id = raw if isinstance(raw, str) else raw.decode()
            if now - score >= max_idle_seconds:
                due.append(channel_id)
                continue
            try:
                if await client.llen(self._list_key(channel_id)) >= max_size:
                    due.append(channel_id)
            except Exception:  # pragma: no cover
                continue
        return due

    async def drain(self, channel_instance_id: str) -> list[str]:
        key = self._list_key(channel_instance_id)
        try:
            client = self._redis.client()
            pipe = client.pipeline()
            # 取全量再删键，两个命令在同一 pipeline 里：中间有新消息写入时
            # 会被一并删掉（丢至多几条），但不会出现「取了却没删」导致下一轮
            # 重复蒸馏同一批对话、产出重复记忆。宁可少沉淀，不可重复沉淀。
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            pipe.zrem(_INDEX_KEY, channel_instance_id)
            results = await pipe.execute()
            lines = results[0] or []
            return [line if isinstance(line, str) else line.decode() for line in lines]
        except Exception as exc:  # pragma: no cover
            logger.debug(f"窗口取出失败，降级到内存: {exc}")
            return await self._fallback.drain(channel_instance_id)


def build_message_window(redis=None) -> MessageWindow:
    return RedisMessageWindow(redis)
