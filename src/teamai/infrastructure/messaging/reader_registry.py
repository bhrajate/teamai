"""按平台分发线程读取，外加一层 Redis 缓存。

两个类都实现 ThreadReader，可以套起来用（Cached 包 Registry），调用方
（application.ConversationService）只认端口，不知道套了几层。

缓存不是可选优化而是必需品：同一线程里连续几条消息各打一次平台 API 会很快
撞上速率限制（Slack 对非 Marketplace 应用的 conversations 系接口配额收紧过）。
TTL 取秒级：线程历史要求「秒级新鲜」，缓存久了会把刚发的消息漏在上下文外。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader
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


def _encode(messages: list[ThreadMessage]) -> str:
    return json.dumps(
        [
            {
                "a": m.author_id,
                "t": m.text,
                "s": m.ts.isoformat() if m.ts else None,
                "b": m.is_bot,
            }
            for m in messages
        ],
        ensure_ascii=False,
    )


def _decode(raw: str) -> list[ThreadMessage]:
    out: list[ThreadMessage] = []
    for d in json.loads(raw):
        ts = d.get("s")
        out.append(
            ThreadMessage(
                author_id=d.get("a", ""),
                text=d.get("t", ""),
                ts=datetime.fromisoformat(ts).astimezone(UTC) if ts else None,
                is_bot=bool(d.get("b")),
            )
        )
    return out


class CachedThreadReader(ThreadReader):
    """给任意 ThreadReader 套一层 Redis 缓存。

    键含 limit：不同 limit 的结果集不同，混用会让要 30 条的调用拿到只有 10 条
    的缓存。键前缀 `thread:` 与 `dedup:` / `ambient:` 并列，便于运维分辨 TTL 语义。

    Redis 不可用时直接穿透到底层读取器：缓存的作用是省配额，不是正确性依赖。
    空结果不缓存 —— 拉取失败也返回空列表，缓存它会把一次偶发失败固化成
    整个 TTL 窗口内都没有历史。
    """

    def __init__(
        self,
        inner: ThreadReader,
        redis: RedisClientProvider | None = None,
        ttl_seconds: int = 45,
    ) -> None:
        self._inner = inner
        self._redis = redis or RedisClientProvider()
        self._ttl = ttl_seconds

    def _key(self, locator: ThreadLocator, limit: int) -> str:
        return f"thread:{locator.platform}:{locator.channel_id}:{locator.thread_ref}:{limit}"

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        key = self._key(locator, limit)
        try:
            client = self._redis.client()
            cached = await client.get(key)
            if cached:
                return _decode(cached if isinstance(cached, str) else cached.decode())
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.debug(f"线程历史缓存读取失败，穿透到平台: {exc}")

        messages = await self._inner.fetch_thread(locator, limit)
        if not messages:
            return messages

        try:
            client = self._redis.client()
            await client.set(key, _encode(messages), ex=self._ttl)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"线程历史缓存写入失败: {exc}")
        return messages

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()  # type: ignore[misc]
