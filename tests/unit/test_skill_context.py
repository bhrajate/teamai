"""skill 在上下文、系统提示词与 router 里的贯通。

这一层最容易出静默缺陷：漏一处不报错，只是模型看不到技能。三个回归点：

- ``ContextBundle.compact()`` 必须重建 ``skills``（漏了则「聊得久的线程里技能
  突然全部消失」，短线程一切正常）
- 系统提示词只出 ``name: description``，不出正文
- router 按频道取 skill 并放进 bundle
"""

from __future__ import annotations

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.prompts import build_system_prompt
from teamai.application.agent.runtime import StageStatus
from teamai.application.router import MessageRouter
from teamai.domain.models import ChannelInstance, PermissionPolicy, Skill, SkillFile
from teamai.domain.ports import ThreadMessage
from tests.doubles import (
    FakeChannels,
    FakeConversation,
    FakeDistiller,
    FakeIntentClassifier,
    FakeMemory,
    FakePolicyRepo,
    FakeRuntime,
    FakeTags,
    mention,
)


def _instance() -> ChannelInstance:
    return ChannelInstance(
        id="ch1",
        platform="slack",
        channel_id="C1",
        workspace_id="W1",
        agent_identity="teamai",
    )


def _skill(name: str = "code-review", content: str = "# 详细步骤\n\n1. 看 diff", **kw) -> Skill:
    return Skill(
        id=f"skill_{name}",
        name=name,
        description=kw.pop("description", f"{name} 的适用场景"),
        content=content,
        **kw,
    )


def _bundle(skills: list[Skill] | None = None, history: list[ThreadMessage] | None = None):
    return ContextBundle(
        task_id="t1",
        channel_instance_id="ch1",
        user_prompt="看下这个 PR",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=_instance(),
        policy=PermissionPolicy(id="p1", channel_instance_id="ch1", allowed_tools=[]),
        thread_history=history or [],
        skills=skills or [],
    )


# ---- ContextBundle ----


def test_skill_catalog只给名字与描述() -> None:
    cat = _bundle([_skill()]).skill_catalog
    assert cat == "- code-review: code-review 的适用场景"
    assert "详细步骤" not in cat


def test_skill_catalog每技能一行() -> None:
    cat = _bundle([_skill("a", description="A 的场景"), _skill("b", description="B 的场景")])
    assert cat.skill_catalog.splitlines() == ["- a: A 的场景", "- b: B 的场景"]


def test_无技能时catalog为空串() -> None:
    assert _bundle().skill_catalog == ""


def test_skill_ref_names留痕() -> None:
    """留的是「当时挂了哪些可选项」，不是「模型实际载入了哪几个」。

    排查「为什么没用某个技能」时，先要能区分「它没挂上」与「挂上了但模型
    判断不相关」—— 这个字段答的是前者。
    """
    assert _bundle([_skill("a"), _skill("b")]).skill_ref_names == ["a", "b"]


def test_compact保留skills() -> None:
    """回归点。compact() 逐字段重建 bundle，漏掉 skills 的表现极隐蔽：
    压缩只在历史超阈值时发生，于是长线程里技能全部消失、短线程正常。
    """
    history = [ThreadMessage(author_id="u1", text=f"第 {i} 条") for i in range(10)]
    bundle = _bundle([_skill()], history=history)

    compacted = bundle.compact(max_history=3, summary_threshold=100)

    assert compacted.dropped_history == 7
    assert [s.name for s in compacted.skills] == ["code-review"]


def test_compact不修改原bundle() -> None:
    history = [ThreadMessage(author_id="u1", text=f"第 {i} 条") for i in range(10)]
    bundle = _bundle([_skill()], history=history)

    bundle.compact(max_history=3, summary_threshold=100).skills.clear()

    assert len(bundle.skills) == 1


def test_未超阈值时compact原样返回() -> None:
    bundle = _bundle([_skill()])
    assert bundle.compact(60, 120) is bundle


# ---- 系统提示词 ----


def _prompt(catalog: str = "", **kw) -> str:
    return build_system_prompt(
        _instance(),
        PermissionPolicy(id="p1", channel_instance_id="ch1", allowed_tools=["github"]),
        skill_catalog=catalog,
        **kw,
    )


def test_提示词含技能清单与使用规则() -> None:
    out = _prompt("- code-review: 审查 PR")

    assert "可用技能" in out
    assert "- code-review: 审查 PR" in out
    # 必须交代「先载入再执行」，否则模型会照着一行描述硬做
    assert "load_skill" in out


