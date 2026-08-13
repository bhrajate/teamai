"""两个平台 ThreadReader 的归一化，重点是 `is_self` 的判定。

这两个读取器此前完全没有测试，于是一个错标缺陷活了下来：判定写成「有 bot_id」
（Slack）和「sender_type 是 app」（飞书），那是**某个**机器人而非**本**机器人。
团队频道里常有 CI 通知、告警机器人，它们的消息被渲染成 `AI:`，模型会以为那些话
是自己上一轮说的 —— 于是可能围绕别的机器人的输出往下答，或「承认」一个自己没做过
的判断。

`is_self` 必须严格：身份未知时一律为假。宁可把自己的回复降级成普通参与者，
也不能把别人的话认领成自己的。
"""

from __future__ import annotations

from teamai.domain.ports import ThreadLocator
from teamai.infrastructure.messaging.feishu_reader import FeishuThreadReader
from teamai.infrastructure.messaging.slack_reader import SlackThreadReader

LOCATOR = ThreadLocator(platform="slack", channel_id="C1", thread_ref="1700000000.1")
OUR_BOT_ID = "B_self"
OUR_USER_ID = "U_self"

# ===== Slack =====


class FakeSlackClient:
    """只实现被用到的两个方法，并记下各自被调了几次。

    `auth_test` 的调用次数是被测契约之一：它该每进程一次，不是每次拉线程一次。
    """

    def __init__(
        self,
        messages: list[dict] | None = None,
        *,
        identity: dict | None = None,
        auth_boom: bool = False,
        replies_boom: bool = False,
    ) -> None:
        self._messages = messages or []
        self._identity = identity if identity is not None else {
            "bot_id": OUR_BOT_ID,
            "user_id": OUR_USER_ID,
        }
        self._auth_boom = auth_boom
        self._replies_boom = replies_boom
        self.auth_calls = 0
        self.replies_calls = 0

    async def auth_test(self):
        self.auth_calls += 1
        if self._auth_boom:
            raise ConnectionError("missing_scope")
        return self._identity

    async def conversations_replies(self, channel: str, ts: str, limit: int):
        self.replies_calls += 1
        if self._replies_boom:
            raise ConnectionError("ratelimited")
        return {"messages": list(self._messages)}


def _slack(messages: list[dict], **kwargs) -> tuple[SlackThreadReader, FakeSlackClient]:
    client = FakeSlackClient(messages, **kwargs)
    return SlackThreadReader(client), client  # type: ignore[arg-type]


async def test_slack_自己的回复按bot_id标为self() -> None:
    reader, _ = _slack([{"text": "我建议看网关日志", "bot_id": OUR_BOT_ID, "ts": "1.0"}])

    messages = await reader.fetch_thread(LOCATOR, 10)

    assert [m.is_self for m in messages] == [True]
    assert messages[0].render() == "AI: 我建议看网关日志"


async def test_slack_自己的回复按user_id标为self() -> None:
    """经 chat.postMessage 发出的消息两个字段都可能有，只比 bot_id 会漏。"""
    reader, _ = _slack([{"text": "我说的", "user": OUR_USER_ID, "ts": "1.0"}])

    assert [m.is_self for m in await reader.fetch_thread(LOCATOR, 10)] == [True]


async def test_slack_别的机器人不算self() -> None:
    """本次修复的核心。原判定是 `bool(m.get("bot_id"))`，CI 通知会命中，
    于是它的消息渲染成 `AI:`，模型以为是自己说的。"""
    reader, _ = _slack(
        [
            {"text": "构建 #412 失败", "bot_id": "B_ci", "subtype": "bot_message", "ts": "1.0"},
            {"text": "磁盘使用率 91%", "bot_id": "B_alert", "ts": "2.0"},
        ]
    )

    messages = await reader.fetch_thread(LOCATOR, 10)

    assert [m.is_self for m in messages] == [False, False]
    assert messages[0].render() == "B_ci: 构建 #412 失败", "该按普通参与者渲染"


async def test_slack_人说的不算self() -> None:
    reader, _ = _slack([{"text": "帮我看下", "user": "U_human", "ts": "1.0"}])

    messages = await reader.fetch_thread(LOCATOR, 10)

    assert [m.is_self for m in messages] == [False]
    assert messages[0].render() == "U_human: 帮我看下"


