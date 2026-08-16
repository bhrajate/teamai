"""记忆蒸馏测试。

三条契约在这里锁住：
1. 普通消息只入窗，不逐条落库（否则 memory_entries 退化成聊天日志）；
2. 模型判「无可记内容」时一条都不写 —— 不给它空结果的出口，它会把寒暄编成事实；
3. 蒸馏烧的 token 计入该频道配额，后台任务不能绕过预算硬上限。
"""

from __future__ import annotations

import pytest

from teamai.application.agent.prompts import DISTILL_NONE
from teamai.application.budget import BudgetController
from teamai.application.distiller import DistillAction, MemoryDistiller, _parse_entries
from teamai.application.memory import MemoryService
from teamai.domain.models import (
    AuditAction,
    BudgetQuota,
    BudgetScope,
    MemoryType,
)
from teamai.domain.ports import LLMGateway, LLMResult, ToolBundle
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.window import InMemoryMessageWindow
from tests.fakes import (
    FakeAuditRepository,
    FakeBudgetRepository,
    FakeChannelRepository,
    FakeMemoryRepository,
)


class StubGateway(LLMGateway):
    """返回预设的蒸馏结果，并记下每次调用。

    `outputs` 给多轮蒸馏用：按调用次序逐个返回，用尽后重复最后一个。去重相关的
    测试要跑多轮（同一事实在多个窗口被提到），每轮的模型输出不同。
    """

    def __init__(
        self, output: str = "", tokens: int = 50, outputs: list[str] | None = None
    ) -> None:
        self.output = output
        self.calls: list[str] = []
        self._tokens = tokens
        self._outputs = list(outputs) if outputs else None

    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
    ) -> LLMResult:
        self.calls.append(prompt)
        if self._outputs:
            idx = min(len(self.calls) - 1, len(self._outputs) - 1)
            output = self._outputs[idx]
        else:
            output = self.output
        return LLMResult(
            output=output,
            tokens=self._tokens,
            tokens_in=self._tokens,
            model_id="anthropic:claude-3-5-haiku",
        )


def _build(
    output: str = "",
    *,
    quota: BudgetQuota | None = None,
    tokens: int = 50,
    outputs: list[str] | None = None,
) -> tuple[MemoryDistiller, StubGateway, FakeMemoryRepository, FakeBudgetRepository]:
    audit = AuditLogWriter(FakeAuditRepository())
    memory_repo = FakeMemoryRepository()
    budget_repo = FakeBudgetRepository(quota)
    memory = MemoryService(memory_repo, FakeChannelRepository(), audit)
    gateway = StubGateway(output, tokens, outputs=outputs)
    distiller = MemoryDistiller(
        InMemoryMessageWindow(),
        memory,
        gateway,
        BudgetController(budget_repo, audit),
        window_size=3,
        max_idle_seconds=600,
    )
    return distiller, gateway, memory_repo, budget_repo


# ===== 解析 =====


def test_解析动作类型编号与内容() -> None:
    items = _parse_entries("ADD|DECISION||决定用 Postgres\nUPDATE|FACT|3|超时改成 5 秒")

    assert [(i.action, i.type, i.ref, i.content) for i in items] == [
        (DistillAction.ADD, MemoryType.DECISION, None, "决定用 Postgres"),
        (DistillAction.UPDATE, MemoryType.FACT, 3, "超时改成 5 秒"),
    ]


def test_NOOP_只需编号不需内容() -> None:
    """NOOP 表示「库里已经有了」，没有新内容要写。"""
    (item,) = _parse_entries("NOOP||4|")

    assert item.action is DistillAction.NOOP
    assert item.ref == 4


def test_NOOP_缺编号则整行丢弃() -> None:
    """没有编号的 NOOP 什么都指不到，是一行噪声。"""
    assert _parse_entries("NOOP|||") == []


