"""把记忆重新入队投影，用于全量重建向量索引。

## 什么时候用

对账（`MemoryReconciler`）能发现「向量该有却没有 / 不该有却有 / 内容漂移」，但有
两类情况它查不出来，需要人工触发全量重建：

1. **换了 embedding 模型。** 库里的 `embedded_hash` 与 content 一致、`embedding_ref`
   也在，对账判「一切正常」—— 但那些向量是旧模型算的，与新模型的查询向量不在同一个
   语义空间里，检索质量会静默变差。
2. **向量被写进了错误的集合**（配错 `qdrant_collection`）。Postgres 侧的标记全都
   自洽，只有实际检索不出东西。

另外首次上线本方案时，存量行的 `embedded_hash` 全为 NULL，对账会把它们全判为需
重算 —— 那种情况**不需要**这个脚本，让对账分几轮补完即可（它有 limit 保护）。

## 做法

清掉指定范围内记忆的 `embedding_ref` 与 `embedded_hash`，并入队一条 UPSERT。
清标记是为了让 projector 不走 hash 短路（否则它会判「已是最新」而跳过）。

不直接调 embedder：投影逻辑只该有一处。这个脚本只负责「让 projector 认为该重算」。

用法：

    # ① 默认 dry-run：只统计，不动数据
    python -m scripts.rebuild_memory_vectors

    # ② 只重建某个频道
    python -m scripts.rebuild_memory_vectors --channel ch_xxx

    # ③ 确认后执行
    python -m scripts.rebuild_memory_vectors --apply

⚠️ `--apply` 会让这批记忆在 projector 追上之前**暂时搜不到**（语义检索段为空，
检索会回落到时间倒序）。全量重建大量记忆时建议分频道做。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from teamai.domain.models import MemoryType, OutboxOp
from teamai.infrastructure.db import get_engine, get_session_factory
from teamai.infrastructure.orm.memory import MemoryEntryModel
from teamai.infrastructure.repositories.outbox import SQLOutboxRepository

logger = logging.getLogger("rebuild-vectors")


def _target_stmt(channel: str | None):
    """要重建的行：应当有向量的（非偏好、未被取代）。

    判据与 `MemoryEntry.should_embed()` 一致 —— 偏好和已被取代的条目本就不该有
    向量，把它们入队只会让 projector 白跑一轮删除。
    """
    stmt = select(MemoryEntryModel).where(
        MemoryEntryModel.type != MemoryType.PREFERENCE,
        MemoryEntryModel.superseded_by.is_(None),
    )
    if channel:
        stmt = stmt.where(MemoryEntryModel.channel_instance_id == channel)
    return stmt.order_by(MemoryEntryModel.created_at)


async def _run(channel: str | None, apply: bool) -> int:
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(_target_stmt(channel))).scalars().all())
        scope = f"频道 {channel}" if channel else "全部频道"
        logger.info(f"{scope}：{len(rows)} 条记忆应当有向量")

        if not rows:
            return 0
        if not apply:
            logger.info("")
            logger.info(f"dry-run，未改动数据。确认后加 --apply 重新入队这 {len(rows)} 条。")
            return 0

        outbox = SQLOutboxRepository(session, dialect="postgresql")
        for row in rows:
            # 清标记，否则 projector 会按 hash 判「已是最新」而跳过
            row.embedding_ref = None
            row.embedded_hash = None
            await outbox.enqueue(row.id, OutboxOp.UPSERT)
        await session.commit()

        logger.warning(
            f"已重新入队 {len(rows)} 条。这批记忆在 projector 追上之前暂时搜不到"
            f"（语义检索会回落到时间倒序）。"
        )
        return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="重新入队记忆向量投影，用于全量重建（默认 dry-run）"
    )
    parser.add_argument("--apply", action="store_true", help="实际执行。不加则只统计")
    parser.add_argument("--channel", default=None, help="只处理指定频道，缺省为全部")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main() -> int:
        try:
            return await _run(args.channel, args.apply)
        finally:
            await get_engine().dispose()

    try:
        asyncio.run(_main())
    except Exception as exc:
        logger.error(f"执行失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
