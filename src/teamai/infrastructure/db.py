"""SQLAlchemy 异步引擎与会话管理。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

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


async def init_db() -> None:
    """创建表结构（生产环境应使用迁移工具，这里为开发便捷直接建表）。"""
    import teamai.infrastructure.orm  # noqa: F401  确保模型已注册

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_db_or_warn() -> None:
    """建表，失败只告警不中断。两个进程入口共用的启动动作。

    不让异常外抛：Postgres 未就绪时 web 进程仍应起得来，
    否则 `/api/health` 也探不到、编排系统无从区分「没起来」与「依赖没起来」。
    worker 同理，队列消费不该被建表挡住。
    """
    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as exc:
        logger.warning(f"数据库初始化失败（可能未启动 Postgres）: {exc}")
