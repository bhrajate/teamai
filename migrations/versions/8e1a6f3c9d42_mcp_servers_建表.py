"""mcp_servers 建表

存每个频道配置的 MCP server（streamable HTTP）：端点、认证头与启停状态。
worker 启动时读取 enabled 的行，连接后把工具以 `mcp__<name>__<tool>` 注册进
工具注册表 —— 表是配置的唯一事实来源，改配置后重启 worker 即生效。

headers 以 JSON 字符串存储，与 permission_policies 的先例一致。它含凭据
（Authorization 等），但本项目是内部部署信任模型（管理台 token 也是明文存
localStorage），不在此做加密；API 响应侧一律脱敏回显。

`(channel_instance_id, name)` 唯一：name 拼进工具名前缀，同频道内必须无歧义。
last_error 是 worker 启动时的连接失败快照，不是实时探活 —— 有意的边界，
探活要常驻连接或定时任务，首期不做。

Revision ID: 8e1a6f3c9d42
Revises: b4e6c2a91d78
Create Date: 2026-08-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e1a6f3c9d42"
down_revision: str | Sequence[str] | None = "b4e6c2a91d78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("headers", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_instance_id",
            "name",
            name="uq_mcp_servers_channel_name",
        ),
    )
    op.create_index(
        "ix_mcp_servers_channel_instance_id",
        "mcp_servers",
        ["channel_instance_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_mcp_servers_channel_instance_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
