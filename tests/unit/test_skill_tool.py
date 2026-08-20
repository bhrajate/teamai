"""load_skill 工具与 ToolRegistry 的 skill 挂载。

覆盖渐进式披露的取回端：清单里只有 name+description，正文靠这个工具取。
用 pydantic-ai 的 Tool 真实调用路径（不直接调闭包里的函数），从而工具名、
参数 schema 与 ModelRetry 的传播都被验到。
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, ModelRetry, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from teamai.domain.models.skill import Skill, SkillFile
from teamai.infrastructure.tools.registry import ToolRegistry
from teamai.infrastructure.tools.skill_tool import build_skill_file_tool, build_skill_tool


def _skill(
    name: str = "code-review",
    content: str = "# 步骤\n\n1. 看 diff\n2. 提问题",
    files: list[SkillFile] | None = None,
) -> Skill:
    return Skill(
        id=f"skill_{name}",
        name=name,
        description=f"{name} 的适用场景",
        content=content,
        files=files or [],
    )


def _file(path: str = "checklist.md", content: str = "- 检查一\n- 检查二", **kw) -> SkillFile:
    return SkillFile(
        id=kw.pop("id", f"skf_{path}"),
        skill_id=kw.pop("skill_id", "skill_code-review"),
        path=path,
        description=kw.pop("description", f"{path} 的用途"),
        content=content,
    )


def _fn(tool: Tool):
    """取出 Tool 里的实际函数。

    pydantic-ai 的 Tool 把函数存在 .function 上；测试直接调它，
    绕开构造 RunContext 的开销（这个工具不吃 ctx）。
    """
    return tool.function


def test_工具名与描述() -> None:
    """名字是模型要打出来的，且描述里必须交代「先载入再执行」。"""
    tool = build_skill_tool([_skill()])
    assert tool.name == "load_skill"
    assert tool.description is not None
    assert "载入" in tool.description


async def test_按名取回正文() -> None:
    tool = build_skill_tool([_skill()])

    out = await _fn(tool)("code-review")

    assert "1. 看 diff" in out
    # 带上技能名：一次 run 里可能载入多个，模型要能分清哪段属于哪个
    assert "code-review" in out


async def test_正文不做json包装() -> None:
    """有意偏离本项目 ok()/fail() 的约定，理由见 skill_tool.py。

    锁住它：若日后有人「为了统一」套上 ok()，几千字 Markdown 里的每个换行
    都会变成 \\n 字面量，既多花 token 又更难读。
    """
    tool = build_skill_tool([_skill(content="第一行\n第二行")])

    out = await _fn(tool)("code-review")

    assert "第一行\n第二行" in out
    assert '\\n' not in out
    assert not out.lstrip().startswith("{")


async def test_名字不存在时回灌有效取值() -> None:
    """用 ModelRetry 而非 fail()：清单就在模型上下文里，它能自己改对。"""
    tool = build_skill_tool([_skill("code-review"), _skill("weekly-report")])

    with pytest.raises(ModelRetry) as exc:
        await _fn(tool)("code_review")  # 下划线写错成连字符

    msg = str(exc.value)
    assert "code-review" in msg
    assert "weekly-report" in msg


async def test_多个技能各自可取() -> None:
    tool = build_skill_tool([_skill("a", content="AAA"), _skill("b", content="BBB")])

    assert "AAA" in await _fn(tool)("a")
    assert "BBB" in await _fn(tool)("b")


# ---- ToolRegistry 挂载 ----


class _Recorder:
    """记下模型看到的工具定义，并可按脚本发起一次工具调用。

    与 test_tools.py 同一套做法：断言的是「模型实际收到了什么」，
    而非工具集的内部结构 —— 后者随 pydantic-ai 版本变动，前者才是契约。
    """

    __name__ = "recorder"

    def __init__(self, call: tuple[str, dict] | None = None) -> None:
        self.tools: list[ToolDefinition] = []
        self._call = call
        self._step = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.tools = list(info.function_tools)
        self._step += 1
        if self._call is not None and self._step == 1:
            return ModelResponse(parts=[ToolCallPart(self._call[0], self._call[1])])
        return ModelResponse(parts=[TextPart("done")])


async def _run(
    registry: ToolRegistry,
    allowed: list[str],
    skills: list[Skill] | None = None,
    call: tuple[str, dict] | None = None,
):
    toolset = registry.for_channel(allowed, skills)
    recorder = _Recorder(call)
    agent = Agent(FunctionModel(recorder), toolsets=[toolset] if toolset is not None else None)
    result = await agent.run("go")
    return recorder, result


def _names(recorder: _Recorder) -> set[str]:
    return {t.name for t in recorder.tools}


async def test_有skill时模型看得到load_skill() -> None:
    registry = ToolRegistry()
    registry.register(Tool(_dummy, name="github"))

    recorder, _ = await _run(registry, ["github"], [_skill()])

    assert _names(recorder) == {"github", "load_skill"}


async def test_白名单为空但有skill时仍挂load_skill() -> None:
    """频道启用 skill 即授权，不需要在策略页再补一条 —— 同一件事配两处必有漏配，
    而漏配的表现是「技能明明启用了，模型却说没有这个能力」。"""
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill()])

    assert _names(recorder) == {"load_skill"}


async def test_无skill时不挂load_skill() -> None:
    """挂一个什么都载不到的工具只会诱导模型做无谓往返。"""
    registry = ToolRegistry()
    registry.register(Tool(_dummy, name="github"))

    recorder, _ = await _run(registry, ["github"], [])

    assert _names(recorder) == {"github"}


def test_两者都空时不挂工具() -> None:
    registry = ToolRegistry()
    assert registry.for_channel([], []) is None
    assert registry.for_channel([], None) is None


async def test_skills参数可省略保持向后兼容() -> None:
    """现有调用点（含测试）不传 skills，行为须与改动前一致。"""
    registry = ToolRegistry()
    registry.register(Tool(_dummy, name="github"))
    recorder = _Recorder()
    agent = Agent(FunctionModel(recorder), toolsets=[registry.for_channel(["github"])])

    await agent.run("go")

    assert _names(recorder) == {"github"}


async def test_load_skill的schema有name参数() -> None:
    """模型要能看出该传什么。schema 空的话它只能靠描述里的散文猜。"""
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill()])

    (tool,) = recorder.tools
    assert "name" in tool.parameters_json_schema["properties"]


async def test_模型调用load_skill能拿到正文() -> None:
    """端到端：模型发起工具调用 → 正文回到对话里。

    这是渐进式披露真正要成立的那一步 —— 前面的测试只验证工具挂上了。
    """
    registry = ToolRegistry()

    _, result = await _run(
        registry,
        [],
        [_skill(content="# 步骤\n\n1. 先看 diff")],
        call=("load_skill", {"name": "code-review"}),
    )

    returns = [
        p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert len(returns) == 1
    assert "1. 先看 diff" in str(returns[0].content)


async def test_模型传错名字时收到重试提示() -> None:
    """ModelRetry 应变成 RetryPromptPart 回到模型，而不是让整个 run 失败。"""
    registry = ToolRegistry()

    _, result = await _run(
        registry,
        [],
        [_skill("code-review")],
        call=("load_skill", {"name": "不存在的技能"}),
    )

    retries = [
        p for m in result.all_messages() for p in m.parts if isinstance(p, RetryPromptPart)
    ]
    assert len(retries) == 1
    assert "code-review" in str(retries[0].content)


def test_load_skill不进全局注册表() -> None:
    """它闭包捕获了本次 run 的 skill 集合，存进全局表会让下一个频道拿到
    上一个频道的技能。"""
    registry = ToolRegistry()

    registry.for_channel([], [_skill()])

    assert "load_skill" not in registry.names


async def test_不同频道拿到各自的技能() -> None:
    """同一个 registry 连续为两个频道裁剪，第二次不该看到第一次的技能。

    这是「工具全局注册、skill 按频道」这对矛盾的核心风险点：若 load_skill 被
    存进全局表，B 频道会载入到 A 频道的技能正文（跨频道信息泄漏）。
    """
    registry = ToolRegistry()

    _, r1 = await _run(
        registry, [], [_skill("a", content="AAA")], call=("load_skill", {"name": "a"})
    )
    # 第二个频道只启用了 b，问 a 应该拿不到
    _, r2 = await _run(
        registry, [], [_skill("b", content="BBB")], call=("load_skill", {"name": "a"})
    )

    assert "AAA" in str(
        [p for m in r1.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)][0].content
    )
    retries = [p for m in r2.all_messages() for p in m.parts if isinstance(p, RetryPromptPart)]
    assert len(retries) == 1, "B 频道竟然载入到了 A 频道的技能"


async def _dummy() -> str:
    """占位工具。"""
    return "ok"


# ---- 第 2 级：load_skill 给文件清单，不给内容 ----


async def test_load_skill带出文件清单() -> None:
    tool = build_skill_tool([_skill(files=[_file()])])

    out = await _fn(tool)("code-review")

    assert "checklist.md" in out
    assert "checklist.md 的用途" in out
    # 提到取回的办法，否则模型看到清单也不知道下一步调什么
    assert "read_skill_file" in out


async def test_load_skill不内联文件内容() -> None:
    """三级披露的核心断言。

    内容内联进第 2 级的话，一个带 3 个文档的 skill 每次载入都要付全部文档的
    代价，而典型任务只用得上其中一个 —— 第 1 级省下的又在第 2 级花掉了。
    """
    tool = build_skill_tool([_skill(files=[_file(content="这段正文不该出现在清单里")])])

    out = await _fn(tool)("code-review")

    assert "这段正文不该出现在清单里" not in out


async def test_清单带文件大小() -> None:
    """让模型在读之前对代价有数。"""
    tool = build_skill_tool([_skill(files=[_file(content="x" * 2048)])])

    out = await _fn(tool)("code-review")

    assert "2.0 KB" in out


async def test_无文件时不出现附带文件段落() -> None:
    tool = build_skill_tool([_skill()])

    out = await _fn(tool)("code-review")

    assert "附带文件" not in out
    assert "read_skill_file" not in out


# ---- 第 3 级：read_skill_file ----


async def test_读文件拿到内容() -> None:
    tool = build_skill_file_tool([_skill(files=[_file(content="完整的检查清单正文")])])

    out = await _fn(tool)("code-review", "checklist.md")

    assert "完整的检查清单正文" in out
    # 头部带技能名与路径：一次 run 读了多个文件后要能分清哪段是哪个
    assert "code-review/checklist.md" in out


async def test_读文件不做json包装() -> None:
    tool = build_skill_file_tool([_skill(files=[_file(content="第一行\n第二行")])])

    out = await _fn(tool)("code-review", "checklist.md")

    assert "第一行\n第二行" in out
    assert "\\n" not in out


async def test_路径不存在时回灌该技能的文件列表() -> None:
    tool = build_skill_file_tool([_skill(files=[_file("a.md"), _file("b.md")])])

    with pytest.raises(ModelRetry) as exc:
        await _fn(tool)("code-review", "c.md")

    msg = str(exc.value)
    assert "a.md" in msg and "b.md" in msg


async def test_技能名不存在时提示带文件的技能() -> None:
    """与「路径不对」分开给提示：合成一句「找不到」会让模型不知道改哪个参数。"""
    tool = build_skill_file_tool([_skill("code-review", files=[_file()])])

    with pytest.raises(ModelRetry) as exc:
        await _fn(tool)("weekly-report", "checklist.md")

    assert "code-review" in str(exc.value)


async def test_不能跨技能读文件() -> None:
    """两个技能各带一个同名文件时，必须按技能名限定作用域。

    只按路径索引会撞车，而撞车的表现是模型读到另一个技能的文件 —— 跨技能串味，
    且从输出上很难看出来。
    """
    tool = build_skill_file_tool(
        [
            _skill("a", files=[_file("reference.md", content="AAA", skill_id="skill_a")]),
            _skill("b", files=[_file("reference.md", content="BBB", skill_id="skill_b")]),
        ]
    )

    assert "AAA" in await _fn(tool)("a", "reference.md")
    assert "BBB" in await _fn(tool)("b", "reference.md")


# ---- 挂载条件 ----


async def test_有文件时才挂read_skill_file() -> None:
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill(files=[_file()])])

    assert _names(recorder) == {"load_skill", "read_skill_file"}


async def test_无文件时不挂read_skill_file() -> None:
    """一个永远返回「没有这个文件」的工具会占着模型的注意力，且诱导它猜路径。"""
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill()])

    assert _names(recorder) == {"load_skill"}


async def test_任一技能有文件即挂() -> None:
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill("a"), _skill("b", files=[_file()])])

    assert "read_skill_file" in _names(recorder)


async def test_read_skill_file的schema有两个参数() -> None:
    registry = ToolRegistry()

    recorder, _ = await _run(registry, [], [_skill(files=[_file()])])

    (tool,) = [t for t in recorder.tools if t.name == "read_skill_file"]
    assert set(tool.parameters_json_schema["properties"]) == {"skill", "path"}


async def test_模型端到端读到文件内容() -> None:
    """完整链路：工具挂上 → 模型调用 → 文件内容回到对话里。"""
    registry = ToolRegistry()

    _, result = await _run(
        registry,
        [],
        [_skill(files=[_file(content="端到端的文件正文")])],
        call=("read_skill_file", {"skill": "code-review", "path": "checklist.md"}),
    )

    returns = [
        p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert len(returns) == 1
    assert "端到端的文件正文" in str(returns[0].content)
