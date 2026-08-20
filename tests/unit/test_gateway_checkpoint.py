"""gateway 的检查点行为。

用 FunctionModel 充当模型，不碰网络。三处最容易静默退化，这里写死：

- **落库判据**：退化成「历史里存在任何 ToolReturnPart」会落下带悬空调用的
  检查点，而那种历史续跑时 SDK 直接抛 UserError
- **node.request 必须拼上**：只看 ctx.state.message_history 会一个检查点都不落
- **回调容错**：sink 抛异常不该毁掉一次正在成功的 run
"""

from __future__ import annotations

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teamai.domain.ports import TokenBudgetExceeded
from teamai.infrastructure.llm.gateway import (
    ModelConfig,
    ModelRequestNode,
    PydanticAIGateway,
    _dangling,
    _has_tool_return,
)

CALLS: list[str] = []


async def ping(n: int) -> str:
    """记账用工具。"""
    CALLS.append(f"ping({n})")
    return f"pong-{n}"


class Rounds:
    """跑满 n 轮工具调用后收尾。

    ⚠️ 行为必须从**传入的历史**推导，不能用自增计数器：续跑时 FunctionModel 是
    新实例，计数器归零就会从第一轮重放 —— 那测的是替身自己的 bug 而非 SDK 行为。
    真实模型也是看历史决定下一步的。（这个坑在设计探针阶段真的踩过。）
    """

    __name__ = "rounds"

    def __init__(self, n: int = 3) -> None:
        self.n = n

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = sum(1 for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        if done < self.n:
            return ModelResponse(parts=[ToolCallPart("ping", {"n": done + 1})])
        return ModelResponse(parts=[TextPart("done")])


class Sink:
    """收集检查点。"""

    def __init__(self, fail: bool = False) -> None:
        self.blobs: list[bytes] = []
        self.tokens: list[int] = []
        self._fail = fail

    async def __call__(self, messages: bytes, tokens_total: int) -> None:
        if self._fail:
            raise RuntimeError("模拟落库失败")
        self.blobs.append(messages)
        self.tokens.append(tokens_total)

    def parsed(self) -> list[list[ModelMessage]]:
        return [ModelMessagesTypeAdapter.validate_json(b) for b in self.blobs]


def _gateway(n: int = 3) -> tuple[PydanticAIGateway, FunctionModel]:
    """造一个 gateway，并把它的模型换成 FunctionModel。"""
    gw = PydanticAIGateway(ModelConfig())
    model = FunctionModel(Rounds(n))
    gw._model = lambda level: model  # type: ignore[assignment]  # noqa: SLF001
    return gw, model


def _toolset():
    from pydantic_ai.toolsets import FunctionToolset

    return FunctionToolset([Tool(ping)])


# ---- 落库判据 ----


async def test_每个工具轮落一个检查点() -> None:
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()

    r = await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    assert CALLS == ["ping(1)", "ping(2)", "ping(3)"]
    assert len(sink.blobs) == 3, "三轮工具应产出三个检查点"
    assert r.output == "done"


async def test_检查点绝无悬空调用() -> None:
    """带悬空调用的历史续跑时 SDK 直接抛 UserError，故这是硬前提。"""
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()

    await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    for i, msgs in enumerate(sink.parsed(), 1):
        assert _dangling(msgs) == 0, f"检查点{i} 带悬空工具调用"
        assert _has_tool_return(msgs), f"检查点{i} 没有工具结果，不该落库"


async def test_检查点递增且互不重复() -> None:
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()

    await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    lens = [len(m) for m in sink.parsed()]
    assert lens == sorted(lens) and len(set(lens)) == len(lens), f"检查点未递增: {lens}"
    assert len(set(sink.blobs)) == len(sink.blobs), "有重复检查点（去重失效）"


async def test_判据A会落下带悬空的检查点() -> None:
    """反例固化。

    判据 A =「历史里存在**任何** ToolReturnPart」。它从第一个工具结果之后就
    恒为真，其中包含「更新的一个 call 已发出、结果未回」的时刻。若有人把
    _has_tool_return 的判定挪到不检查悬空的位置，或去掉 _dangling 检查，
    就会落下这种检查点 —— 而它一旦用于续跑就是 UserError。

    这里直接验证：存在「A 为真但悬空非 0」的历史，故 A 不等价于正确判据。
    """
    dangling_hist: list[ModelMessage] = ModelMessagesTypeAdapter.validate_json(
        ModelMessagesTypeAdapter.dump_json(
            [
                ModelResponse(parts=[ToolCallPart("ping", {"n": 1}, tool_call_id="tc1")]),
            ]
        )
    )
    # 手工拼一段：第一轮结果已到，第二轮 call 已发但未回
    from pydantic_ai.messages import ModelRequest

    mixed = [
        *dangling_hist,
        ModelRequest(parts=[ToolReturnPart("ping", "pong-1", tool_call_id="tc1")]),
        ModelResponse(parts=[ToolCallPart("ping", {"n": 2}, tool_call_id="tc2")]),
    ]

    assert _has_tool_return(mixed) is True, "判据 A 在此状态为真"
    assert _dangling(mixed) == 1, "但它带悬空调用"
    # 结论：仅凭 A 落库是错的，必须同时要求 _dangling == 0


async def test_悬空按tool_call_id配对而非位置() -> None:
    """一轮里可以有多个并行工具调用，按位置数会误判。"""
    from pydantic_ai.messages import ModelRequest

    parallel = [
        ModelResponse(
            parts=[
                ToolCallPart("ping", {"n": 1}, tool_call_id="a"),
                ToolCallPart("ping", {"n": 2}, tool_call_id="b"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart("ping", "pong-1", tool_call_id="a"),
                ToolReturnPart("ping", "pong-2", tool_call_id="b"),
            ]
        ),
    ]
    assert _dangling(parallel) == 0

    half = parallel[:1] + [
        ModelRequest(parts=[ToolReturnPart("ping", "pong-1", tool_call_id="a")])
    ]
    assert _dangling(half) == 1


# ---- 续跑 ----


async def test_从中间检查点续跑只跑剩余轮次() -> None:
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()
    await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    for i, blob in enumerate(sink.blobs):
        done_before = i + 1
        CALLS.clear()
        gw2, _ = _gateway(3)
        r = await gw2.run("go", model_level="light", tools=_toolset(), history=blob)
        assert len(CALLS) == 3 - done_before, (
            f"从检查点{done_before} 续跑跑了 {CALLS}，期望 {3 - done_before} 次"
        )
        assert r.output == "done"


async def test_续跑不重复第一轮() -> None:
    """最关键的一条：续跑的意义就是不重跑已完成的工具。"""
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()
    await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    CALLS.clear()
    gw2, _ = _gateway(3)
    await gw2.run("go", model_level="light", tools=_toolset(), history=sink.blobs[-1])

    assert CALLS == [], "最后一个检查点之后已无工具可跑"


async def test_续跑时prompt被忽略() -> None:
    """原始提问已在历史里；再传一次 user prompt 会被 SDK 拒绝。

    gateway 内部处理了这件事（传 None），故调用方照常传 prompt 不该出错。
    """
    CALLS.clear()
    gw, _ = _gateway(2)
    sink = Sink()
    await gw.run("原始提问", model_level="light", tools=_toolset(), on_checkpoint=sink)

    gw2, _ = _gateway(2)
    # 传一个完全不同的 prompt，不该抛错，也不该影响结果
    r = await gw2.run("完全不同的话", model_level="light", tools=_toolset(), history=sink.blobs[0])
    assert r.output == "done"


async def test_原始提问保留在检查点里() -> None:
    """这是「载荷可纯从 DB 重建」的前提 —— 巡检重投时不需要 Redis 里那条消息。"""
    CALLS.clear()
    gw, _ = _gateway(2)
    sink = Sink()
    await gw.run("看下这个 PR", model_level="light", tools=_toolset(), on_checkpoint=sink)

    first = sink.parsed()[0]
    texts = [
        getattr(p, "content", "") for m in first for p in m.parts if type(p).__name__ == "UserPromptPart"
    ]
    assert any("看下这个 PR" in str(t) for t in texts), f"原始提问丢了: {first}"


# ---- 容错与兼容 ----


async def test_回调抛异常不影响run() -> None:
    """落不下检查点远好于让一次正在成功的 run 失败。"""
    CALLS.clear()
    gw, _ = _gateway(2)

    r = await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=Sink(fail=True))

    assert r.output == "done"
    assert CALLS == ["ping(1)", "ping(2)"]


async def test_不传回调时不落检查点也能跑() -> None:
    """向后兼容：现有调用点不改也要照常工作。"""
    CALLS.clear()
    gw, _ = _gateway(2)

    r = await gw.run("go", model_level="light", tools=_toolset())

    assert r.output == "done"
    assert r.tokens > 0


async def test_纯文本任务不落检查点() -> None:
    """判据要求有工具结果。纯对话重跑只花 token、无副作用，不值得付 DB 写入。"""
    gw, _ = _gateway(0)  # 0 轮 → 直接出文本
    sink = Sink()

    r = await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    assert r.output == "done"
    assert sink.blobs == []


async def test_无工具集时不落检查点() -> None:
    gw, _ = _gateway(0)
    sink = Sink()

    await gw.run("go", model_level="light", on_checkpoint=sink)

    assert sink.blobs == []


async def test_token上限仍转域异常() -> None:
    """改用 iter() 后 UsageLimitExceeded 的翻译不能丢。"""
    CALLS.clear()
    gw, _ = _gateway(3)

    with pytest.raises(TokenBudgetExceeded):
        await gw.run("go", model_level="light", tools=_toolset(), token_limit=1)


async def test_回调报的token是本段用量() -> None:
    """传空 RunUsage 让 run.usage 只统计本段 —— 调用方据此加基数。"""
    CALLS.clear()
    gw, _ = _gateway(3)
    sink = Sink()

    r = await gw.run("go", model_level="light", tools=_toolset(), on_checkpoint=sink)

    assert sink.tokens == sorted(sink.tokens), f"本段累计应单调不减: {sink.tokens}"
    assert sink.tokens[-1] <= r.tokens, "检查点报的量不该超过整段总量"


# ---- SDK 依赖守卫 ----


def test_ModelRequestNode可导入() -> None:
    """它来自 pydantic_ai._agent_graph —— **私有模块**。

    SDK 升级挪走它时要立刻红，而不是等到线上「再也不落检查点」这种无声失效：
    isinstance 对一个错误的类型永远返回 False，没有任何报错。
    """
    from pydantic_ai._agent_graph import ModelRequestNode as Imported

    assert ModelRequestNode is Imported
    assert hasattr(Imported, "__mro__")


def test_ModelRequestNode有request属性() -> None:
    """落库判据依赖 node.request 携带待发的 ToolReturnPart。

    这是整个方案的支点：ctx.state.message_history 滞后一轮，只看它一个检查点
    都落不下。若 SDK 改掉这个字段名，判据会静默失效。
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ModelRequestNode)}
    assert "request" in fields, f"ModelRequestNode 的字段变了: {fields}"
