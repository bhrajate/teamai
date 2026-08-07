"""SQLAlchemy 异步引擎与会话管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from teamai.config import settings


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