def test_未知动作按ADD处理() -> None:
    """漏写动作最可能是想新增。按 ADD 最多多一条重复，判成 UPDATE 会误伤
    一条正确记忆。"""
    (item,) = _parse_entries("WAT|FACT||这条仍然有用")

    assert item.action is DistillAction.ADD
    assert item.content == "这条仍然有用"


def test_UPDATE缺编号降级为ADD() -> None:
    """没有编号就无从取代。"""
    (item,) = _parse_entries("UPDATE|FACT||超时是 5 秒")

    assert item.action is DistillAction.ADD
    assert item.ref is None


def test_未知类型归入背景知识而非丢弃() -> None:
    """分类错了还能用，内容丢了就没了。"""
    (item,) = _parse_entries("ADD|WHATEVER||这条仍然有用")

    assert item.type is MemoryType.BACKGROUND_KNOWLEDGE
    assert item.content == "这条仍然有用"


def test_兼容旧的两段格式() -> None:
    """加动作维度之前的输出形状，模型偶尔会退回去。缺动作即视为 ADD。"""
    (item,) = _parse_entries("FACT|超时是 30 秒")

    assert item.action is DistillAction.ADD
    assert item.type is MemoryType.FACT
    assert item.content == "超时是 30 秒"


def test_兼容旧的三段格式() -> None:
    """`动作|类型|内容`（漏了编号位）也要收下。"""
    (item,) = _parse_entries("ADD|FACT|端口是 8000")

    assert item.action is DistillAction.ADD
    assert item.type is MemoryType.FACT
    assert item.content == "端口是 8000"


def test_没有分隔符时整行当内容() -> None:
    (item,) = _parse_entries("模型忘了带任何前缀")

    assert item.action is DistillAction.ADD
    assert item.type is MemoryType.BACKGROUND_KNOWLEDGE
    assert item.content == "模型忘了带任何前缀"


def test_NONE_与空行不产出条目() -> None:
    assert _parse_entries(f"{DISTILL_NONE}") == []
    assert _parse_entries("") == []
    assert _parse_entries("\n\n  \n") == []


def test_列表符号被剥掉() -> None:
    """模型常自作主张加 markdown 列表符号。"""
    (item,) = _parse_entries("- ADD|FACT||端口是 8000")

    assert item.type is MemoryType.FACT
    assert item.content == "端口是 8000"


def test_超长内容被截断而非丢弃() -> None:
    (item,) = _parse_entries("ADD|FACT||" + "长" * 900)

    assert len(item.content) == 500


def test_内容里的竖线不被截掉() -> None:
    """内容本身可能含 `|`（贴表格、贴命令）。按前三段切，其余全归内容。"""
    (item,) = _parse_entries("ADD|FACT||命令是 `a | b | c`")

    assert item.content == "命令是 `a | b | c`"


# ===== 窗口与触发 =====


async def test_未满且未静置时不蒸馏() -> None:
    distiller, gateway, memory_repo, _ = _build("FACT|不该被写入")

    await distiller.observe("ch_1", "U1", "第一句")
    report = await distiller.sweep()

    assert gateway.calls == [], "窗口没满就不该调模型"
    assert report.considered == 0
    assert memory_repo.stored == []


async def test_窗口满后蒸馏并写入记忆() -> None:
    distiller, gateway, memory_repo, _ = _build("DECISION|决定下周发版")

    for i in range(3):  # window_size=3
        await distiller.observe("ch_1", "U1", f"第 {i} 句")
    report = await distiller.sweep()

    assert len(gateway.calls) == 1
    assert "第 0 句" in gateway.calls[0], "整窗对话应一并送去蒸馏"
    assert report.distilled == {"ch_1": 1}
    assert [e.content for e in memory_repo.stored] == ["决定下周发版"]
    assert memory_repo.stored[0].type is MemoryType.DECISION