async def test_slack_身份拉取失败时不认领任何消息() -> None:
    """降级方向必须是「少标」而不是「错标」：自己的回复退化成普通参与者，
    但绝不能把别人的话标成自己的。"""
    reader, client = _slack(
        [
            {"text": "我说的", "bot_id": OUR_BOT_ID, "ts": "1.0"},
            {"text": "CI 说的", "bot_id": "B_ci", "ts": "2.0"},
        ],
        auth_boom=True,
    )

    messages = await reader.fetch_thread(LOCATOR, 10)

    assert [m.is_self for m in messages] == [False, False]
    assert client.replies_calls == 1, "身份拉取失败不该影响线程拉取"


async def test_slack_身份只拉一次() -> None:
    """每次拉线程都打一次 auth.test 会额外烧掉本就紧张的配额。"""
    reader, client = _slack([{"text": "x", "user": "U_human", "ts": "1.0"}])

    await reader.fetch_thread(LOCATOR, 10)
    await reader.fetch_thread(LOCATOR, 10)

    assert client.auth_calls == 1
    assert client.replies_calls == 2


async def test_slack_身份拉取失败后不再重试() -> None:
    """权限不足是稳定失败，重试只是每次白打一发。"""
    reader, client = _slack([{"text": "x", "user": "U_human", "ts": "1.0"}], auth_boom=True)

    await reader.fetch_thread(LOCATOR, 10)
    await reader.fetch_thread(LOCATOR, 10)

    assert client.auth_calls == 1


async def test_slack_显式传入身份则不打auth_test() -> None:
    client = FakeSlackClient([{"text": "我说的", "bot_id": OUR_BOT_ID, "ts": "1.0"}])
    reader = SlackThreadReader(client, bot_id=OUR_BOT_ID)  # type: ignore[arg-type]

    messages = await reader.fetch_thread(LOCATOR, 10)

    assert client.auth_calls == 0
    assert [m.is_self for m in messages] == [True]


async def test_slack_空文本消息被丢弃() -> None:
    """纯附件/纯 block 消息没有可用文本。"""
    reader, _ = _slack(
        [
            {"text": "  ", "user": "U_human", "ts": "1.0"},
            {"user": "U_human", "ts": "2.0"},
            {"text": "有内容", "user": "U_human", "ts": "3.0"},
        ]
    )

    assert [m.text for m in await reader.fetch_thread(LOCATOR, 10)] == ["有内容"]


async def test_slack_拉取失败返回空历史() -> None:
    """端口契约：拉不到就空着。线程被删、bot 不在频道、限流都走这里。"""
    reader, _ = _slack([{"text": "x", "user": "U", "ts": "1.0"}], replies_boom=True)

    assert await reader.fetch_thread(LOCATOR, 10) == []


async def test_slack_按limit截取最近的() -> None:
    """conversations.replies 的 limit 是分页大小，与「最近 N 条」不完全等同。"""
    reader, _ = _slack(
        [{"text": f"第 {i} 句", "user": "U_human", "ts": f"{i}.0"} for i in range(5)]
    )

    messages = await reader.fetch_thread(LOCATOR, 2)

    assert [m.text for m in messages] == ["第 3 句", "第 4 句"]


# ===== 飞书 =====

OUR_OPEN_ID = "ou_self"
FEISHU_LOCATOR = ThreadLocator(platform="feishu", channel_id="oc_1", thread_ref="om_root")


class _Obj:
    """按属性访问的简易替身 —— 读取器全程用 getattr 取字段。"""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def _item(
    text: str,
    sender_id: str,
    *,
    sender_type: str = "user",
    message_id: str = "om_x",
    root_id: str | None = "om_root",
    msg_type: str = "text",
    create_time: str = "1700000000000",
) -> _Obj:
    import json as _json

    return _Obj(
        message_id=message_id,
        root_id=root_id,
        msg_type=msg_type,
        body=_Obj(content=_json.dumps({"text": text}, ensure_ascii=False)),
        sender=_Obj(id=sender_id, sender_type=sender_type),
        create_time=create_time,
    )


class FakeLarkClient:
    def __init__(self, items: list[_Obj], *, code: int = 0, boom: bool = False) -> None:
        self._items = items
        self._code = code
        self._boom = boom
        self.list_calls = 0
        self.config = object()
        outer = self

        class _Message:
            async def alist(self, request):
                outer.list_calls += 1
                if outer._boom:
                    raise ConnectionError("网络挂了")
                return _Obj(code=outer._code, msg="ok", data=_Obj(items=list(outer._items)))

        self.im = _Obj(v1=_Obj(message=_Message()))


