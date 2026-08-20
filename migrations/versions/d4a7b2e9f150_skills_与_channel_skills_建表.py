"""skills 与 channel_skills 建表

skill 是一份「怎么做某类事」的指令文本，全局定义一份、按频道启用，故是多对多。
与 mcp_servers 的「每频道各存一行」不同：skill 正文是会被反复改措辞的散文，
按频道存会分裂成多份副本各自漂移。

模型侧走渐进式披露：系统提示词只常驻 `name: description`，正文由模型调
`load_skill(name)` 按需取回。因此 description 限长（每次 run 都要付它的
token），content 用 Text（散文没有合理长度上限）。

`skills.name` 全局唯一 —— 模型是照名字调工具的，重名会让「载入哪一个」取决于
查询顺序。`channel_skills` 的 (channel_instance_id, skill_id) 唯一，防覆盖式
写入中途重试攒出重复行（表现是清单里同一个 skill 出现两遍）。

不设外键：对齐本项目其余表（channel_instance_id 在各表里都是裸字符串），
删 skill 时的关联清理由 SQLSkillRepository.delete 显式做。

Revision ID: d4a7b2e9f150
Revises: 8e1a6f3c9d42
Create Date: 2026-08-20 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7b2e9f150"
down_revision: str | Sequence[str] | None = "8e1a6f3c9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_skills_name"),
    )
    op.create_table(
        "channel_skills",
        sa.Column("channel_instance_id", sa.String(length=32), nullable=False),
        sa.Column("skill_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_instance_id", "skill_id"),
        sa.UniqueConstraint(
            "channel_instance_id",
            "skill_id",
            name="uq_channel_skills_pair",
        ),
    )
    op.create_index(
        "ix_channel_skills_channel_instance_id",
        "channel_skills",
        ["channel_instance_id"],
    )
    op.create_index("ix_channel_skills_skill_id", "channel_skills", ["skill_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_channel_skills_skill_id", table_name="channel_skills")
    op.drop_index("ix_channel_skills_channel_instance_id", table_name="channel_skills")
    op.drop_table("channel_skills")
    op.drop_table("skills")
