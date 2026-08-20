"""Skill 管理路由。

两组端点，作用域不同 —— 这是 skill 与其余资源（policy/预算/标签/MCP）的关键差别：

- ``/skills``：全局 skill 库的 CRUD。正文改一次，所有启用频道同时生效。
- ``/channels/{id}/skills``：该频道启用哪些（覆盖式设置一组 id）。

改动**不需要重启 worker**：skill 每次 agent run 从库里读。这与 MCP server 相反
（那边要在启动时建长连接），前端文案不要照抄。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import (
    skill_file_to_dict,
    skill_summary_to_dict,
    skill_to_dict,
)
from teamai.container import Container
from teamai.domain.models.skill import (
    DESCRIPTION_MAX_LEN,
    FILE_MAX_BYTES,
    FILE_PATH_PATTERN,
    NAME_PATTERN,
    is_safe_path,
)

_NAME_RE = re.compile(NAME_PATTERN)
_PATH_RE = re.compile(FILE_PATH_PATTERN)


def _validate_file(path: str, content: str) -> None:
    """附带文件的入库校验：路径形态与大小上限。

    大小按 UTF-8 字节算，与 ``SkillFile.size_bytes`` 一致 —— 按字符算会让
    中文文档在「看着没超」的情况下被拒。
    """
    if not _PATH_RE.fullmatch(path) or not is_safe_path(path):
        raise HTTPException(
            status_code=422,
            detail=(
                "path 只允许字母、数字、下划线、连字符、点与斜杠，"
                "不能为空、以 / 开头或结尾、或含 .. 段"
            ),
        )
    size = len(content.encode("utf-8"))
    if size > FILE_MAX_BYTES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"文件 {size} 字节，超出上限 {FILE_MAX_BYTES} 字节（64 KB）。"
                "每次 agent 调用都会预加载本频道全部启用技能的文件，故有此限制"
            ),
        )


def _validate(name: str, description: str) -> None:
    """入库前拦下 name 与 description 的格式问题。

    两者都直接影响模型行为：name 要被模型原样打进 ``load_skill(name)``，
    description 每次 run 都常驻系统提示词。
    """
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="name 只允许小写字母、数字与连字符（模型要照这个名字调 load_skill）",
        )
    if not description.strip():
        raise HTTPException(
            status_code=422,
            detail="description 必填 —— 它是模型判断该不该用这个技能的唯一依据",
        )
    if len(description) > DESCRIPTION_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=(
                f"description 不得超过 {DESCRIPTION_MAX_LEN} 字："
                "它每次调用都常驻系统提示词，详细步骤应写在正文里"
            ),
        )


def build_skill_router(container: Container) -> APIRouter:
    router = APIRouter()

    # ---- 全局 skill 库 ----

    @router.get("/skills")
    async def list_skills() -> list[dict[str, Any]]:
        """全局 skill 库。带正文 —— 管理页要能直接编辑。"""
        return [skill_to_dict(s) for s in await container.skills.list_all()]

    @router.post("/skills")
    async def create_skill(body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name", "")
        description = body.get("description", "")
        _validate(name, description)
        if await container.skills.find_by_name(name):
            raise HTTPException(status_code=409, detail="已存在同名技能")

        skill = await container.skills.create(
            name,
            description,
            body.get("content") or "",
            enabled=bool(body.get("enabled", True)),
            actor=body.get("actor"),
        )
        return skill_to_dict(skill)

    @router.put("/skills/{skill_id}")
    async def update_skill(skill_id: str, body: dict[str, Any]) -> dict[str, Any]:
        skill = await container.skills.get(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="技能不存在")

        # 改名要重查唯一性。改名会让引用旧名的地方失效 —— 但 skill 没有「白名单
        # 残留」问题（模型每次都从当前清单里读名字），故允许改，不像 MCP server
        # 的 name 那样锁死。
        name = body.get("name")
        description = body.get("description")
        if name is not None or description is not None:
            _validate(
                name if name is not None else skill.name,
                description if description is not None else skill.description,
            )
        if name is not None and name != skill.name:
            existing = await container.skills.find_by_name(name)
            if existing is not None and existing.id != skill_id:
                raise HTTPException(status_code=409, detail="已存在同名技能")

        updated = await container.skills.update(
            skill,
            name=name,
            description=description,
            content=body.get("content"),
            enabled=body.get("enabled"),
            actor=body.get("actor"),
        )
        return skill_to_dict(updated)

    @router.delete("/skills/{skill_id}")
    async def delete_skill(skill_id: str, actor: str | None = None) -> dict[str, Any]:
        skill = await container.skills.get(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="技能不存在")
        await container.skills.delete(skill, actor=actor)
        return {"ok": True}

    # ---- 附带文件 ----
    #
    # 文件挂在 skill 下（``/skills/{id}/files``），不是独立资源：一个文件脱离
    # 它的 skill 没有意义，且 path 的唯一性是「同一 skill 内」而非全局。

    async def _require_skill(skill_id: str):
        skill = await container.skills.get(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="技能不存在")
        return skill

    @router.get("/skills/{skill_id}/files/{file_id}")
    async def get_file(skill_id: str, file_id: str) -> dict[str, Any]:
        """单个文件，**带内容**。列表接口只给摘要，编辑时经此单取。"""
        await _require_skill(skill_id)
        file = await container.skills.get_file(skill_id, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        return skill_file_to_dict(file)

    @router.post("/skills/{skill_id}/files")
    async def create_file(skill_id: str, body: dict[str, Any]) -> dict[str, Any]:
        skill = await _require_skill(skill_id)
        path = body.get("path", "")
        content = body.get("content") or ""
        _validate_file(path, content)
        if await container.skills.find_file_by_path(skill_id, path):
            raise HTTPException(status_code=409, detail="该技能已存在同路径的文件")

        file = await container.skills.set_file(
            skill,
            path=path,
            description=body.get("description") or "",
            content=content,
            actor=body.get("actor"),
        )
        return skill_file_to_dict(file)

    @router.put("/skills/{skill_id}/files/{file_id}")
    async def update_file(skill_id: str, file_id: str, body: dict[str, Any]) -> dict[str, Any]:
        skill = await _require_skill(skill_id)
        existing = await container.skills.get_file(skill_id, file_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="文件不存在")

        path = body.get("path", existing.path)
        content = body.get("content", existing.content)
        _validate_file(path, content)
        if path != existing.path:
            clash = await container.skills.find_file_by_path(skill_id, path)
            if clash is not None and clash.id != file_id:
                raise HTTPException(status_code=409, detail="该技能已存在同路径的文件")

        file = await container.skills.set_file(
            skill,
            path=path,
            description=body.get("description", existing.description),
            content=content,
            file_id=file_id,
            actor=body.get("actor"),
        )
        return skill_file_to_dict(file)

    @router.delete("/skills/{skill_id}/files/{file_id}")
    async def delete_file(
        skill_id: str, file_id: str, actor: str | None = None
    ) -> dict[str, Any]:
        skill = await _require_skill(skill_id)
        file = await container.skills.get_file(skill_id, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        await container.skills.delete_file(skill, file, actor=actor)
        return {"ok": True}

    # ---- 频道启用关系 ----

    @router.get("/channels/{channel_instance_id}/skills")
    async def list_channel_skills(channel_instance_id: str) -> dict[str, Any]:
        """全局库 + 该频道的勾选状态，一次返回。

        合成一个响应而非让前端打两次：管理页要渲染的是「全部技能，其中这些打勾」，
        分两次取则中间态下（另一人正在增删技能）会出现勾选指向不存在的行。

        ``enabled_ids`` **不过滤全局 enabled**：它答的是「这个频道勾了哪些」。
        全局停用的技能在此仍带勾，但 ``skills[].enabled`` 为 false，前端据此
        显示成「已停用」而非把勾去掉 —— 否则管理员会以为关联关系丢了。
        """
        skills = await container.skills.list_all()
        enabled_ids = await container.skills.list_channel_skill_ids(channel_instance_id)
        return {
            # 频道页只需要名字与描述来做勾选，正文是几 KB 的散文，不必带
            "skills": [skill_summary_to_dict(s) for s in skills],
            "enabled_ids": enabled_ids,
        }

    @router.put("/channels/{channel_instance_id}/skills")
    async def set_channel_skills(
        channel_instance_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """覆盖式设置该频道启用的技能。

        入参是最终应启用的完整 id 列表。不存在的 id 会被静默丢弃，
        返回值里的 ``enabled_ids`` 是实际生效的集合（见 SkillService 的说明）。
        """
        raw = body.get("skill_ids")
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            raise HTTPException(status_code=422, detail="skill_ids 必须是字符串数组")

        effective = await container.skills.set_channel_skills(
            channel_instance_id, raw, actor=body.get("actor")
        )
        return {"enabled_ids": effective}

    return router
