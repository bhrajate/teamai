"""按平台分发线程读取，外加一层自更新的 Redis 缓存。

两个类都实现 ThreadReader，可以套起来用（Cached 包 Registry），调用方
（application.ConversationService）只认端口，不知道套了几层。

缓存不是可选优化而是必需品：同一线程里连续几条消息各打一次平台 API 会很快
撞上速率限制（Slack 对非 Marketplace 应用的 conversations 系接口配额收紧过）。

但「只读缓存 + TTL 过期」在这里是错的，因为线程历史不是不可变对象 —— 每来一条
消息它就变了。原先的实现把「线程最近 N 条」当快照整体缓存，于是 TTL 窗口内的
第二次读取拿到的是过期快照：机器人看不见自己上一轮的回复。而唯一会连续读同一
线程的场景就是多轮对话，缓存想省的调用与最需要新鲜数据的时刻完全重合。

故 CachedThreadReader 同时实现 ThreadHistorySink：每条经手的消息 append 进缓存，
缓存自己保持新鲜。TTL 仍然保留，作用从「保证新鲜」变成「兜底纠错」—— 追加过程中
丢的、重的、乱序的，都在下一个窗口被平台的权威数据抹平。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from teamai.domain.ports import (
    ThreadHistorySink,
    ThreadLocator,
    ThreadMessage,
    ThreadReader,
)
from teamai.infrastructure.redis_client import RedisClientProvider

logger = logging.getLogger(__name__)


class ThreadReaderRegistry(ThreadReader):
    """按 locator.platform 路由。未注册的平台返回空历史。

    与 PublisherRegistry 同构：未注册时记 warning 并降级，不抛异常 ——
    平台凭据不全时（settings.slack_enabled / feishu_enabled 为假）就是这个情形，
    此时应只丢历史，不影响任务本身。
    """

    def __init__(self, readers: dict[str, ThreadReader] | None = None) -> None:
        self._readers: dict[str, ThreadReader] = readers or {}

    def register(self, platform: str, reader: ThreadReader) -> None:
        self._readers[platform] = reader

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        reader = self._readers.get(locator.platform)
        if reader is None:
            logger.debug(f"平台 {locator.platform} 未注册 ThreadReader，本次无线程历史")
            return []
        return await reader.fetch_thread(locator, limit)

    async def aclose(self) -> None:
        for reader in self._readers.values():
            closer = getattr(reader, "aclose", None)
            if closer is not None:
                await closer()  # type: ignore[misc]


def _encode(message: ThreadMessage) -> str:
    """单条消息编码成一个 LIST 元素。

    逐条编码而非整表一个 JSON：追加一条消息才能是一次 RPUSHX，不必读出整表、
    改完再写回 —— 后者在 web 与 worker 两个进程并发追加时会互相覆盖。
    键名短是因为这些串每条消息都要在 Redis 里存一份。
    """
    return json.dumps(
        {
            "a": message.author_id,
            "t": message.text,
            "s": message.ts.isoformat() if message.ts else None,
            "b": message.is_bot,
        },
        ensure_ascii=False,
    )


def _decode(raw: str) -> ThreadMessage:
    d = json.loads(raw)
    ts = d.get("s")
    return ThreadMessage(
        author_id=d.get("a", ""),
        text=d.get("t", ""),
        ts=datetime.fromisoformat(ts).astimezone(UTC) if ts else None,
        is_bot=bool(d.get("b")),
    )


class CachedThreadReader(ThreadReader, ThreadHistorySink):
    """给任意 ThreadReader 套一层能自更新的 Redis 缓存。

    数据结构是每线程一个 LIST，按时间正序存最近 `cache_limit` 条消息。键前缀
    `thread:` 与 `dedup:` / `ambient:` 并列，便于运维分辨 TTL 语义。

    **键里不再含 limit。** 原先含它是为了防「要 30 条的调用拿到只有 10 条的
    缓存」，但那样同一线程会有多份互不相干的缓存，`note()` 无从知道该往哪几个键
    追加（SCAN 一遍键空间太贵）。改为缓存固定按 `cache_limit` 存，读取时切出末尾
    `limit` 条 —— 一份缓存服务所有不超过 `cache_limit` 的请求，防串的目的照样达到。
    `limit > cache_limit` 的请求直接绕过缓存打平台，不拿短缓存充数。

    Redis 不可用时穿透到底层读取器：缓存的作用是省配额，不是正确性依赖。
    空结果不缓存 —— 拉取失败也返回空列表，缓存它会把一次偶发失败固化成整个
    TTL 窗口内都没有历史。
    """

    def __init__(
        self,
        inner: ThreadReader,
        redis: RedisClientProvider | None = None,
        ttl_seconds: int = 45,
        cache_limit: int = 30,
    ) -> None:
        self._inner = inner
        self._redis = redis or RedisClientProvider()
        self._ttl = ttl_seconds
        self._cache_limit = max(1, cache_limit)

    def _key(self, locator: ThreadLocator) -> str:
        return f"thread:{locator.platform}:{locator.channel_id}:{locator.thread_ref}"

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        # 要得比缓存容量还多：绕过缓存，否则会把「缓存只存了 30 条」误当成
        # 「线程只有 30 条」返回给要 50 条的调用方。
        if limit > self._cache_limit:
            logger.debug(f"请求 {limit} 条超出缓存容量 {self._cache_limit}，本次直取平台")
            return await self._inner.fetch_thread(locator, limit)

        key = self._key(locator)
        try:
            client = self._redis.client()
            raw = await client.lrange(key, 0, -1)
            if raw:
                cached = [_decode(r if isinstance(r, str) else r.decode()) for r in raw]
                return cached[-limit:]
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.debug(f"线程历史缓存读取失败，穿透到平台: {exc}")

        # 按缓存容量拉取而不是按本次的 limit：多拉的部分留给后续更大的请求复用，
        # 反正是同一次 API 调用 —— 而配额是按调用次数算的。
        messages = await self._inner.fetch_thread(locator, self._cache_limit)
        if not messages:
            return messages

        try:
            client = self._redis.client()
            pipe = client.pipeline()
            # 先删再写：残留的旧元素会与新快照里的同一条消息重复。平台返回的是
            # 权威全量，直接替换掉本地攒的那份。
            pipe.delete(key)
            pipe.rpush(key, *[_encode(m) for m in messages[-self._cache_limit :]])
            pipe.expire(key, self._ttl)
            await pipe.execute()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"线程历史缓存写入失败: {exc}")
        return messages[-limit:]

    async def note(self, locator: ThreadLocator, message: ThreadMessage) -> None:
        """把一条已知消息追加进缓存。无缓存则什么都不做。

        RPUSHX 而非 RPUSH 是这里的关键：键不存在时它返回 0、不建键。否则一条
        追加就会凭空造出一段「只有一条消息的线程历史」，下次读取命中它、把真实
        历史整个挡住 —— 比缓存陈旧严重得多。键不存在意味着下次读取本就会穿透到
        平台拿全量，那才是正确的补法。

        追加后不 EXPIRE：Redis 的 TTL 是键的属性，RPUSHX / LTRIM 这类改值命令不
        会重置它（只有 SET 那样的整键覆盖会）。这正是想要的 —— 缓存仍然每
        `ttl_seconds` 被平台数据整体重建一次，追加过程中万一丢了/重了/乱序了，
        都在下个窗口被抹平，不会永久驻留。
        """
        if not locator.thread_ref or not message.text:
            return
        key = self._key(locator)
        try:
            client = self._redis.client()
            pipe = client.pipeline()
            pipe.rpushx(key, _encode(message))
            # 持续有人说话的线程会一直追加，不截会无界增长（TTL 只是上限不是约束）。
            # 截尾留最近 cache_limit 条，与快照写入时的容量一致。
            pipe.ltrim(key, -self._cache_limit, -1)
            await pipe.execute()
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.debug(f"线程历史缓存追加失败，等 TTL 到点由平台重建: {exc}")

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()  # type: ignore[misc]
