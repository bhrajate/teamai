"""工具提供方端口。

领域层只关心「按频道白名单取一份工具集」这件事，不关心工具集长什么样。
故 ``ToolBundle`` 是**不透明句柄**：领域与用例层都不得对它做任何解构，
只负责原样递给 :class:`~teamai.domain.ports.llm.LLMGateway`；
只有 infrastructure 层的实现方知道它实际是 pydantic-ai 的 toolset。

这里刻意不定义「工具描述符」（name/description/参数 schema）那类结构。
若在领域层重新描述工具，就得由 infrastructure 把它翻译回 SDK 的工具对象，
翻译层一旦失真就会丢掉参数 schema 与调用前校验 —— 那正是本项目此前
自造 ``BaseTool`` 踩过的坑。工具的形状交给 SDK，领域只管授权范围。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from teamai.domain.models.skill import Skill

ToolBundle: TypeAlias = object
"""一份已按频道权限裁剪好的工具集。对领域层不透明，不可解构。"""


class ToolProvider(ABC):
    """按频道白名单裁剪工具集。实现方负责与具体 Agent SDK 交互。"""

    @abstractmethod
    def register(self, tool: Any) -> None:
        """注册一个工具（对领域不透明，形状由实现方与 SDK 约定）。

        McpService 装载 MCP server 时经此接口补注册动态工具。
        """
        ...

    @abstractmethod
    def for_channel(
        self,
        allowed: list[str],
        skills: Sequence[Skill] | None = None,
        approvals: Mapping[str, int] | None = None,
    ) -> ToolBundle | None:
        """裁出本次调用可见的工具集。

        未授权的工具不应出现在返回的工具集中 —— 模型无法调用它看不见的工具，
        因此鉴权只发生在这一步，不需要在工具执行时二次检查。

        ``skills`` 非空时额外挂 ``load_skill``（按名取回正文 + 文件清单）；其中
        若有 skill 带附带文件，再挂 ``read_skill_file``（按路径取回文件内容）。
        这是三级渐进式披露，见 :mod:`teamai.domain.models.skill`。两点有意如此：

        **不受 allowed 白名单管制。** 频道启用某个 skill 这个动作本身就是授权，
        再要求管理员去策略页补一条 ``load_skill`` 才生效，等于同一件事配两处，
        漏配的表现是「skill 明明启用了，模型却说没有这个能力」。

        **传的是完整 Skill（含 content），不是 id 列表。** 于是取正文是纯内存
        查表，工具内不碰数据库。若让工具自己查库，它会在 agent run 进行中复用
        组合根那个共享 ``AsyncSession`` —— 而 AsyncSession 不允许并发使用，
        这正是 ``container.open_job_scope`` 文档里记的那类「another operation
        is in progress」故障，且同样会被 run 的顶层兜底吞成一句错误文本。

        ``approvals`` 是「工具名 → 需要几个批准」（取自
        :meth:`PermissionPolicy.approvals_needed` 的配置）。其中的工具被调用时，
        实现方须让本次 run **中断而不执行**，并把待批的调用交回用例层。

        这一层只管「要不要批」；「谁能批、够不够数」是用例层的判定
        （application/approval.py）—— 那些依赖 task.requester_id 与审批人名单，
        属业务规则，不该下沉到工具集里。

        无任何可用工具且无 skill 时返回 ``None``，表示本次调用不挂工具。
        """
        ...
