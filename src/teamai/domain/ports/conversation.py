"""入向会话读取端口：拉线程历史、缓冲待蒸馏的消息。

与 `MessagePublisher`（出向发送）刻意分成两个端口而不是给它加方法：worker 只
需要发送能力，把读取塞进同一个抽象会逼它实现用不到的方法；且两者的失败语义
不同 —— 发不出去要让调用方知道（抛 ConnectionError），拉不到历史则应静默降级
成「没有历史」，任务照跑。

只依赖标准库，满足 test_domain_不导入三方库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ThreadLocator:
    """定位一个线程。三个字段与 `ReplyTarget` 相同，但刻意不复用它。

    两个理由：一是方向相反，让读取端口收一个名为 "Reply" 的类型会让契约读起来
    是反的；二是两者会分化 —— 读取日后可能要加游标/起始时间（分页拉更早的
    历史），而发送永远不需要，那时给 ReplyTarget 加字段会污染出向路径。
    """

    platform: str
    channel_id: str
    thread_ref: str


@dataclass(frozen=True)
class ThreadMessage:
    """线程里的一条消息。平台无关，由各平台实现归一。

    `author_id` 为空表示取不到发送者（如平台的系统消息）。

    `is_self` 严格表示「这条是本 bot 自己发的」，不是「某个机器人发的」。这个
    区分是必需的，因为 `render()` 把它渲染成 `AI:` —— 而团队频道里往往还有 CI
    通知、告警机器人。把它们的消息标成 `AI:`，模型会以为那些话是自己上一轮说的，
    于是可能「承认」一个自己没做过的判断、或围绕别的机器人的输出继续往下答。

    字段原名 `is_bot`，语义是「某个机器人」—— 名字本身诚实，但它被当成「我」来
    渲染，两个平台的实现也就都按字面写成了「有 bot_id / sender_type 是 app」。
    改名是修复的一部分：留着旧名，下一个人还会照字面再写错一遍。

    别的机器人不单独标记，按普通参与者渲染成 `<bot_id>: ...`。模型本来也不知道
    任何一个 ID 背后是人还是机器，多一档只在「想让模型知道这是机器输出」时才有
    价值，那是另一件事。
    """

    author_id: str
    text: str
    ts: datetime | None = None
    is_self: bool = False

    def render(self) -> str:
        """渲染成提示词里的一行。"""
        who = "AI" if self.is_self else (self.author_id or "unknown")
        return f"{who}: {self.text}"


class ThreadReader(ABC):
    """按平台拉取某个线程的最近消息。"""

    @abstractmethod
    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        """返回按时间正序排列的最近 limit 条消息。

        拉不到（线程不存在、权限不足、平台不可用）返回空列表而不是抛异常：
        调用方是 Agent 上下文组装，没有历史仍可作答，为此让任务失败不划算。
        实现方须自行兜住平台异常并记日志。
        """
        ...


class ThreadHistorySink(ABC):
    """把已知的新消息补进线程历史缓存。

    存在的理由：缓存若只能靠 TTL 过期整体重建，则 TTL 窗口内的第二次读取拿到的
    是一张过期快照 —— 机器人看不见自己上一轮的回复，也看不见其间别人插的话。而
    多轮对话恰恰是唯一会连续读同一线程的场景，缓存想省的调用与最需要新鲜数据的
    时刻完全重合。故让每条经手的消息（入向的用户消息、出向的机器人回复）顺手
    append 进去，缓存自己保持新鲜，不必靠缩短 TTL 来换正确性。

    实现须遵守两条语义，否则会引入比「历史陈旧」更糟的问题：

    1. **只在已有缓存时追加，不得凭 append 建立缓存。** 无缓存时追加会得到一段
       只含一条消息的「历史」，下次读取会把它当成完整的线程历史返回，真实历史
       反而被挡住。此时正确的行为是什么都不做，让下次读取穿透到平台拿全量。
    2. **不得延长缓存的过期时间。** TTL 到点整体重建是这套机制的纠错手段 ——
       追加过程中丢的、重的、乱序的，都在下一个窗口被平台的权威数据抹平。
       每次追加都续期会让一次错误追加永久驻留。

    追加失败一律静默：与 ThreadReader 同理，历史是增益。
    """

    @abstractmethod
    async def note(self, locator: ThreadLocator, message: ThreadMessage) -> None:
        """把一条已知消息追加进该线程的缓存（若缓存存在）。"""
        ...


class MessageWindow(ABC):
    """待蒸馏消息的滚动缓冲。

    非 @ 消息不再逐条落库（那会把聊天碎片混进 memory_entries），先攒进本缓冲，
    由 worker 的定时任务成窗取出、蒸馏成记忆后丢弃原文。

    实现须按 channel_instance_id 分键，并额外记录各频道窗口的首次写入时间，
    否则 `due_channels` 只能遍历全部键才能找出到期的那几个。
    """

    @abstractmethod
    async def append(self, channel_instance_id: str, line: str) -> int:
        """追加一条，返回追加后的窗口长度。"""
        ...

    @abstractmethod
    async def due_channels(self, max_size: int, max_idle_seconds: int) -> list[str]:
        """返回该蒸馏的频道 id：窗口已满 max_size，或首次写入距今超过 max_idle_seconds。"""
        ...

    @abstractmethod
    async def drain(self, channel_instance_id: str) -> list[str]:
        """取出并清空某频道的整个窗口。"""
        ...
