"""Skill 管理服务：全局 skill 库的 CRUD + 频道启用关系。

skill 是全局定义、按频道启用的指令文本（模型经 ``load_skill`` 按需取回，
见 :mod:`teamai.domain.models.skill`）。本服务是管理台的用例层，agent 侧只经
``SkillRepository.list_for_channel`` 读，不走这里。

审计复用 ``POLICY_CHANGE`` + ``detail.event``，与 TagResolver 的先例一致，
而不是新增 ``AuditAction`` 成员：``audit_logs.action`` 在 Postgres 上是原生
枚举类型，加成员必须配一条 ``ALTER TYPE ... ADD VALUE`` 迁移，漏了会让**已升级
的库**在写审计时抛 InvalidTextRepresentationError —— 这是真实发生过的故障
（见 tests/unit/test_enum_migrations.py 的背景说明）。skill 变更本就属于
「谁改了这个频道的行为配置」，归到 POLICY_CHANGE 语义上也站得住。
"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.domain.identity import gen_id
from teamai.domain.models import GLOBAL_SCOPE, AuditAction
from teamai.domain.models.skill import Skill, SkillFile
from teamai.domain.ports.uow import UnitOfWork
from teamai.domain.repositories.skill import SkillRepository
from teamai.domain.services import AuditLogWriter

# GLOBAL_SCOPE 定义在 domain/models/audit.py —— 它是审计的概念（「这条记录不属于
# 任何频道」），读侧（admin/audit.py 的全局流水端点）也要用同一个值。
# 放在这里会让读侧反向依赖 application。


class SkillService:
    def __init__(
        self,
        repo: SkillRepository,
        audit: AuditLogWriter,
        uow: UnitOfWork,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._uow = uow

    async def list_all(self) -> list[Skill]:
        return await self._repo.list_all()

    async def get(self, skill_id: str) -> Skill | None:
        return await self._repo.get(skill_id)

    async def find_by_name(self, name: str) -> Skill | None:
        return await self._repo.find_by_name(name)

    async def create(
        self,
        name: str,
        description: str,
        content: str,
        *,
        enabled: bool = True,
        actor: str | None = None,
    ) -> Skill:
        skill = Skill(
            id=gen_id("skill"),
            name=name,
            description=description,
            content=content,
            enabled=enabled,
        )
        async with self._uow:
            await self._repo.upsert(skill)
        await self._audit.record(
            GLOBAL_SCOPE,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "skill_create", "skill": name},
        )
        return skill

    async def update(
        self,
        skill: Skill,
        *,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
        enabled: bool | None = None,
        actor: str | None = None,
    ) -> Skill:
        """改已有 skill。只改传入的字段。

        改动立即对**全部**启用频道生效 —— 正文只有一份，这正是全局定义的用意。
        与 MCP server 不同，这里不需要重启 worker：skill 是每次 run 从库里读的，
        不像 MCP 那样在启动时建立长连接。
        """
        changed: dict[str, object] = {}
        if name is not None and name != skill.name:
            skill.name = name
            changed["name"] = name
        if description is not None:
            skill.description = description
            changed["description"] = True
        if content is not None:
            skill.content = content
            changed["content"] = True
        if enabled is not None and enabled != skill.enabled:
            skill.enabled = enabled
            changed["enabled"] = enabled

        skill.updated_at = datetime.now(UTC)
        async with self._uow:
            await self._repo.upsert(skill)
        await self._audit.record(
            GLOBAL_SCOPE,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "skill_update", "skill": skill.name, "changed": changed},
        )
        return skill

    async def delete(self, skill: Skill, *, actor: str | None = None) -> None:
        """删 skill。仓储会一并清掉全部频道关联。"""
        async with self._uow:
            await self._repo.delete(skill.id)
        await self._audit.record(
            GLOBAL_SCOPE,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "skill_delete", "skill": skill.name},
        )

    # ---- 附带文件 ----
    #
    # 校验（路径合法、大小上限、同 skill 内 path 唯一）留在路由层：那里能把每种
    # 失败映射成对应的 HTTP 状态码（422 / 409），而在服务层抛自定义异常再由路由
    # 翻译回来是多一层转手，本项目其余资源也都是在路由层校验（见 admin/mcp.py）。

    async def get_file(self, skill_id: str, file_id: str) -> SkillFile | None:
        return await self._repo.get_file(skill_id, file_id)

    async def find_file_by_path(self, skill_id: str, path: str) -> SkillFile | None:
        return await self._repo.find_file_by_path(skill_id, path)

    async def set_file(
        self,
        skill: Skill,
        *,
        path: str,
        description: str,
        content: str,
        file_id: str | None = None,
        actor: str | None = None,
    ) -> SkillFile:
        """新建或覆盖一个附带文件。

        建与改走同一个方法：两者的差别只有「id 是给的还是生成的」，而校验、
        审计、事务边界完全一样。拆成两个方法会让那三样各写两遍。
        """
        existing = await self._repo.get_file(skill.id, file_id) if file_id else None
        file = SkillFile(
            id=file_id or gen_id("skf"),
            skill_id=skill.id,
            path=path,
            description=description,
            content=content,
            # 保留原始创建时间：这是「改这个文件」而不是「换一个新文件」
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        async with self._uow:
            await self._repo.upsert_file(file)
        await self._audit.record(
            GLOBAL_SCOPE,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={
                "event": "skill_file_update" if existing else "skill_file_create",
                "skill": skill.name,
                "path": path,
                # 记字节数而非内容：审计表不该存业务数据的副本，但「这次改动
                # 让文件变多大」是排查 token 成本时要看的
                "size_bytes": file.size_bytes,
            },
        )
        return file

    async def delete_file(
        self,
        skill: Skill,
        file: SkillFile,
        *,
        actor: str | None = None,
    ) -> None:
        async with self._uow:
            await self._repo.delete_file(skill.id, file.id)
        await self._audit.record(
            GLOBAL_SCOPE,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "skill_file_delete", "skill": skill.name, "path": file.path},
        )

    async def list_for_channel(self, channel_instance_id: str) -> list[Skill]:
        """该频道当前可用的 skill（已关联 且 全局 enabled，含附带文件）。"""
        return await self._repo.list_for_channel(channel_instance_id)

    async def list_channel_skill_ids(self, channel_instance_id: str) -> list[str]:
        """该频道关联的 skill id，不过滤 enabled。供管理页回显勾选。"""
        return await self._repo.list_channel_skill_ids(channel_instance_id)

    async def set_channel_skills(
        self,
        channel_instance_id: str,
        skill_ids: list[str],
        *,
        actor: str | None = None,
    ) -> list[str]:
        """覆盖式设置频道启用的 skill。返回实际生效的 id 列表。

        不存在的 id 静默丢弃而非报错：管理页的勾选是基于它上一次拉到的列表，
        若期间有人删了某个 skill，提交时那个 id 就成了幽灵。为此报 422 会让
        用户面对一个自己无法理解也无法修正的错误（他勾的东西看着还在页面上），
        丢弃并返回实际结果则让前端刷新后自然收敛。
        """
        known = {s.id for s in await self._repo.list_all()}
        effective = [sid for sid in dict.fromkeys(skill_ids) if sid in known]
        async with self._uow:
            await self._repo.set_channel_skills(channel_instance_id, effective)
        await self._audit.record(
            channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "channel_skills_set", "count": len(effective), "skill_ids": effective},
        )
        return effective
