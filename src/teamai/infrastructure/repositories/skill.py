"""SkillRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.skill import Skill, SkillFile
from teamai.domain.repositories.skill import SkillRepository
from teamai.infrastructure.orm.skill import ChannelSkillModel, SkillFileModel, SkillModel


def _file_to_model(f: SkillFile) -> SkillFileModel:
    return SkillFileModel(
        id=f.id,
        skill_id=f.skill_id,
        path=f.path,
        description=f.description,
        content=f.content,
        created_at=f.created_at,
        updated_at=f.updated_at,
    )


def _model_to_file(m: SkillFileModel) -> SkillFile:
    return SkillFile(
        id=m.id,
        skill_id=m.skill_id,
        path=m.path,
        description=m.description,
        content=m.content,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _skill_to_model(s: Skill) -> SkillModel:
    return SkillModel(
        id=s.id,
        name=s.name,
        description=s.description,
        content=s.content,
        enabled=s.enabled,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _model_to_skill(m: SkillModel, files: list[SkillFile] | None = None) -> Skill:
    return Skill(
        id=m.id,
        name=m.name,
        description=m.description,
        content=m.content,
        enabled=m.enabled,
        created_at=m.created_at,
        updated_at=m.updated_at,
        files=files or [],
    )


class SQLSkillRepository(SkillRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _with_files(self, models: list[SkillModel]) -> list[Skill]:
        """把一批 SkillModel 映成领域对象，各自带上附带文件。

        一条 ``IN`` 查询而非逐个 skill 查一次：后者是 N+1，而本方法在 agent 的
        每次 run 上都会被调用（``list_for_channel``），N 是频道启用的技能数。

        文件在构造 ``Skill`` 时一次传入，而不是先建对象再赋 ``.files``：
        后者会被 tests/unit/test_repository_commit.py 的启发式判定成「改了已加载
        的 ORM 模型」（它按「对非 self 对象赋属性」识别写操作）。那是误判 ——
        ``Skill`` 是领域对象、赋值碰不到 session —— 但一次性构造本身也更干净，
        没有「半成品对象」这个中间态。
        """
        if not models:
            return []
        stmt = (
            select(SkillFileModel)
            .where(SkillFileModel.skill_id.in_([m.id for m in models]))
            .order_by(SkillFileModel.path)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[str, list[SkillFile]] = {}
        for f in rows:
            grouped.setdefault(f.skill_id, []).append(_model_to_file(f))
        return [_model_to_skill(m, grouped.get(m.id, [])) for m in models]

    async def list_all(self) -> list[Skill]:
        stmt = select(SkillModel).order_by(SkillModel.name)
        rows = (await self._session.execute(stmt)).scalars().all()
        return await self._with_files(list(rows))

    async def get(self, skill_id: str) -> Skill | None:
        stmt = select(SkillModel).where(SkillModel.id == skill_id)
        m = (await self._session.execute(stmt)).scalars().first()
        if m is None:
            return None
        return (await self._with_files([m]))[0]

    async def find_by_name(self, name: str) -> Skill | None:
        stmt = select(SkillModel).where(SkillModel.name == name)
        m = (await self._session.execute(stmt)).scalars().first()
        if m is None:
            return None
        return (await self._with_files([m]))[0]

    async def upsert(self, skill: Skill) -> None:
        # 只 flush 不 commit：事务边界由用例层（UoW）声明，
        # 见 tests/unit/test_repository_commit.py 的约束说明。
        await self._session.merge(_skill_to_model(skill))
        await self._session.flush()

    async def delete(self, skill_id: str) -> None:
        """删 skill 本体 + 全部频道关联。

        关联行显式删而非靠外键 ON DELETE CASCADE：表间本就没设外键（对齐其余
        表），所以数据库不会替我们做这件事。漏删的后果见接口文档。
        """
        await self._session.execute(
            delete(ChannelSkillModel).where(ChannelSkillModel.skill_id == skill_id)
        )
        # 附带文件同样要清 —— 留下的话它们既取不到（没有 skill 可归属）
        # 也不会被任何地方发现，只是永久占着库
        await self._session.execute(
            delete(SkillFileModel).where(SkillFileModel.skill_id == skill_id)
        )
        await self._session.execute(delete(SkillModel).where(SkillModel.id == skill_id))
        await self._session.flush()

    # ---- 附带文件 ----

    async def get_file(self, skill_id: str, file_id: str) -> SkillFile | None:
        stmt = select(SkillFileModel).where(
            SkillFileModel.id == file_id,
            SkillFileModel.skill_id == skill_id,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_file(m) if m else None

    async def find_file_by_path(self, skill_id: str, path: str) -> SkillFile | None:
        stmt = select(SkillFileModel).where(
            SkillFileModel.skill_id == skill_id,
            SkillFileModel.path == path,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_file(m) if m else None

    async def upsert_file(self, file: SkillFile) -> None:
        await self._session.merge(_file_to_model(file))
        await self._session.flush()

    async def delete_file(self, skill_id: str, file_id: str) -> None:
        stmt = delete(SkillFileModel).where(
            SkillFileModel.id == file_id,
            SkillFileModel.skill_id == skill_id,
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def list_for_channel(self, channel_instance_id: str) -> list[Skill]:
        """已关联 且 全局 enabled 的 skill。

        用 join 而非「先查 id 再查实体」：后者是两次往返，且中间态下（关联行
        指向已删除的 skill）会拿到 None 需要额外过滤。
        """
        stmt = (
            select(SkillModel)
            .join(ChannelSkillModel, ChannelSkillModel.skill_id == SkillModel.id)
            .where(
                ChannelSkillModel.channel_instance_id == channel_instance_id,
                SkillModel.enabled.is_(True),
            )
            .order_by(SkillModel.name)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return await self._with_files(list(rows))

    async def list_channel_skill_ids(self, channel_instance_id: str) -> list[str]:
        stmt = select(ChannelSkillModel.skill_id).where(
            ChannelSkillModel.channel_instance_id == channel_instance_id
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def set_channel_skills(self, channel_instance_id: str, skill_ids: list[str]) -> None:
        """覆盖式设置：先清空该频道的关联，再插入给定集合。

        去重后再插：同一 id 在入参里出现两次会撞 uq_channel_skills_pair，
        而这对调用方是无意义的报错（勾选框本就表达集合语义）。
        """
        await self._session.execute(
            delete(ChannelSkillModel).where(
                ChannelSkillModel.channel_instance_id == channel_instance_id
            )
        )
        now = datetime.now(UTC)
        for skill_id in dict.fromkeys(skill_ids):
            self._session.add(
                ChannelSkillModel(
                    channel_instance_id=channel_instance_id,
                    skill_id=skill_id,
                    created_at=now,
                )
            )
        await self._session.flush()
