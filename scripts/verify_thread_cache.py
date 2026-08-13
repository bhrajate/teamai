"""对真 Redis 验证线程历史缓存的自更新语义。

单测用的是内存替身，这里核对替身与真 Redis 行为一致的三条关键假设 —— 它们
都是「文档说是这样」而非「代码能保证」，替身写错了单测也会绿：

1. RPUSHX 键不存在时不建键（否则一条追加会凭空造出一段假历史）；
2. RPUSHX / LTRIM 不重置 TTL（TTL 到点整体重建是本机制的纠错手段）；
3. LTRIM 负索引按预期保留末尾 N 条。

用法：redis-server --port 6399 --save '' --daemonize yes 后
      uv run python scripts/verify_thread_cache.py
"""

from __future__ import annotations

import asyncio
import sys

REDIS_URL = "redis://127.0.0.1:6399/0"


async def main() -> int:
    import redis.asyncio as aioredis

    from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader
    from teamai.infrastructure.messaging.reader_registry import CachedThreadReader

    class Provider:
        def __init__(self) -> None:
            self._c = aioredis.from_url(REDIS_URL, decode_responses=True)

        def client(self):
            return self._c

        async def aclose(self) -> None:
            await self._c.aclose()

    class Platform(ThreadReader):
        """记录被打了几次，模拟平台配额。"""

        def __init__(self) -> None:
            self.calls = 0

        async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
            self.calls += 1
            return [ThreadMessage(author_id="U1", text="列几个方案")]

    provider = Provider()
    client = provider.client()
    locator = ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")
    key = "thread:slack:C1:t1"
    await client.delete(key)

    platform = Platform()
    cached = CachedThreadReader(platform, provider, ttl_seconds=45, cache_limit=5)
    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(label)

    print("1. 无缓存时 note 不建键")
    await cached.note(locator, ThreadMessage(author_id="U1", text="孤零零一句"))
    check(await client.exists(key) == 0, "RPUSHX 未建键")
    check(platform.calls == 0, "note 不该打平台")

    print("2. 首次读取写入快照并设 TTL")
    first = await cached.fetch_thread(locator, 5)
    check([m.text for m in first] == ["列几个方案"], "返回平台数据")
    ttl_after_write = await client.ttl(key)
    check(0 < ttl_after_write <= 45, f"TTL 已设置（{ttl_after_write}s）")

    print("3. 追加后 TTL 不被延长，且新消息立即可见")
    await asyncio.sleep(1.1)  # 让 TTL 明显走掉一截
    ttl_before_note = await client.ttl(key)
    await cached.note(locator, ThreadMessage(author_id="B1", text="方案一二三", is_bot=True))
    await cached.note(locator, ThreadMessage(author_id="U1", text="第二个细化下"))
    ttl_after_note = await client.ttl(key)
    check(
        ttl_after_note <= ttl_before_note,
        f"TTL 未被续期（{ttl_before_note}s → {ttl_after_note}s）",
    )
    check(ttl_after_note < ttl_after_write, "TTL 仍在原窗口内倒数")

    second = await cached.fetch_thread(locator, 5)
    check(
        [m.text for m in second] == ["列几个方案", "方案一二三", "第二个细化下"],
        "机器人自己的回复在窗口内可见",
    )
    check([m.is_bot for m in second] == [False, True, False], "is_bot 往返正确")
    check(platform.calls == 1, f"全程只打一次平台（实际 {platform.calls} 次）")

    print("4. 追加按容量截尾（cache_limit=5）")
    for i in range(6):
        await cached.note(locator, ThreadMessage(author_id="U1", text=f"新 {i}"))
    check(await client.llen(key) == 5, f"LTRIM 生效，长度 {await client.llen(key)}")
    tail = await cached.fetch_thread(locator, 5)
    check([m.text for m in tail] == [f"新 {i}" for i in range(1, 6)], "保留的是最近 5 条")

    print("5. 小 limit 从缓存切尾，不重打平台")
    few = await cached.fetch_thread(locator, 2)
    check([m.text for m in few] == ["新 4", "新 5"], "切出最近 2 条")
    check(platform.calls == 1, "仍未再打平台")

    print("6. 超出容量的请求绕过缓存")
    many = await cached.fetch_thread(locator, 50)
    check(platform.calls == 2, "直取平台")
    check([m.text for m in many] == ["列几个方案"], "返回平台原样结果")

    print("7. TTL 到点后由平台数据整体重建")
    await client.expire(key, 1)
    await asyncio.sleep(1.3)
    check(await client.exists(key) == 0, "键已过期")
    rebuilt = await cached.fetch_thread(locator, 5)
    check([m.text for m in rebuilt] == ["列几个方案"], "本地追加的内容已被抹平")

    await client.delete(key)
    await provider.aclose()

    print()
    if failures:
        print(f"{len(failures)} 条断言失败：" + "，".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
