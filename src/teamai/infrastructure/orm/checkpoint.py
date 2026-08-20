"""task_checkpoints 表：agent 执行到干净轮边界时的消息历史快照。

按 task_id 覆盖写，只留最新一份。worker 崩溃后由超时巡检据此续跑。

**单独建表而不并入 tasks**：messages 是序列化的消息历史，实测每个工具轮约
1.2 KB（十轮量级 12 KB），而 tasks 被列表端点与超时巡检反复全表扫。大字段与
热扫描表放一起会拖慢后者 —— 与记忆向量那次 TOAST 的实测教训同理
（单频道 7.5ms vs 全表 206ms）。

不做外键：与本项目其余表一致（task_id 在各表里都是裸字符串）。终态时的清理
由 TaskOrchestrator.transition 显式做。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


class TaskCheckpointModel(Base):
    __tablename__ = "task_checkpoints"

    # task_id 直接做主键：一个任务只保留最新检查点，故不需要独立的代理键。
    # 覆盖写靠 session.merge 落到这个主键上。
    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    messages: Mapped[bytes] = mapped_column(LargeBinary)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # ---- 工具审批（HITL）----
    #
    # 与检查点同表：两者都是同一个任务的执行期状态，主键都是 task_id，生命周期
    # 也一样（终态时一起清）。分表会让「取一个任务的执行状态」变成两次查询，
    # 且要各自维护清理逻辑。
    #
    # 待批的工具调用（PendingApproval 的序列化）。非空即任务卡在 WAITING_INPUT。
    pending_approval: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
