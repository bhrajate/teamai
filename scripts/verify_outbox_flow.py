"""冒烟：记忆写入 → outbox 入队 → 投影 → 向量可检索。

单测全用替身，这里打真 Postgres 与真 outbox 表，验证跨进程的形态：入队与记忆行
是否真在一个事务里、租约 SQL 在 Postgres 上是否如预期、投影后 embedded_hash 是否
回填。向量库与 embedder 用桩（不依赖 Qdrant 与 embedding 凭据），因为要验的是
**投影链路的形态**，不是那两个外部服务本身。

形状对齐 scripts/verify_long_task_flow.py。

用法：

    make up               # 至少要 postgres
    make migrate
    python -m scripts.verify_outbox_flow

退出码 0 表示全部断言通过。会在库里留下一个 `ch_verify_outbox` 频道的测试数据，
脚本末尾自行清理。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import delete, select

from teamai.application.memory import MemoryService
from teamai.application.projector import MemoryProjector, content_hash
from teamai.domain.models import MemoryEntry, MemoryType, OutboxOp
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.db import get_engine, get_session_factory
from teamai.infrastructure.orm.memory import MemoryEntryModel
from teamai.infrastructure.orm.outbox import MemoryOutboxModel
from teamai.infrastructure.repositories import (
    SQLAuditRepository,
    SQLChannelRepository,
    SQLMemoryRepository,
    SQLOutboxRepository,
)
from teamai.infrastructure.uow import SQLUnitOfWork

logger = logging.getLogger("verify-outbox")

CHANNEL = "ch_verify_outbox"


class _StubVectorStore:
    def __init__(self) -> None:
        self.upserted: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def upsert(self, entry: MemoryEntry, embedding: list[float]) -> str:
        self.upserted.append((entry.id, entry.content))
        return f"point-{entry.id}"

    async def query(self, channel_instance_id: str, embedding, top_k: int) -> list[str]:
        return [eid for eid, _ in self.upserted][:top_k]

    async def delete(self, entry_id: str) -> None:
        self.deleted.append(entry_id)


class _StubEmbedder:
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2, 0.3]


def _memory_service(session, outbox_repo) -> MemoryService:
    return MemoryService(
        SQLMemoryRepository(session),
        SQLChannelRepository(session),
        AuditLogWriter(SQLAuditRepository(session)),
        outbox_repo,
        SQLUnitOfWork(session),
    )


async def _cleanup(session) -> None:
    ids = (
        (
            await session.execute(
                select(MemoryEntryModel.id).where(
                    MemoryEntryModel.channel_instance_id == CHANNEL
                )
            )
        )
        .scalars()
        .all()
    )
    if ids:
        await session.execute(
            delete(MemoryOutboxModel).where(MemoryOutboxModel.entry_id.in_(ids))
        )
    await session.execute(
        delete(MemoryEntryModel).where(MemoryEntryModel.channel_instance_id == CHANNEL)
    )
    await session.commit()


async def _pending(session, entry_id: str) -> list[MemoryOutboxModel]:
    return list(
        (
            await session.execute(
                select(MemoryOutboxModel).where(MemoryOutboxModel.entry_id == entry_id)
            )
        )
        .scalars()
        .all()
    )


async def _run() -> int:
    factory = get_session_factory()
    vector, embedder = _StubVectorStore(), _StubEmbedder()

    # 投影器每轮开一个新 session —— 与生产一致
    from contextlib import asynccontextmanager
    from dataclasses import dataclass

    @dataclass
    class _Scope:
        outbox_repo: object
        memory_repo: object

    @asynccontextmanager
    async def _scope():
        s = factory()
        try:
            yield _Scope(
                outbox_repo=SQLOutboxRepository(s, dialect="postgresql"),
                memory_repo=SQLMemoryRepository(s),
            )
            await s.commit()
        finally:
            await s.close()

    projector = MemoryProjector(_scope, vector, embedder, batch_size=10)

    async with factory() as session:
        await _cleanup(session)

        # ① 写一条记忆 —— 记忆行与 outbox 行必须同时出现
        outbox_repo = SQLOutboxRepository(session, dialect="postgresql")
        service = _memory_service(session, outbox_repo)
        entry = await service.store(CHANNEL, "订单服务的超时阈值是 30 秒", type=MemoryType.FACT)
        logger.info(f"① 写入记忆 {entry.id}")

        rows = await _pending(session, entry.id)
        assert len(rows) == 1, f"应有 1 条 outbox 记录，实际 {len(rows)}"
        assert rows[0].op is OutboxOp.UPSERT
        assert entry.embedding_ref is None, "写路径不该回填 embedding_ref"
        logger.info("   ✓ outbox 入队且未回填向量标记")

    # ② 投影一轮
    report = await projector.run_once()
    assert report.upserted == [entry.id], f"应投影 1 条，实际 {report}"
    assert vector.upserted == [(entry.id, "订单服务的超时阈值是 30 秒")]
    logger.info("② 投影完成，向量已写入")

    async with factory() as session:
        stored = await SQLMemoryRepository(session).get(entry.id)
        assert stored is not None
        assert stored.embedding_ref == f"point-{entry.id}"
        assert stored.embedded_hash == content_hash("订单服务的超时阈值是 30 秒")
        assert await _pending(session, entry.id) == [], "处理成功后 outbox 应清空"
        logger.info("   ✓ embedding_ref 与 embedded_hash 已回填，队列已清空")

    # ③ 改内容 —— 应重新入队，投影后 hash 跟着变
    async with factory() as session:
        service = _memory_service(session, SQLOutboxRepository(session, dialect="postgresql"))
        await service.edit(entry.id, content="超时阈值改成 5 秒了")
        rows = await _pending(session, entry.id)
        assert len(rows) == 1, "改内容后应重新入队"
    report = await projector.run_once()
    assert report.upserted == [entry.id]
    async with factory() as session:
        stored = await SQLMemoryRepository(session).get(entry.id)
        assert stored is not None and stored.embedded_hash == content_hash("超时阈值改成 5 秒了")
        logger.info("③ 编辑后向量按新内容重算，hash 已更新")

    # ④ 幂等：同一条再投影一轮应被跳过，不重复调 embedder
    before = len(embedder.calls)
    async with factory() as session:
        await SQLOutboxRepository(session, dialect="postgresql").enqueue(
            entry.id, OutboxOp.UPSERT
        )
        await session.commit()
    report = await projector.run_once()
    assert report.skipped == [entry.id], f"hash 未变应跳过，实际 {report}"
    assert len(embedder.calls) == before, "跳过时不该再调 embedding"
    logger.info("④ hash 未变时跳过，未重复 embed")

    # ⑤ 删除 —— 入队 DELETE，投影后向量被撤
    async with factory() as session:
        service = _memory_service(session, SQLOutboxRepository(session, dialect="postgresql"))
        await service.delete(entry.id)
        rows = await _pending(session, entry.id)
        assert len(rows) == 1 and rows[0].op is OutboxOp.DELETE
    report = await projector.run_once()
    assert report.deleted == [entry.id]
    assert entry.id in vector.deleted
    logger.info("⑤ 删除后向量已撤")

    # ⑥ 偏好不入队
    async with factory() as session:
        service = _memory_service(session, SQLOutboxRepository(session, dialect="postgresql"))
        pref = await service.store(CHANNEL, "回答要简短", type=MemoryType.PREFERENCE)
        assert await _pending(session, pref.id) == [], "偏好不该入队"
        logger.info("⑥ 偏好未入队")
        await _cleanup(session)

    logger.info("")
    logger.info("全部断言通过 ✓")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main() -> int:
        try:
            return await _run()
        finally:
            await get_engine().dispose()

    try:
        return asyncio.run(_main())
    except AssertionError as exc:
        logger.error(f"断言失败: {exc}")
        return 1
    except Exception as exc:
        logger.error(f"执行失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
