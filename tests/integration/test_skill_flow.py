"""skill 全链路：真 SQLite → 服务 → 提示词 → 工具 → 模型。

单元测试各自只覆盖一层，接缝处的错配（仓储少带文件、提示词漏拼清单、工具挂不上）
在那些测试里都是绿的。这里把三级渐进式披露串起来跑一遍：

1. 建 skill + 附带文件，在某频道启用
2. 系统提示词里只出现 name + description（第 1 级）
3. 模型调 load_skill → 拿到正文 + 文件清单，但**没有**文件内容（第 2 级）
4. 模型调 read_skill_file → 拿到文件内容（第 3 级）

外加两条隔离：未启用的频道拿不到；全局停用后所有频道立刻失效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from teamai.adapters.admin.serializers import skill_to_dict
from teamai.application.agent.prompts import build_system_prompt
from teamai.application.skill import SkillService
from teamai.domain.models import GLOBAL_SCOPE, AuditLog, ChannelInstance, PermissionPolicy
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.db import Base
from teamai.infrastructure.repositories.skill import SQLSkillRepository
from teamai.infrastructure.tools.registry import ToolRegistry

CH = "ch_01K5X"


class _Uow:
    def __init__(self, session) -> None:
        self._s = session

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, exc_type, *_: object) -> None:
        if exc_type is None:
            await self._s.commit()
        else:
            await self._s.rollback()


class _AuditRepo:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)


@pytest_asyncio.fixture
async def svc() -> AsyncIterator[tuple[SkillService, _AuditRepo]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        audit = _AuditRepo()
        yield SkillService(SQLSkillRepository(s), AuditLogWriter(audit), _Uow(s)), audit
    await engine.dispose()


def _instance() -> ChannelInstance:
    return ChannelInstance(
        id=CH, platform="slack", channel_id="C1", workspace_id="W1", agent_identity="teamai"
    )


class _Script:
    """按脚本依次发起若干次工具调用，最后收尾。"""

    __name__ = "script"

    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        self.calls = calls
        self.seen_tools: list[str] = []
        self._i = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen_tools = sorted(t.name for t in info.function_tools)
        if self._i < len(self.calls):
            name, args = self.calls[self._i]
            self._i += 1
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])


def _returns(result) -> list[str]:
    return [
        str(p.content)
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]


async def test_三级渐进式披露全链路(svc) -> None:
    service, audit = svc

    # --- 1. 建 skill + 文件，在频道启用 ---
    review = await service.create(
        "code-review",
        "按团队 Go 规范审查 PR，产出分级问题清单",
        "# 审查步骤\n\n1. 先看 diff 整体结构\n2. 再逐项对照清单",
        actor="admin",
    )
    await service.set_file(
        review,
        path="checklist.md",
        description="逐项检查清单",
        content="- 错误是否都被处理\n- 是否有并发写共享 map",
        actor="admin",
    )
    # 另一个 skill，不在本频道启用 —— 用来验证隔离
    await service.create("weekly-report", "从 git log 生成周报", "# 周报步骤", actor="admin")

    await service.set_channel_skills(CH, [review.id], actor="admin")

    skills = await service.list_for_channel(CH)
    assert [s.name for s in skills] == ["code-review"]
    # 仓储必须把文件连内容一起带出来 —— 工具执行时不能再查库
    assert [f.path for f in skills[0].files] == ["checklist.md"]
    assert "并发写共享 map" in skills[0].files[0].content

    # --- 2. 第 1 级：系统提示词只有 name + description ---
    prompt = build_system_prompt(
        _instance(),
        PermissionPolicy(id="p1", channel_instance_id=CH, allowed_tools=[]),
        skill_catalog="\n".join(s.catalog_line for s in skills),
    )
    assert "- code-review: 按团队 Go 规范审查 PR，产出分级问题清单" in prompt
    assert "审查步骤" not in prompt, "正文不该进常驻提示词"
    assert "checklist.md" not in prompt, "文件清单不该进常驻提示词"
    assert "weekly-report" not in prompt, "未启用的技能不该出现"

    # --- 3/4. 第 2、3 级：两次工具往返 ---
    registry = ToolRegistry()
    script = _Script(
        [
            ("load_skill", {"name": "code-review"}),
            ("read_skill_file", {"skill": "code-review", "path": "checklist.md"}),
        ]
    )
    agent = Agent(
        FunctionModel(script),
        instructions=prompt,
        toolsets=[registry.for_channel([], skills)],
    )
    result = await agent.run("看下这个 PR")

    assert script.seen_tools == ["load_skill", "read_skill_file"]
    loaded, file_read = _returns(result)

    # 第 2 级：正文 + 文件清单，但没有文件内容
    assert "1. 先看 diff 整体结构" in loaded
    assert "checklist.md" in loaded
    assert "逐项检查清单" in loaded
    assert "并发写共享 map" not in loaded, "文件内容不该内联进 load_skill"

    # 第 3 级：文件内容
    assert "并发写共享 map" in file_read

    # --- 审计：全局变更归 GLOBAL_SCOPE，频道启用归频道 ---
    by_scope: dict[str, list[str]] = {}
    for log in audit.logs:
        by_scope.setdefault(log.channel_instance_id, []).append(log.detail["event"])
    assert by_scope[GLOBAL_SCOPE] == [
        "skill_create",
        "skill_file_create",
        "skill_create",
    ]
    assert by_scope[CH] == ["channel_skills_set"]


async def test_未启用的频道拿不到(svc) -> None:
    service, _ = svc
    s = await service.create("code-review", "审查 PR", "# 步骤")
    await service.set_channel_skills(CH, [s.id])

    assert await service.list_for_channel("ch_other") == []


async def test_全局停用后所有频道立刻失效(svc) -> None:
    """不需要重启，也不需要去各频道取消勾选 —— 这是「写坏了先下线」的入口。"""
    service, _ = svc
    s = await service.create("code-review", "审查 PR", "# 步骤")
    await service.set_channel_skills(CH, [s.id])
    assert len(await service.list_for_channel(CH)) == 1

    await service.update(s, enabled=False)

    assert await service.list_for_channel(CH) == []
    # 但勾选记录留着：否则管理页会显示成关联关系丢了
    assert await service.list_channel_skill_ids(CH) == [s.id]


async def test_改正文对已启用频道即时生效(svc) -> None:
    """与 MCP server 相反：那边要重启 worker 才装载，skill 每次 run 从库里读。"""
    service, _ = svc
    s = await service.create("code-review", "审查 PR", "# 旧步骤")
    await service.set_channel_skills(CH, [s.id])

    await service.update(s, content="# 新步骤")

    (got,) = await service.list_for_channel(CH)
    assert got.content == "# 新步骤"


async def test_删skill清掉文件与频道关联(svc) -> None:
    service, _ = svc
    s = await service.create("code-review", "审查 PR", "# 步骤")
    await service.set_file(s, path="a.md", description="用途", content="内容")
    await service.set_channel_skills(CH, [s.id])

    await service.delete(s)

    assert await service.list_for_channel(CH) == []
    assert await service.list_channel_skill_ids(CH) == []
    assert await service.get_file(s.id, "a.md") is None


async def test_序列化形状不含文件内容(svc) -> None:
    """列表响应若带上每个文件的内容，会膨胀到 64 KB × 文件数 × 技能数。"""
    service, _ = svc
    s = await service.create("code-review", "审查 PR", "# 步骤")
    await service.set_file(s, path="a.md", description="用途", content="不该出现在列表里")

    (dumped,) = [skill_to_dict(x) for x in await service.list_all()]

    assert dumped["content"] == "# 步骤"
    assert "不该出现在列表里" not in str(dumped)
    assert set(dumped["files"][0]) == {"id", "skill_id", "path", "description", "size_bytes"}
