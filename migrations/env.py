"""Alembic 迁移环境（async 变体）。

与项目建表路径（infrastructure/db.py 的 Base.metadata.create_all）保持一致：
target_metadata 取同一个 Base，URL 取同一个 settings.database_url —— 两条建库
路径（init_db 的开发建表 与 alembic upgrade head 的部署迁移）产出的库不得漂移。

须在导入 Base 前先导入 teamai.infrastructure.orm，把全部表模型注册进 metadata，
否则 autogenerate / upgrade 会静默漏表。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 先注册全部 ORM 表，再取 Base.metadata 作 target_metadata
import teamai.infrastructure.orm  # noqa: F401
from teamai.config import settings
from teamai.infrastructure.db import Base

config = context.config

# 本项目凭据走 settings（.env / config.yaml），不用 alembic.ini 里的占位 URL。
# 在读取配置前注入，async_engine_from_config 才会用到真实连接串。
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline 模式：只生成 SQL 不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