def _patch_identity(monkeypatch, open_id: str | None, *, boom: bool = False) -> list[int]:
    """替换 lark SDK 的身份拉取，返回一个记录调用次数的可变计数器。"""
    calls: list[int] = []

    async def fake_fetch(config):
        calls.append(1)
        if boom:
            raise ConnectionError("凭据不对")
        return _Obj(open_id=open_id) if open_id is not None else None

    import lark_oapi.channel.bot_identity as mod

    monkeypatch.setattr(mod, "fetch_bot_identity", fake_fetch)
    return calls


async def test_飞书_自己的回复标为self(monkeypatch) -> None:
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(FakeLarkClient([_item("我建议看网关日志", OUR_OPEN_ID, sender_type="app")]))  # type: ignore[arg-type]

    messages = await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert [m.is_self for m in messages] == [True]
    assert messages[0].render() == "AI: 我建议看网关日志"


async def test_飞书_别的应用不算self(monkeypatch) -> None:
    """本次修复的核心。原判定 `sender_type == "app"` 让群里任何应用都命中 ——
    而那个 or 分支是唯一活着的分支，因为 bot_open_id 从来没有调用方传值。"""
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(  # type: ignore[arg-type]
        FakeLarkClient(
            # 接口按 ByCreateTimeDesc 返回，故这里是「新的在前」，读取器会反转
            [
                _item("磁盘使用率 91%", "ou_alert_bot", sender_type="app"),
                _item("构建 #412 失败", "ou_ci_bot", sender_type="app"),
            ]
        )
    )

    messages = await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert [m.is_self for m in messages] == [False, False]
    assert [m.render() for m in messages] == [
        "ou_ci_bot: 构建 #412 失败",
        "ou_alert_bot: 磁盘使用率 91%",
    ], "该按普通参与者渲染，且反转成时间正序"


async def test_飞书_身份拉取失败时不认领任何消息(monkeypatch) -> None:
    _patch_identity(monkeypatch, None, boom=True)
    reader = FeishuThreadReader(  # type: ignore[arg-type]
        FakeLarkClient(
            [
                _item("我说的", OUR_OPEN_ID, sender_type="app"),
                _item("CI 说的", "ou_ci_bot", sender_type="app"),
            ]
        )
    )

    messages = await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert [m.is_self for m in messages] == [False, False]
    assert len(messages) == 2, "身份拉不到不该影响线程内容"


async def test_飞书_身份只拉一次(monkeypatch) -> None:
    calls = _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(FakeLarkClient([_item("话", "ou_user1")]))  # type: ignore[arg-type]

    await reader.fetch_thread(FEISHU_LOCATOR, 10)
    await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert len(calls) == 1


async def test_飞书_显式传入身份则不拉取(monkeypatch) -> None:
    """连接器启动时已经拉过一份，传进来就不必再打一次。"""
    calls = _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(  # type: ignore[arg-type]
        FakeLarkClient([_item("我说的", OUR_OPEN_ID, sender_type="app")]),
        bot_open_id=OUR_OPEN_ID,
    )

    messages = await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert calls == []
    assert [m.is_self for m in messages] == [True]


async def test_飞书_只保留本线程的消息(monkeypatch) -> None:
    """没有「按根消息拉整串」的接口，只能拉会话最近一批再客户端过滤。"""
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(  # type: ignore[arg-type]
        FakeLarkClient(
            [
                _item("本线程的回复", "ou_user1", root_id="om_root"),
                _item("别的话题", "ou_user2", root_id="om_other", message_id="om_y"),
                _item("根消息自身", "ou_user1", root_id=None, message_id="om_root"),
            ]
        )
    )

    messages = await reader.fetch_thread(FEISHU_LOCATOR, 10)

    assert [m.text for m in messages] == ["根消息自身", "本线程的回复"], "倒序拉取后反转成正序"


async def test_飞书_非文本消息被丢弃(monkeypatch) -> None:
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(  # type: ignore[arg-type]
        FakeLarkClient(
            [
                _item("图片说明", "ou_user1", msg_type="image"),
                _item("文本内容", "ou_user1"),
            ]
        )
    )

    assert [m.text for m in await reader.fetch_thread(FEISHU_LOCATOR, 10)] == ["文本内容"]


async def test_飞书_接口报错返回空历史(monkeypatch) -> None:
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(FakeLarkClient([], code=99991663))  # type: ignore[arg-type]

    assert await reader.fetch_thread(FEISHU_LOCATOR, 10) == []


async def test_飞书_请求异常返回空历史(monkeypatch) -> None:
    _patch_identity(monkeypatch, OUR_OPEN_ID)
    reader = FeishuThreadReader(FakeLarkClient([], boom=True))  # type: ignore[arg-type]

    assert await reader.fetch_thread(FEISHU_LOCATOR, 10) == []
