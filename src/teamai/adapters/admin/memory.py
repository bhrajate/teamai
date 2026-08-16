"""记忆管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import memory_to_dict
from teamai.container import Container
from teamai.domain.models import MemoryType


def _parse_type(raw: object) -> MemoryType:
    """把请求里的 type 解析成枚举，非法值立即 400。

    与蒸馏解析（distiller._parse_entries 把未知类型归入 BACKGROUND_KNOWLEDGE）
    刻意不同：那边宽容是因为模型输出不可控、丢内容比分错类更糟；这边是人在
    调接口，静默改成别的类型只会让人以为自己设对了。
    """
    try:
        return MemoryType[str(raw).upper()]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"type 非法：{raw!r}，可选 {[t.name for t in MemoryType]}",
        ) from None


def build_memory_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/memories")
    async def list_memories(
        channel_instance_id: str, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """列出该频道的记忆。默认只给现行事实。

        `include_superseded=true` 连已被取代的一起返回 —— 「这条事实之前是什么」
        只有这样才看得到，而这是排查「机器人为什么这么说」的主要线索。默认关
        是因为控制台的日常用途是看「现在记着什么」，混入历史版本会让同一件事
        出现多条、看起来像没去重。
        """
        entries = await container.memory.list(
            channel_instance_id, current_only=not include_superseded
        )
        return [memory_to_dict(e) for e in entries]

    @router.post("/channels/{channel_instance_id}/memories")
    async def create_memory(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        kwargs: dict[str, Any] = {}
        if body.get("type") is not None:
            kwargs["type"] = _parse_type(body["type"])
        entry = await container.memory.store(
            channel_instance_id, content, source_user_id=body.get("user_id"), **kwargs
        )
        return memory_to_dict(entry)

    @router.patch("/memories/{entry_id}")
    async def update_memory(entry_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """改内容与/或类型。

        这是「这条记忆写错了」的路径（笔误、措辞），改完仍是同一条事实，保留
        id 与 created_at。「事实变了」是另一回事 —— 那由蒸馏的 UPDATE 动作走
        supersede，新写一条并把旧条目标记为已取代，两条都留着可查。

        改内容会触发向量重算（见 MemoryService.edit）。
        """
        content = body.get("content")
        raw_type = body.get("type")
        if content is None and raw_type is None:
            raise HTTPException(status_code=400, detail="至少要改 content 或 type 之一")
        if content is not None and not str(content).strip():
            raise HTTPException(status_code=400, detail="content 不能为空")

        entry = await container.memory.edit(
            entry_id,
            content=content,
            type=_parse_type(raw_type) if raw_type is not None else None,
            actor=body.get("actor"),
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        return memory_to_dict(entry)

    @router.delete("/memories/{entry_id}")
    async def delete_memory(entry_id: str, actor: str | None = None) -> dict[str, str]:
        await container.memory.delete(entry_id, actor=actor)
        return {"status": "deleted"}

    return router
