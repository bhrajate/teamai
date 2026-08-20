"""权限策略领域模型：PermissionPolicy 与 AmbientRule。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AmbientRule:
    trigger: str
    params: dict[str, Any] = field(default_factory=dict)
    action: str = "nudge"


@dataclass
class PermissionPolicy:
    id: str
    channel_instance_id: str
    allowed_tools: list[str] = field(default_factory=list)
    ambient_rules: list[AmbientRule] = field(default_factory=list)
    # 需要人工审批的工具 → 需要几个批准。1 = 单人批，2 = 四眼原则的双批。
    #
    # 用 dict 而非 list：list 只能表达「要不要批」，第二个危险工具出现时就得再
    # 加一个字段。MCP 工具是外部的、风险面不可控，早晚要区分档位。
    #
    # 不做「风险分数 → 档位」那套（业界常见做法）：我们没有评分系统，硬造一个
    # 只会得到假精度。直接配档位等价于业界的「硬性覆盖」。
    approval_required_tools: dict[str, int] = field(default_factory=dict)
    # 频道级审批人。审批人的第二级来源，第一级是 task.owner_id。
    #
    # ⚠️ 这个列表为空且任务无 owner_id 时，需审批的工具**拒绝执行**而非放宽 ——
    # 与 allowed_tools 的语义一致：白名单里没有的工具不是「谁都能用」。
    approver_ids: list[str] = field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=_utcnow)

    def can_use_tool(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def approvals_needed(self, tool_name: str) -> int:
        """该工具需要几个批准；0 表示不需要审批。

        按前缀也匹配一次：``mcp__server`` 配了审批时，该 server 展开出的
        ``mcp__server__tool`` 全部继承 —— 与 allowed_tools 的 server 级挂载
        对称（见 infrastructure/tools/registry.py）。否则管理员得为一个 MCP
        server 的每个动态工具各配一条，而那些工具名要连上 server 才知道。
        """
        if (exact := self.approval_required_tools.get(tool_name)) is not None:
            return exact
        for configured, count in self.approval_required_tools.items():
            if tool_name.startswith(f"{configured}__"):
                return count
        return 0