async def test_蒸馏走_MEMORY_DISTILL_审计动作() -> None:
    """人工写入与系统蒸馏要能区分，否则排查「记忆库里怎么会有这条」无从下手。"""
    audit_repo = FakeAuditRepository()
    audit = AuditLogWriter(audit_repo)
    memory = MemoryService(FakeMemoryRepository(), FakeChannelRepository(), audit)
    distiller = MemoryDistiller(
        InMemoryMessageWindow(),
        memory,
        StubGateway("FACT|端口 8000"),
        BudgetController(FakeBudgetRepository(), audit),
        window_size=1,
    )

    await distiller.observe("ch_1", "U1", "端口是 8000")
    await distiller.sweep()

    actions = [log.action for log in audit_repo.logs]
    assert AuditAction.MEMORY_DISTILL in actions
    assert AuditAction.MEMORY_STORE not in actions


async def test_模型判无可记内容时一条不写() -> None:
    distiller, gateway, memory_repo, _ = _build(DISTILL_NONE)

    for i in range(3):
        await distiller.observe("ch_1", "U1", f"哈哈 {i}")
    report = await distiller.sweep()

    assert len(gateway.calls) == 1, "仍然调了模型"
    assert memory_repo.stored == []
    assert report.empty == ["ch_1"]
    assert report.distilled == {}


# ===== 去重与取代 =====


async def _run_window(distiller: MemoryDistiller, channel: str, texts: list[str]) -> None:
    """喂满一个窗口并蒸馏一轮。"""
    for t in texts:
        await distiller.observe(channel, "U1", t)
    await distiller.sweep()


async def test_同一事实在多个窗口被提到只存一条() -> None:
    """去重的核心用例。

    改造前蒸馏只会追加：同一件事在三个窗口里被提到就存三条，`top_k=5` 的名额
    被同一个事实占掉多个，检索质量随使用时长单调下降。

    现在第二、三轮的候选里能看到已存的那条，模型判 NOOP。
    """
    distiller, gateway, memory_repo, _ = _build(
        outputs=[
            "ADD|FACT||支付模块依赖 redis 缓存",
            "NOOP||1|",
            "NOOP||1|",
        ]
    )

    for round_no in range(3):
        await _run_window(distiller, "ch_1", [f"第 {round_no} 轮第 {i} 句" for i in range(3)])

    assert len(gateway.calls) == 3, "三轮都该调模型"
    current = [e for e in memory_repo.stored if e.is_current]
    assert [e.content for e in current] == ["支付模块依赖 redis 缓存"]
    # 后两轮判 NOOP，没有产出新知识 —— 不该被算作有产出
    assert len(memory_repo.stored) == 1


async def test_候选记忆被送进提示词供比对() -> None:
    """没有候选，模型无从判断 UPDATE / NOOP，去重必然失效。"""
    distiller, gateway, _, _ = _build(
        outputs=["ADD|FACT||超时设为 3 秒", "NOOP||1|"]
    )

    await _run_window(distiller, "ch_1", ["a", "b", "c"])
    await _run_window(distiller, "ch_1", ["d", "e", "f"])

    assert "该频道目前没有已存记忆" in gateway.calls[0], "首轮没有候选，要明确告知"
    assert "超时设为 3 秒" in gateway.calls[1], "第二轮要把已存记忆作为候选送进去"
    assert "1. 超时设为 3 秒" in gateway.calls[1], "候选要带编号供引用"


async def test_事实变化时旧条目被取代而非并列() -> None:
    """矛盾共存是改造前最严重的缺陷：团队三月定「超时 3 秒」、六月改成 5 秒，
    库里两条并列，检索按相似度取 top_k 时两条几乎一样，模型看到互相矛盾的
    上下文且没有任何信号判断哪条是现行的。"""
    distiller, gateway, memory_repo, _ = _build(
        outputs=[
            "ADD|FACT||接口超时设为 3 秒",
            "UPDATE|FACT|1|接口超时设为 5 秒",
        ]
    )

    await _run_window(distiller, "ch_1", ["超时定 3 秒吧", "好", "行"])
    await _run_window(distiller, "ch_1", ["超时改成 5 秒", "同意", "改了"])

    current = [e for e in memory_repo.stored if e.is_current]
    assert [e.content for e in current] == ["接口超时设为 5 秒"], "只有新值是现行事实"

    superseded = [e for e in memory_repo.stored if not e.is_current]
    assert [e.content for e in superseded] == ["接口超时设为 3 秒"]
    assert superseded[0].superseded_at is not None
    # 取代指针指向新条目，能回答「被什么取代了」
    assert superseded[0].superseded_by == current[0].id


