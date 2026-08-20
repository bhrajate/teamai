"""Agent 执行检查点领域模型。

一次 agent run 在**干净的轮边界**（历史里没有未被应答的工具调用）留下的消息
历史快照。worker 崩溃后，超时巡检据此重新入队，续跑只执行剩余的工具轮次。

设计与 pydantic-ai 的行为实测见 docs/SPEC-agent-checkpoint.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TaskCheckpoint:
    """一个任务的最新检查点。按 task_id 覆盖写，不留历史。

    只留最新的那份：续跑永远从最近的干净边界开始。留一串历史要配 GC，
    而换不来任何东西 —— 除了时间旅行式调试，那不在本功能范围内。
    """

    task_id: str
    # 序列化后的消息历史。**对领域不透明** —— 只有 infrastructure 的 gateway
    # 知道它实际是 pydantic-ai 的 ModelMessage 列表。
    #
    # 存 bytes 而非在领域层重新描述消息结构，理由与 ports/tools.py 的
    # ToolBundle 一致：翻译层一旦失真就会丢东西，而这里失真的后果格外隐蔽 ——
    # 续跑时上下文少一段，模型照着残缺历史继续答，没有任何报错。
    messages: bytes
    # 截至本检查点的累计 token 消耗（跨全部执行段）。
    #
    # 每落一个检查点就据此补扣预算增量 —— 不等 run 结束一次扣完，那样
    # worker 崩溃时这一段的 token 永远不会被计费。
    tokens_used: int
    # 已续跑次数。超过配置上限即放弃续跑、收敛到 FAILED。
    attempts: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