def test_提示词交代不相关别载入() -> None:
    """否则模型会把清单里的技能全载一遍，渐进式披露退化成全量注入。"""
    out = _prompt("- code-review: 审查 PR")
    assert "无关" in out or "不要载入" in out


def test_无技能时提示词不出现技能段() -> None:
    out = _prompt("")
    assert "可用技能" not in out
    assert "load_skill" not in out


def test_技能清单在标签三项之前() -> None:
    """技能是频道的常备能力，标签三项是本次调用的具体设定 —— 后者更贴近当前
    任务，理应压在前者之上。"""
    out = _prompt("- code-review: 审查 PR", role="资深工程师", tag_instruction="只看安全问题")

    assert out.index("可用技能") < out.index("角色设定")
    assert out.index("可用技能") < out.index("当前任务预设指令")


def test_提示词不含技能正文() -> None:
    """正文只在 load_skill 之后进上下文。"""
    out = _prompt("- code-review: 审查 PR")
    assert "详细步骤" not in out


# ---- router ----


class FakeSkills:
    """按频道返回 skill，并记下被问过哪些频道。"""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self.skills = skills or []
        self.asked: list[str] = []

    async def list_for_channel(self, channel_instance_id: str) -> list[Skill]:
        self.asked.append(channel_instance_id)
        return self.skills


def _router(runtime: FakeRuntime, skills: FakeSkills | None = None) -> MessageRouter:
    from teamai.application.budget import BudgetController
    from teamai.application.orchestrator import TaskOrchestrator
    from teamai.domain.services import AuditLogWriter
    from tests.fakes import (
        FakeAuditRepository,
        FakeBudgetRepository,
        FakeTaskQueue,
        FakeTaskRepository,
    )

    audit = AuditLogWriter(FakeAuditRepository())
    return MessageRouter(
        orchestrator=TaskOrchestrator(FakeTaskRepository(), audit, FakeTaskQueue()),
        intent=FakeIntentClassifier("query"),
        tags=FakeTags(),
        memory=FakeMemory(),
        budget=BudgetController(FakeBudgetRepository(), audit),
        runtime=runtime,
        channels=FakeChannels(_instance()),
        policy_repo=FakePolicyRepo(),
        conversation=FakeConversation(),
        distiller=FakeDistiller(),
        skills=skills,
    )


async def test_router把频道技能放进bundle() -> None:
    runtime = FakeRuntime()
    skills = FakeSkills([_skill()])

    await _router(runtime, skills).route(mention("看下这个 PR"))

    assert skills.asked == ["ch1"]
    (bundle,) = runtime.bundles
    assert [s.name for s in bundle.skills] == ["code-review"]


async def test_router把清单写进系统提示词() -> None:
    runtime = FakeRuntime()

    await _router(runtime, FakeSkills([_skill()])).route(mention("看下这个 PR"))

    (bundle,) = runtime.bundles
    assert "可用技能" in bundle.system_prompt
    assert "- code-review: code-review 的适用场景" in bundle.system_prompt
    # 正文不进常驻提示词
    assert "详细步骤" not in bundle.system_prompt


async def test_未装配skill服务时照常运行() -> None:
    """窄装配与测试场景：技能能力整体不出现，任务照跑。"""
    runtime = FakeRuntime()

    decision = await _router(runtime, None).route(mention("看下这个 PR"))

    assert decision.handler == "respond"
    (bundle,) = runtime.bundles
    assert bundle.skills == []
    assert "可用技能" not in bundle.system_prompt


async def test_文件随技能一起进bundle() -> None:
    """read_skill_file 靠 bundle 里的内容做内存查表，工具执行时不能碰库。"""
    runtime = FakeRuntime()
    s = _skill()
    s.files = [
        SkillFile(id="skf_1", skill_id=s.id, path="a.md", description="用途", content="内容")
    ]

    await _router(runtime, FakeSkills([s])).route(mention("看下这个 PR"))

    (bundle,) = runtime.bundles
    assert [f.path for f in bundle.skills[0].files] == ["a.md"]
    assert bundle.skills[0].files[0].content == "内容"


async def test_留痕带技能名() -> None:
    runtime = FakeRuntime(status=StageStatus.DONE)

    await _router(runtime, FakeSkills([_skill("a"), _skill("b")])).route(mention("看下 PR"))

    (bundle,) = runtime.bundles
    assert bundle.skill_ref_names == ["a", "b"]
