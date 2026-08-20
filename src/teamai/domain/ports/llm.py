"""大模型调用端口。

与 queue / dedup 同理：契约由领域层声明，infrastructure 层用具体 SDK 实现。

端口只覆盖「发起一次带工具的模型调用」。预算核算、上下文压缩、审计留痕、
超限转 PAUSED 都是用例层的策略，不在此列。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from teamai.domain.ports.tools import ToolBundle


@dataclass(frozen=True)
class ApprovalDecision:
    """对一个待批调用的最终裁决，交给实现方去恢复执行。

    ``approved=False`` 时 ``reason`` 会被回灌给模型 —— 它据此向用户说明「这件事
    没做，因为…」，而不是当作工具执行失败去重试。所以拒绝理由要写给**模型**看，
    不是写给日志看。

    ``override_args`` 非空时用它替换模型原本给的参数：审批不是批/否二选一，
    人可以改完再放行（框架支持，已实测）。
    """

    approved: bool
    reason: str = ""
    override_args: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    """一个因等批准而被拦下的工具调用。

    ``tool_call_id`` 是框架分配的，恢复执行时必须原样传回 —— 它是「这个批准
    对应哪次调用」的唯一凭据。领域层不解释它的格式。
    """

    tool_call_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    """一次调用的结果。

    输入/输出 token 分开报，不只给 total：两者单价差数倍，只有 total 时
    按频道核算成本只能猜一个平均单价。`tokens` 保留为两者之和，预算控制器
    与审计沿用它 —— 配额限的是总量，不必区分。

    `model_id` 是**实际生效**的模型而非配置里的档位：light 档走
    FallbackModel(primary → fallback)，主模型失败时真正跑的是备用模型。
    只记档位会让成本归因按错误的单价计算。
    """

    output: str
    tokens: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    model_id: str = ""
    # 因等人工批准而中断的工具调用。非空即本次 run **没有跑完** ——
    # `output` 此时不是给用户的答复（框架会把待批清单塞进去），调用方须据此
    # 把任务转 WAITING_INPUT 而不是回复用户。
    #
    # 用 list 而非单个：模型可能在同一轮里发起多个需审批的调用。
    pending_approvals: list[ApprovalRequest] = field(default_factory=list)
    # 恢复执行所需的消息历史（与 CheckpointSink 交出的同一形态）。
    # 有待批时必然非空 —— 批准后要拿它继续跑。
    history: bytes | None = None

    @property
    def awaiting_approval(self) -> bool:
        return bool(self.pending_approvals)


class TokenBudgetExceeded(Exception):
    """本次调用触达 token 上限。

    实现方须把底层 SDK 的超限信号翻译成本异常。用例层据此把任务转 PAUSED，
    从而不必 catch 某个具体 SDK 的异常类型 —— 否则换 SDK 会直接改坏状态机。
    """


class CheckpointSink(Protocol):
    """执行中途的持久化回调，用于 worker 崩溃后续跑。

    ``messages`` 对领域**不透明** —— 只有实现方（gateway）知道它实际是什么，
    调用方只负责原样存起来、下次原样传回 ``run(history=...)``。与
    :data:`ToolBundle` 同一套理由：若在领域层重新描述消息结构，就得由
    infrastructure 翻译回 SDK 对象，而翻译层一旦失真，表现是续跑时上下文
    少一段、模型照着残缺历史继续答，没有任何报错。

    ``tokens_total`` 是**本段**（本次 run）截至此刻的累计消耗，不含续跑前
    各段。调用方要自己加基数才是任务总量 —— 这样才能让 ``token_limit``
    与「当前剩余配额」直接对应，详见 gateway 实现的说明。

    实现方须保证异常不外抛：gateway 会兜住并只记 warning。「检查点落不下」
    远好于「让一次正在成功的 run 失败」—— 前者最坏是崩溃后从更早的点重来。
    """

    async def __call__(self, messages: bytes, tokens_total: int) -> None: ...


class LLMGateway(ABC):
    """一次模型调用。实现方负责模型选择、重试与工具执行循环。"""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
        history: bytes | None = None,
        on_checkpoint: CheckpointSink | None = None,
        approval_results: dict[str, ApprovalDecision] | None = None,
    ) -> LLMResult:
        """执行一次调用并返回输出与 token 消耗。

        Args:
            prompt: 用户提示词（已含记忆与线程历史等上下文）。
            model_level: 模型档位，``light`` 或 ``full``。具体模型 ID 由实现方决定。
            system_prompt: 系统提示词。
            tools: 已按频道权限裁剪的工具集，见 :data:`ToolBundle`。
            token_limit: 本次调用的 token 上限；触达时抛 :class:`TokenBudgetExceeded`。
            history: 续跑起点 —— 此前某次调用在干净轮边界上留下的消息历史
                （由 ``on_checkpoint`` 交出）。**非空时 ``prompt`` 被忽略**：
                原始提问就是历史的第一条，再给一次会被 SDK 拒绝。这个取舍收在
                实现方内部，调用方照常传 prompt 即可。
            on_checkpoint: 非空时，实现方应在每个**干净**的轮边界回调它一次。
                「干净」指历史里没有未被应答的工具调用 —— 带悬空调用的历史无法
                用于续跑。纯文本轮次不必回调：那种轮次重跑只花 token、无副作用，
                不值得付持久化的代价。

            approval_results: 审批结果，键是 ``ApprovalRequest.tool_call_id``。
                与 ``history`` 配合使用 —— 拿上次中断时的历史 + 这批结果继续跑。
                批准的工具会执行（可带覆盖参数），拒绝的不执行、理由回灌给模型。

        三个新增参数都可选，不传即退回「一次调用、中途不可观测」的原有行为。
        """
        ...