async def test_被取代的记忆不再进入检索() -> None:
    """superseded_by 的意义就在这里 —— 留在库里可查，但不喂给模型。"""
    distiller, _, memory_repo, _ = _build(
        outputs=[
            "ADD|FACT||超时设为 3 秒",
            "UPDATE|FACT|1|超时设为 5 秒",
        ]
    )
    await _run_window(distiller, "ch_1", ["a", "b", "c"])
    await _run_window(distiller, "ch_1", ["d", "e", "f"])

    listed = await memory_repo.list_by_channel("ch_1")
    assert [e.content for e in listed] == ["超时设为 5 秒"]

    # 显式要历史时两条都在
    all_rows = await memory_repo.list_by_channel("ch_1", current_only=False)
    assert len(all_rows) == 2


async def test_引用不存在的编号降级为新增而非丢弃() -> None:
    """内容本身可能有效，丢掉等于让这条知识彻底进不来。"""
    distiller, _, memory_repo, _ = _build(outputs=["UPDATE|FACT|7|某个新事实"])

    await _run_window(distiller, "ch_1", ["a", "b", "c"])

    assert [e.content for e in memory_repo.stored] == ["某个新事实"]
    assert memory_repo.stored[0].is_current


async def test_同一编号被取代两次时第二次降级为新增() -> None:
    """否则会形成 A→B→C 的链，中间那条 B 从未进入过检索。"""
    distiller, _, memory_repo, _ = _build(
        outputs=[
            "ADD|FACT||原始事实",
            "UPDATE|FACT|1|第一次修订\nUPDATE|FACT|1|第二次修订",
        ]
    )
    await _run_window(distiller, "ch_1", ["a", "b", "c"])
    await _run_window(distiller, "ch_1", ["d", "e", "f"])

    current = sorted(e.content for e in memory_repo.stored if e.is_current)
    assert current == ["第一次修订", "第二次修订"], "第二条作为新增存在，不形成取代链"
    superseded = [e for e in memory_repo.stored if not e.is_current]
    assert [e.content for e in superseded] == ["原始事实"]


async def test_NOOP不计入产出条数() -> None:
    """NOOP 表示「库里已经有了」，本轮没有新知识。若算进去，报告会把「什么都
    没变」报成有产出。"""
    distiller, _, memory_repo, _ = _build(
        outputs=["ADD|FACT||某事实", "NOOP||1|"]
    )
    await _run_window(distiller, "ch_1", ["a", "b", "c"])

    for i in range(3):
        await distiller.observe("ch_1", "U1", f"第二轮 {i}")
    report = await distiller.sweep()

    assert report.distilled == {}, "全是 NOOP 的一轮不该记作有产出"
    assert report.empty == ["ch_1"]
    assert len(memory_repo.stored) == 1


async def test_蒸馏后窗口被清空() -> None:
    """否则下一轮会重复蒸馏同一批对话，产出重复记忆。"""
    distiller, gateway, _, _ = _build("FACT|某事实")

    for i in range(3):
        await distiller.observe("ch_1", "U1", f"第 {i} 句")
    await distiller.sweep()
    await distiller.sweep()

    assert len(gateway.calls) == 1


# ===== 预算 =====


