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
from teamai.application.distiller import MemoryDistiller, _parse_entries
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
    """返回预设的蒸馏结果，并记下每次调用。"""

    def __init__(self, output: str = "", tokens: int = 50) -> None:
        self.output = output
        self.calls: list[str] = []
        self._tokens = tokens

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
        return LLMResult(
            output=self.output,
            tokens=self._tokens,
            tokens_in=self._tokens,
            model_id="anthropic:claude-3-5-haiku",
        )


def _build(
    output: str = "",
    *,
    quota: BudgetQuota | None = None,
    tokens: int = 50,
) -> tuple[MemoryDistiller, StubGateway, FakeMemoryRepository, FakeBudgetRepository]:
    audit = AuditLogWriter(FakeAuditRepository())
    memory_repo = FakeMemoryRepository()
    budget_repo = FakeBudgetRepository(quota)
    memory = MemoryService(memory_repo, FakeChannelRepository(), audit)
    gateway = StubGateway(output, tokens)
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


def test_解析类型与内容() -> None:
    entries = _parse_entries("DECISION|决定用 Postgres\nFACT|超时是 30 秒")

    assert entries == [
        (MemoryType.DECISION, "决定用 Postgres"),
        (MemoryType.FACT, "超时是 30 秒"),
    ]


def test_未知类型归入背景知识而非丢弃() -> None:
    """分类错了还能用，内容丢了就没了。"""
    (entry,) = _parse_entries("WHATEVER|这条仍然有用")

    assert entry == (MemoryType.BACKGROUND_KNOWLEDGE, "这条仍然有用")


def test_没有分隔符时整行当内容() -> None:
    (entry,) = _parse_entries("模型忘了带类型前缀")

    assert entry[0] is MemoryType.BACKGROUND_KNOWLEDGE
    assert entry[1] == "模型忘了带类型前缀"


def test_NONE_与空行不产出条目() -> None:
    assert _parse_entries(f"{DISTILL_NONE}") == []
    assert _parse_entries("") == []
    assert _parse_entries("\n\n  \n") == []


def test_列表符号被剥掉() -> None:
    """模型常自作主张加 markdown 列表符号。"""
    (entry,) = _parse_entries("- FACT|端口是 8000")

    assert entry == (MemoryType.FACT, "端口是 8000")


def test_超长内容被截断而非丢弃() -> None:
    (entry,) = _parse_entries("FACT|" + "长" * 900)

    assert len(entry[1]) == 500


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
