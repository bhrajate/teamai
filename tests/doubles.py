"""application 层的测试替身。

与 tests/fakes.py 的分工：fakes.py 只实现 domain 声明的端口与仓储（故它能
声称「不 import infrastructure」）；本文件的替身要用到 application 层的类型
（Intent / StageResult 等），单独放一处，免得 fakes.py 的层级定位被稀释。

被 test_router.py 与 integration/test_long_task_flow.py 共用 —— 同一套替身
语义，两处断言才可比。
"""

from __future__ import annotations

from teamai.application.agent.runtime import StageResult, StageStatus
from teamai.application.events import IncomingMessage
from teamai.application.intent import Intent
from teamai.domain.models import ChannelInstance, Task
from teamai.domain.ports import ThreadMessage

INSTANCE = ChannelInstance(
    id="ch_1", platform="slack", channel_id="C1", workspace_id="T1", agent_identity="ai_1"
)


class FakeIntentClassifier:
    """固定返回指定 kind，绕开关键词规则以便精确控制同步/异步分叉。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def classify(self, text: str) -> Intent:
        return Intent(kind=self.kind)


class FakeChannels:
    def __init__(self, instance: ChannelInstance | None = None) -> None:
        self._instance = instance if instance is not None else INSTANCE

    async def get_or_create(self, platform: str, channel_id: str, workspace_id: str) -> ChannelInstance:
        return self._instance

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return self._instance


class FakeTags:
    def __init__(self, tag: object | None = None) -> None:
        self.tag = tag

    async def resolve(self, channel_instance_id: str, name: str | None) -> object | None:
        return self.tag


class FakeMemory:
    def __init__(self) -> None:
        self.stored: list[str] = []

    async def store(self, channel_instance_id: str, text: str, source_user_id: str = "") -> None:
        self.stored.append(text)

    async def query_for_context(self, channel_instance_id: str, prompt: str) -> list[str]:
        return []


class FakeDistiller:
    """记录哪些消息进了待蒸馏窗口。

    非 @ 消息现在不再逐条写记忆库，而是先入窗、由 worker 定时蒸馏。
    router 的断言因此从「记忆里存了什么」变成「窗口收到了什么」。
    """

    def __init__(self) -> None:
        self.observed: list[tuple[str, str, str]] = []  # (channel_id, author, text)

    async def observe(self, channel_instance_id: str, author_id: str, text: str) -> None:
        self.observed.append((channel_instance_id, author_id, text))


class FakeConversation:
    """按需返回线程历史，并记下每次拉取的目标。

    默认返回空列表 —— 那是平台拉取失败时的降级形态，也是「没配 reader」时的
    常态，让默认路径与生产的兜底行为一致。
    """

    def __init__(self, history: list[ThreadMessage] | None = None) -> None:
        self._history = history or []
        self.calls: list[tuple[str, str]] = []  # (channel_id, thread_ref)

    async def thread_history(
        self, instance: ChannelInstance, thread_ref: str, limit: int | None = None
    ) -> list[ThreadMessage]:
        self.calls.append((instance.channel_id, thread_ref))
        return list(self._history)


class FakeRuntime:
    """记录 Agent 实际被跑了几次，以及每次拿到的 prompt。

    长任务链路的关键断言就靠它：web 进程阶段 runs 必须是 0，worker 消费后
    才变成 1 —— 否则「拆进程」等于没拆。
    """

    def __init__(self, status: StageStatus = StageStatus.DONE, output: str = "执行完毕") -> None:
        self.runs = 0
        self.prompts: list[str] = []
        # 留整个 bundle 而非只留 prompt：线程历史与记忆命中是 router 组装进
        # bundle 的，断言它们接上了必须看得到对象本身。
        self.bundles: list[object] = []
        self._status = status
        self._output = output

    async def run(self, task: Task, bundle: object) -> StageResult:
        self.runs += 1
        self.bundles.append(bundle)
        self.prompts.append(getattr(bundle, "user_prompt", ""))
        if self._status is StageStatus.DONE:
            return StageResult(status=self._status, output=self._output)
        return StageResult(status=self._status, error=self._output)


class FakePolicyRepo:
    async def get_for_channel(self, channel_instance_id: str) -> None:
        return None


def mention(
    text: str = "帮我审查这段代码",
    *,
    is_mention: bool = True,
    channel_type: str = "channel",
) -> IncomingMessage:
    """构造入向消息。

    `channel_type` 可覆盖：私聊（slack `im`/`mpim`、feishu `p2p`）的内容不该
    进入频道记忆（PRD §4.2），router 靠这个字段判定。
    """
    return IncomingMessage(
        platform="slack",
        event_id="slack:Ev1",
        workspace_id="T1",
        channel_id="C1",
        channel_type=channel_type,
        user_id="U1",
        text=text,
        message_id="1700000000.1",
        thread_ref="1700000000.1",
        is_mention=is_mention,
    )