async def test_蒸馏的token计入频道配额() -> None:
    quota = BudgetQuota(
        id="b1", scope=BudgetScope.CHANNEL, channel_instance_id="ch_1", token_limit=10_000
    )
    distiller, _, _, budget_repo = _build("FACT|某事实", quota=quota, tokens=137)

    for i in range(3):
        await distiller.observe("ch_1", "U1", f"第 {i} 句")
    await distiller.sweep()

    assert budget_repo.quota is not None
    assert budget_repo.quota.used_tokens == 137


async def test_配额耗尽时跳过且保留窗口() -> None:
    """预算暂停的预期是「任务先不跑」，不该顺带丢掉记忆素材。"""
    quota = BudgetQuota(
        id="b1",
        scope=BudgetScope.CHANNEL,
        channel_instance_id="ch_1",
        token_limit=100,
        used_tokens=100,
    )
    distiller, gateway, memory_repo, _ = _build("FACT|某事实", quota=quota)

    for i in range(3):
        await distiller.observe("ch_1", "U1", f"第 {i} 句")
    report = await distiller.sweep()

    assert gateway.calls == []
    assert memory_repo.stored == []
    assert report.skipped_budget == ["ch_1"]

    # 配额恢复后同一批对话仍能被蒸馏 —— 证明窗口没被 drain
    quota.used_tokens = 0
    report2 = await distiller.sweep()
    assert report2.distilled == {"ch_1": 1}


# ===== 故障隔离 =====


async def test_单个频道失败不打断整轮() -> None:
    class Flaky(StubGateway):
        async def run(self, prompt: str, **kwargs) -> LLMResult:
            if "坏频道" in prompt:
                raise RuntimeError("模型炸了")
            return await super().run(prompt, **kwargs)

    audit = AuditLogWriter(FakeAuditRepository())
    memory_repo = FakeMemoryRepository()
    memory = MemoryService(memory_repo, FakeChannelRepository(), audit)
    distiller = MemoryDistiller(
        InMemoryMessageWindow(),
        memory,
        Flaky("FACT|好频道的事实"),
        BudgetController(FakeBudgetRepository(), audit),
        window_size=1,
    )

    await distiller.observe("ch_bad", "U1", "坏频道的话")
    await distiller.observe("ch_good", "U2", "好频道的话")
    report = await distiller.sweep()

    assert [c for c, _ in report.failed] == ["ch_bad"]
    assert report.distilled == {"ch_good": 1}
    assert [e.content for e in memory_repo.stored] == ["好频道的事实"]


async def test_入窗失败不外抛() -> None:
    """缓冲写失败只是「这条对话没进记忆素材」，不该影响消息处理主流程。"""

    class BrokenWindow(InMemoryMessageWindow):
        async def append(self, channel_instance_id: str, line: str) -> int:
            raise ConnectionError("Redis 挂了")

    audit = AuditLogWriter(FakeAuditRepository())
    distiller = MemoryDistiller(
        BrokenWindow(),
        MemoryService(FakeMemoryRepository(), FakeChannelRepository(), audit),
        StubGateway(),
        BudgetController(FakeBudgetRepository(), audit),
    )

    await distiller.observe("ch_1", "U1", "某句话")  # 不该抛


@pytest.mark.parametrize("idle,expect_call", [(0, 1), (600, 0)])
async def test_静置超时也触发蒸馏(idle: int, expect_call: int) -> None:
    """冷清频道的对话不该一直攒着不落地。"""
    audit = AuditLogWriter(FakeAuditRepository())
    distiller = MemoryDistiller(
        InMemoryMessageWindow(),
        MemoryService(FakeMemoryRepository(), FakeChannelRepository(), audit),
        gateway := StubGateway("FACT|某事实"),
        BudgetController(FakeBudgetRepository(), audit),
        window_size=99,  # 永远攒不满，只能靠静置触发
        max_idle_seconds=idle,
    )

    await distiller.observe("ch_1", "U1", "唯一一句")
    await distiller.sweep()

    assert len(gateway.calls) == expect_call
