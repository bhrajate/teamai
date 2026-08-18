"""清理 memory_entries 里混入的聊天碎片。

背景：改造前 router 对每条非 @ 消息无条件调 `MemoryService.store`（只过滤了
「非空且不以 / 开头」），于是「收到」「好的」「哈哈」与真正的项目背景知识并列
写入，`type` 一律 BACKGROUND_KNOWLEDGE。改造后原文改走 Redis 滚动窗口 + 蒸馏，
但历史遗留的那些行仍在库里稀释向量检索的信噪比。

为什么是独立脚本而非 alembic 迁移：判定条件依赖真实数据分布（多长算「碎片」
要看具体频道的说话习惯），而迁移的 downgrade 无法恢复被删的行 —— 把不可逆的
删除写进自动执行的迁移链里，一旦阈值定错就没有退路。

用法：

    # ① 默认 dry-run：只统计与抽样展示，不动数据
    python -m scripts.cleanup_chat_memories

    # ② 看某个频道，并调阈值
    python -m scripts.cleanup_chat_memories --channel ch_xxx --max-length 30

    # ③ 确认无误后实际删除
    python -m scripts.cleanup_chat_memories --apply

判定条件（三者同时满足才算碎片）：
- `type == BACKGROUND_KNOWLEDGE` —— 蒸馏产出的 DECISION / FACT / PREFERENCE 不碰；
- 内容长度 < `--max-length`（默认 25）—— 真正的背景知识很少这么短。

⚠️ 曾经还有第三条判据 `embedding_ref IS NULL`（「有向量引用的是经过正规写入路径
的，留着」）。它已被删除：改造为 outbox + 异步投影之后，「没有向量」只表示投影还
没追上（暂态），不再表示「非正规路径写入」。留着它会让刚写入、投影未完成的正常
记忆被误判成碎片而删掉 —— 而那个窗口只有几秒，dry-run 时未必看得见，正是最难排查
的一类错误。

⚠️ `--apply` 会真的删行且不可恢复。建议先跑 dry-run 看抽样，必要时先备份：
    pg_dump -t memory_entries ... > memory_entries.sql
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import delete, func, select

from teamai.domain.models.memory import MemoryType
from teamai.infrastructure.db import get_engine, get_session_factory
from teamai.infrastructure.orm.memory import MemoryEntryModel

logger = logging.getLogger("cleanup")

# 默认长度阈值，单位是**字符**（Postgres 的 length() 按字符计，不是字节）。
#
# 取 12 而非更大的值：中文每字符承载的信息量远高于英文，「订单服务的超时阈值是
# 30 秒，由网关侧统一配置」只有 23 个字符，却是一条完整的背景知识。实测把阈值
# 定在 25 会把它一起删掉 —— 这正是 dry-run 存在的意义。
#
# 12 字符能覆盖「收到」（2）、「好的我看下」（5）、「哈哈哈」（3）这类附和，
# 而带主语、谓语与具体参数的陈述句基本都超过它。仍建议先跑 dry-run 看抽样，
# 各团队的说话习惯不同。
DEFAULT_MAX_LENGTH = 12

# dry-run 时抽样展示的条数。既要看得出模式，又不能刷屏。
SAMPLE_SIZE = 15


def _conditions(max_length: int, channel: str | None):
    """碎片判定条件。集中一处，保证统计、抽样、删除三步用的是同一套判据。"""
    conds = [
        MemoryEntryModel.type == MemoryType.BACKGROUND_KNOWLEDGE,
        func.length(MemoryEntryModel.content) < max_length,
        # 不再判 embedding_ref：改造后「没有向量」是暂态（投影未追上），
        # 不再表示「非正规路径写入」。理由见模块文档。
    ]
    if channel:
        conds.append(MemoryEntryModel.channel_instance_id == channel)
    return conds


async def _run(max_length: int, channel: str | None, apply: bool) -> int:
    factory = get_session_factory()
    async with factory() as session:
        conds = _conditions(max_length, channel)

        total_stmt = select(func.count()).select_from(MemoryEntryModel)
        if channel:
            total_stmt = total_stmt.where(MemoryEntryModel.channel_instance_id == channel)
        total = (await session.execute(total_stmt)).scalar_one()

        matched = (
            await session.execute(select(func.count()).select_from(MemoryEntryModel).where(*conds))
        ).scalar_one()

        scope = f"频道 {channel}" if channel else "全部频道"
        logger.info(f"{scope}：共 {total} 条记忆，其中 {matched} 条符合碎片判据（长度 < {max_length}）")

        if matched == 0:
            logger.info("没有需要清理的记录")
            return 0

        samples = (
            (
                await session.execute(
                    select(MemoryEntryModel)
                    .where(*conds)
                    .order_by(MemoryEntryModel.created_at.desc())
                    .limit(SAMPLE_SIZE)
                )
            )
            .scalars()
            .all()
        )
        logger.info(f"抽样 {len(samples)} 条：")
        for s in samples:
            logger.info(f"  [{s.channel_instance_id}] {s.content!r}")

        if not apply:
            logger.info("")
            logger.info(f"dry-run，未改动数据。确认无误后加 --apply 实际删除这 {matched} 条。")
            return 0

        result = await session.execute(delete(MemoryEntryModel).where(*conds))
        await session.commit()
        deleted = int(result.rowcount or 0)
        logger.warning(f"已删除 {deleted} 条聊天碎片")
        logger.info(
            "提示：这些行的向量不会被自动清除 —— 本脚本直接打 SQL，不走 "
            "MemoryService，故不会入队 DELETE。残留向量会在检索时因取不到实体而被"
            "过滤（不泄露内容），但白占 top_k 名额。"
        )
        logger.info(
            "对账（memory-vector-reconcile 定时任务）也查不出这些残留：它按"
            "「行存在但不该有向量」判断，而这些行已经不存在了。需要时手动清理 "
            "Qdrant，或删除整个集合后跑 scripts/rebuild_memory_vectors.py --apply。"
        )
        return deleted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理 memory_entries 里的聊天碎片（默认 dry-run）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际执行删除。不加则只统计与抽样展示",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=DEFAULT_MAX_LENGTH,
        help=f"内容短于该长度视为碎片（默认 {DEFAULT_MAX_LENGTH}）",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="只处理指定频道实例，缺省为全部频道",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main() -> int:
        try:
            return await _run(args.max_length, args.channel, args.apply)
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
