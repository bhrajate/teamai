"""skill_files 建表

skill 的附带文件（参考文档、配置样例、脚本源码）。内容存库而非对象存储：
文件一律是文本且有 64 KB 上限（domain/models/skill.py 的 FILE_MAX_BYTES），
这个量级进 Text 列毫无压力，而引入对象存储会多一个部署依赖与一套凭据。

上限本身是「文件预加载进 ContextBundle」这个设计的前提：每次 agent run 都会把
本频道全部启用 skill 的文件读进内存（工具执行时不能碰共享 AsyncSession），
没有上限时一份 10 MB 的文档会让每次 run 都多读一遍它。

这张表让渐进式披露变成三级：系统提示词给 name+description，load_skill 给正文
与文件清单（只有路径、大小、用途），read_skill_file 才给某个文件的内容。
把文件内容内联进第二级会让「带 3 个文档的 skill」每次载入都付全部文档的代价。

`(skill_id, path)` 唯一：模型照 path 调 read_skill_file，重复会让「读到哪一个」
取决于查询顺序。不设外键（对齐其余表），删 skill 时的级联由
SQLSkillRepository.delete 显式做。

文件只读 —— 脚本对模型也只是可读文本，本项目不提供执行路径。

Revision ID: e6c1f4a8b920
Revises: d4a7b2e9f150
Create Date: 2026-08-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6c1f4a8b920"
down_revision: str | Sequence[str] | None = "d4a7b2e9f150"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skill_files",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("skill_id", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "path", name="uq_skill_files_skill_path"),
    )
    op.create_index("ix_skill_files_skill_id", "skill_files", ["skill_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_skill_files_skill_id", table_name="skill_files")
    op.drop_table("skill_files")
