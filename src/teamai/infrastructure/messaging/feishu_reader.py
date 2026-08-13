"""飞书线程读取（ThreadReader 实现）。

飞书没有「按根消息 ID 拉整串回复」的接口，这是与 Slack 最大的结构性差异：

- `im/v1/messages` 的 `container_id_type` 只接受 `chat` 与 `thread`；
- 其中 `thread` 要的是话题群的 thread_id（`omt_` 前缀），而我们持有的 thread_ref
  是根消息的 message_id（`om_` 前缀，见 adapters/feishu/translator.py 取
  `root_id or message_id`）—— 两者不是一回事，直接传会报参数错误。

故取 `container_id_type="chat"` 拉该会话最近一批消息，再在客户端按
`root_id == thread_ref or message_id == thread_ref` 过滤出这一串。代价是多拉一些
消息后丢掉，换来的是不依赖「必须是话题群」这个前提 —— 普通群里的回复串同样能拿到。

`sort_type="ByCreateTimeDesc"` 取最近的，避免从会话开头翻页。拉取页大小按
`limit` 放大若干倍（同一线程的消息在会话流里是稀疏的），并设上限防止一次拉太多。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import lark_oapi
from lark_oapi.api.im.v1.model.list_message_request import ListMessageRequest

from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader
from teamai.infrastructure.messaging.feishu import build_lark_client

logger = logging.getLogger(__name__)

# 为凑够同一线程的 limit 条，需要在会话流里多拉几倍。
# 线程消息在群聊里通常是稀疏的（夹杂其他话题），倍数太小会拉不满。
FETCH_MULTIPLIER = 5
# 单次拉取的硬上限。飞书 list 接口 page_size 上限为 50。
MAX_PAGE_SIZE = 50


def _to_datetime(create_time: str | None) -> datetime | None:
    """飞书的 create_time 是毫秒时间戳字符串。"""
    if not create_time:
        return None
    try:
        return datetime.fromtimestamp(int(create_time) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


def _extract_text(item) -> str:
    """从 message 的 body.content（JSON 字符串）里取纯文本。

    只处理 text 类型：与入向翻译（adapters/feishu/translator.py）保持一致，
    图片/文件/卡片消息在本设计里不进上下文。
    """
    if getattr(item, "msg_type", "") != "text":
        return ""
    body = getattr(item, "body", None)
    raw = getattr(body, "content", "") if body is not None else ""
    if not raw:
        return ""
    try:
        return (json.loads(raw).get("text") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""


class FeishuThreadReader(ThreadReader):
    def __init__(self, client: lark_oapi.Client | None = None, bot_open_id: str = "") -> None:
        self._client = client or build_lark_client()
        # 判定哪条历史消息是本 bot 自己发的。此前这个参数没有任何调用方传值
        # （container 里是 FeishuThreadReader()），恒为空串，于是判定只剩
        # `sender_type == "app"` 那半边 —— 频道里任何应用发的消息都被标成自己的。
        # 现在改为空值时首次拉取自行取一次，见 _resolve_identity。
        self._bot_open_id = bot_open_id
        # 区分「还没问过」与「问过且失败」：失败也不再重试，否则每次拉线程都白打
        # 一次身份接口（凭据不对是稳定失败，重试不会变好）。
        self._identity_resolved = bool(bot_open_id)

    async def _resolve_identity(self) -> None:
        """取本 bot 的 open_id，仅首次调用时打一次 /open-apis/bot/v3/info。

        连接器启动时也拉同一份身份（mention 判定要用），但那份在 adapters 层、
        且 worker 进程根本不起连接器。读取器自己取一次更省事，也让「拉线程历史」
        这件事不依赖装配顺序。失败不抛：拿不到身份只会让 is_self 全为假。
        """
        if self._identity_resolved:
            return
        self._identity_resolved = True
        try:
            from lark_oapi.channel.bot_identity import fetch_bot_identity

            identity = await fetch_bot_identity(self._client.config)
            if identity is not None:
                self._bot_open_id = identity.open_id
                logger.info(f"飞书 bot open_id: {self._bot_open_id}")
            else:
                logger.warning("飞书 bot 身份拉取失败，自己的历史回复将标不出来")
        except Exception as exc:
            logger.warning(f"飞书 bot 身份拉取异常，自己的历史回复将标不出来: {exc}")

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        await self._resolve_identity()
        page_size = min(max(limit * FETCH_MULTIPLIER, limit), MAX_PAGE_SIZE)
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(locator.channel_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(page_size)
            .build()
        )
        try:
            resp = await self._client.im.v1.message.alist(request)
        except Exception as exc:
            logger.warning(f"飞书会话拉取失败 {locator.channel_id}: {exc}")
            return []
        if resp.code != 0:
            logger.warning(f"飞书会话拉取返回错误 {locator.channel_id}: {resp.code} {resp.msg}")
            return []

        items = getattr(resp.data, "items", None) or []
        out: list[ThreadMessage] = []
        for item in items:
            # 过滤出属于本线程的：根消息自身，或 root_id 指向它的回复
            root_id = getattr(item, "root_id", None)
            message_id = getattr(item, "message_id", None)
            if root_id != locator.thread_ref and message_id != locator.thread_ref:
                continue
            text = _extract_text(item)
            if not text:
                continue
            sender = getattr(item, "sender", None)
            sender_id = getattr(sender, "id", "") if sender is not None else ""
            out.append(
                ThreadMessage(
                    author_id=sender_id or "",
                    text=text,
                    ts=_to_datetime(getattr(item, "create_time", None)),
                    # 只认自己的 open_id。原先还 or 了 `sender_type == "app"`，
                    # 那是「某个应用发的」—— 群里的 CI 通知、告警机器人全都命中，
                    # 于是它们的消息被渲染成 AI:，模型会以为是自己上一轮说的。
                    # 身份未知时一律为假：宁可把自己的回复降级成普通参与者，
                    # 也不能把别人的话认领成自己的。
                    is_self=bool(self._bot_open_id) and sender_id == self._bot_open_id,
                )
            )
        # 拉的是倒序，反转成正序后取最近 limit 条
        out.reverse()
        return out[-limit:]

    async def aclose(self) -> None:
        """理由同 FeishuPublisher.aclose：lark SDK 每次请求自建并关闭连接。"""
        return None
