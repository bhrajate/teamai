"""记忆管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import memory_to_dict
from teamai.application.memory import ConflictCheck
from teamai.config import settings
from teamai.container import Container
from teamai.domain.models import AuditAction, MemorySource, MemoryType


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


def _conflict_detail(check: ConflictCheck) -> dict[str, Any]:
    """把冲突检查结果拼成 409 的 detail。

    `message` 单独给一句人话：前端的 `readDetail` 对字符串 detail 有现成处理，
    而结构化 detail 若不带这个字段，页面只能显示「请求失败（HTTP 409）」。

    `degraded` 必须透出。未配 embedding 时这道检查只能查出字面重复，录入人得
    知道自己拿到的是什么 —— 不说的话，「没报冲突」会被读成「确认没冲突」。
    """
    if check.degraded:
        message = (
            f"发现 {len(check.conflicts)} 条字面重复的现行记忆。"
            "未配置 embedding，只能做字面比对，语义上矛盾的记忆查不出来。"
        )
    else:
        message = f"发现 {len(check.conflicts)} 条疑似说同一件事的现行记忆。"
    return {
        "message": message,
        "degraded": check.degraded,
        "conflicts": [
            {
                "entry": memory_to_dict(c.entry),
                # 字面比对路径下没有相似度，给 null 而不是编一个数 —— 前端据此
                # 显示「字面重复」而不是一个假的百分比。
                "score": c.score,
            }
            for c in check.conflicts
        ],
    }


def build_memory_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/embedding")
    async def embedding_state() -> dict[str, Any]:
        """embedder 是否可用。控制台据此在记忆页提示降级。

        为什么不放 `/health`：那个端点**匿名可打**（探针与 make verify-* 要用），
        而「这个部署有没有配 embedding」是运营信息。放进去等于为了省一个端点而把
        匿名面扩宽一点，而 README 里刚说了 `/metrics` 暴露运营信息应在反代限制来源
        —— 两处的取舍该一致。故跟 `/tools` 一路：受令牌保护的只读非资源端点。

        为什么控制台需要它而不是看日志：那条 warning 只在启动时打一次，滚掉之后
        没人知道；而降级的后果（记忆库持续劣化）要几周才从回答质量上看出来。
        """
        embedder = container.embedder
        return {
            "available": embedder.available,
            # 配了哪个模型。「检索质量怎么变差了」的第一个问题就是这个 ——
            # 换过模型而没重建索引时，向量是旧模型算的（对账查不出，见
            # scripts/rebuild_memory_vectors.py）。
            "model": settings.embedding_model or None,
            "dimensions": embedder.dimensions,
        }

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
        """手工写入一条记忆，写前查冲突。

        这条路此前完全不过冲突检查：人在控制台写「超时 5 秒」时库里有蒸馏出的
        「超时 3 秒」，两条直接并列成为现行，而**没有任何一处会发现**。蒸馏侧的
        去重只覆盖它自己那条路。

        三种结果：

        - 疑似冲突且录入人未表态 → **409**，带候选列表，不写库
        - `supersede_id` → 取代那条（新写一条 + 给旧条目打 superseded_by）
        - `force: true` → 明确要并列存在，跳过检查直接写

        为什么是 409 而不是「写进去再警告」：警告没人看，而这里要的正是让人当场
        决定。为什么不自动取代：凭一句待写入的话判不出「新版本」还是「另一件事」，
        而错误取代会作废一条正确的记忆 —— 这个判断该由人做，理由同蒸馏侧不给
        DELETE（见 DistillAction 的文档）。
        """
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")

        supersede_id = body.get("supersede_id")
        force = bool(body.get("force"))
        if supersede_id and force:
            # 不定优先级：两者表达相反的意图（取代那条 / 就是要并列），同时给
            # 说明调用方自己没想清楚。静默择一会让另一半意图无声地丢掉。
            raise HTTPException(
                status_code=400, detail="supersede_id 与 force 不能同时给：要取代还是要并列？"
            )

        mem_type = _parse_type(body["type"]) if body.get("type") is not None else None
        kwargs: dict[str, Any] = {} if mem_type is None else {"type": mem_type}

        if supersede_id:
            entry = await container.memory.supersede(
                str(supersede_id),
                channel_instance_id,
                content,
                source=MemorySource.MANUAL,
                action=AuditAction.MEMORY_STORE,
                **kwargs,
            )
            if entry is None:
                # supersede 对「不存在」与「不属本频道」都返回 None。合成一条
                # 400 而不是 404：两者对调用方是同一件事（这个 id 在这里用不了），
                # 而分开报会把「B 频道有这个 id」透给 A 频道的调用方。
                raise HTTPException(
                    status_code=400,
                    detail=f"要取代的记忆 {supersede_id} 不存在，或不属于本频道",
                )
            return memory_to_dict(entry)

        if not force:
            check = await container.memory.find_conflicts(
                channel_instance_id,
                content,
                **({} if mem_type is None else {"type": mem_type}),
            )
            if check:
                raise HTTPException(status_code=409, detail=_conflict_detail(check))

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
