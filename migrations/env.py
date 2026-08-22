"""Alembic 迁移环境（async 变体）。

建库只有这一条路径：web/worker 启动时 init_db() 直接调 alembic upgrade head
（见 infrastructure/db.py），不存在 create_all 那条分叉 —— 它只建缺失的表、从不
改已有表，与迁移分叉后 upgrade head 会撞「对象已存在」而失败，只能重建库。

target_metadata 取 Base.metadata，URL 取 settings.database_url —— 与应用的
引擎同源。须在导入 Base 前先导入 teamai.infrastructure.orm，把全部表模型注册进
metadata，否则 autogenerate / upgrade 会静默漏表。
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

# 嵌入调用（web/worker 启动时经 db.py 的 _upgrade_schema）跳过 fileConfig：
# 它会把全局 logging 重置、禁用现有 logger，应用侧启动日志（含 uvicorn 的
# startup 错误）会被吞成静默失败。手动跑 alembic CLI 时仍按 ini 正常配置。
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
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
