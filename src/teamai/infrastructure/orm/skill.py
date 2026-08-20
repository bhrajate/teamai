"""skills 与 channel_skills 两张表。

skill 全局定义一份、按频道启用，故是多对多，需要关联表。与 mcp_servers 的
「每频道各存一行」不同：skill 的正文是会被反复修改的散文（改一次措辞就要同步
到每个频道），而 MCP server 配置是各频道各自的端点与凭据，本就该分开存。

关联表不设外键约束：本项目其余表之间也一律不设（channel_instance_id 在各表里
都是裸字符串），级联删除由仓储在应用层显式做 —— 见 SQLSkillRepository.delete。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


class SkillModel(Base):
    __tablename__ = "skills"
    __table_args__ = (
        # name 全局唯一：模型是照名字调 load_skill 的，重名会让「载入哪一个」
        # 取决于查询顺序
        UniqueConstraint("name", name="uq_skills_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256))
    # 正文用 Text 而非带长度的 String：这是 Markdown 散文，长度没有合理上限，
    # 而截断的表现是模型照着半句指令干活
    content: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillFileModel(Base):
    """skill 的附带文件。内容存库，不落磁盘。

    存库而非对象存储：文件是文本且有 64 KB 上限（FILE_MAX_BYTES），这个量级
    进 Text 列毫无压力；而引入对象存储会多一个部署依赖与一套凭据，为几十 KB
    的散文不值得。
    """

    __tablename__ = "skill_files"
    __table_args__ = (
        # 同一 skill 内 path 唯一：模型照 path 调 read_skill_file，重复会让
        # 「读到哪一个」取决于查询顺序
        UniqueConstraint("skill_id", "path", name="uq_skill_files_skill_path"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(32), index=True)
    path: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelSkillModel(Base):
    """频道 ↔ skill 的启用关联。"""

    __tablename__ = "channel_skills"
    __table_args__ = (
        # 同一频道同一 skill 只该有一行。没有它，覆盖式写入若在中途失败重试，
        # 会攒出重复行，list_for_channel 便返回重复的 skill（清单里出现两遍）
        UniqueConstraint("channel_instance_id", "skill_id", name="uq_channel_skills_pair"),
    )

    # 关联表本身没有业务 id，但 SQLAlchemy 要求主键；用复合主键而非再造一个
    # 代理键 —— 那个 id 不会被任何地方引用
    channel_instance_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
