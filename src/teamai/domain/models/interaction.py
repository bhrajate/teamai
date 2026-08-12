"""Agent 交互记录领域模型。

与 `AuditLog` 的分工：审计记「发生了什么动作」（枚举 + 小字典，永久留存），
本模型记「模型看到了什么、回了什么」（提示词与响应全文，按保留期清理）。
把两者合成一张表的话，要么审计被大字段拖胖、要么交互内容被迫塞进
`AuditLog.detail` 的 JSON 里而无法按字段查询与统计。

存在的理由是「可复现」：回答错了、越权调了工具、token 烧超了，都需要还原
当时实际组装出的提示词。`audit_logs` 只有动作枚举，还原不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InteractionResult(Enum):
    DONE = "DONE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


@dataclass
class AgentInteraction:
    """一次 Agent 调用的完整留痕。

    `context_refs` 存的是引用（记忆条目 id、线程历史条数），不是内容快照：
    管理员删掉某条记忆后，审计链仍能指出「当时引用过它」，但库里不会留下
    第二份副本 —— 否则「删除记忆」就成了假的。
    """

    id: str
    task_id: str
    channel_instance_id: str
    thread_ref: str
    user_prompt: str
    system_prompt: str
    model_level: str
    requester_id: str | None = None
    # 实际生效的模型 ID。light 档降级到备用模型时与配置里的主模型不同，
    # 成本归因必须按这个算。
    model_id: str = ""
    response: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    result: InteractionResult = InteractionResult.DONE
    error: str | None = None
    context_refs: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out
