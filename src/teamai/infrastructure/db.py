"""SQLAlchemy 异步引擎与会话管理。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from teamai.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """提供异步会话上下文。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def _upgrade_schema() -> None:
    """同步执行 alembic upgrade head（经 to_thread 放进线程池，不阻塞事件循环）。

    alembic command 是同步 API，其 env.py 内部用 asyncio.run 自建事件循环；
    在线程池里执行正好避开「loop 已运行」冲突。
    """
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]  # src/teamai/infrastructure/db.py -> 仓库根
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    # env.py 会从 settings 取 URL；这里显式再设一遍，不依赖 .ini 里的占位串。
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    # 应用内嵌调用：让 env.py 跳过 fileConfig，避免重置全局 logging 吞掉启动日志。
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """用 alembic 迁移建库，建库只有这一条路径。

    不用 create_all：它只建缺失的表、从不改已有表，与迁移分叉后（版本号落后、
    新对象却已存在）`alembic upgrade head` 会撞「对象已存在」而失败，只能重建库。
    web/worker 启动时 upgrade 到 head，schema 与迁移定义永不过期。
    """
    await asyncio.to_thread(_upgrade_schema)


async def init_db_or_warn() -> None:
    """迁移建库，失败只告警不中断。两个进程入口共用的启动动作。

    不让异常外抛：Postgres 未就绪时 web 进程仍应起得来，
    否则 `/api/health` 也探不到、编排系统无从区分「没起来」与「依赖没起来」。
    worker 同理，队列消费不该被建表挡住。
    """
    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as exc:
        logger.warning(f"数据库初始化失败（可能未启动 Postgres）: {exc}")
