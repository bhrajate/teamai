"""Embedder 端口的实现：OpenAI 兼容的 embeddings 接口，无凭据时装空实现。

为什么单独走 OpenAI 兼容协议而不复用 LLMGateway 的 pydantic-ai：pydantic-ai 面向
「带工具的对话调用」，不提供 embeddings 抽象。而 embeddings 的 `/v1/embeddings`
是各家（OpenAI、通义、智谱、本地 vLLM/Ollama、各类中转）事实上的统一协议，
直接用 openai SDK 打它反而是最少适配的做法。

未配置 embedding 凭据时装 `NullEmbedder`：`available` 为假，`MemoryService` 据此
跳过向量检索、走按时间倒序的有界回落。这比「装一个会失败的实现再靠 except 兜」
更好 —— 后者每次检索都白发一个请求，而且缺陷会被日志噪音掩盖。改造前的实现
根本没注入 embedder，向量链路从未运行过，正是被静默降级藏住的。
"""

from __future__ import annotations

import logging

from teamai.config import Settings
from teamai.domain.ports import Embedder

logger = logging.getLogger(__name__)

# 没有 embedding 时到底失去了什么。
#
# 这段话值得写全并且用 warning 而不是 info：前两条是「这次回答差一点」，第三条是
# **记忆库会持续劣化**，而且要几周才看得出来。此前这里只说「语义检索关闭」，
# 而那是三层后果里最轻的一层。
#
# 第三条的机制：蒸馏靠 `MemoryService.find_similar` 取候选给模型比对，没有向量时
# 它回落成「最近 10 条」—— 于是三个月前那条「超时 3 秒」永远进不了候选，模型看不到
# 冲突就判 ADD，两条并列成为现行。去重与取代对旧记忆基本是瞎的。
_DEGRADED_CONSEQUENCES = (
    "装 NullEmbedder。三层后果："
    "① 语义检索关闭，记忆检索回落到按时间倒序取最近若干条；"
    "② 手工写入的冲突检查退化为字面比对，语义上矛盾的记忆查不出来；"
    "③ 蒸馏的去重与取代对旧记忆基本失效（候选退化成「最近 10 条」，"
    "更早的矛盾记忆进不了比对，会持续并列堆积）。"
)


class NullEmbedder(Embedder):
    """不产出向量的空实现。维度取一个常见值，但不会被用到。"""

    @property
    def dimensions(self) -> int:
        return 1536

    @property
    def available(self) -> bool:
        return False

    async def embed(self, text: str) -> list[float]:
        return []


class OpenAICompatibleEmbedder(Embedder):
    """打 `/v1/embeddings` 的实现。

    维度由配置声明而非向模型问：不同模型维度不同，而向量库建集合时就要知道它，
    此时可能还没发过任何请求。配错会在首次 upsert 时被向量库拒绝 —— 这比默默
    建出一个维度不符的集合好。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "",
        dimensions: int = 1536,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._dimensions = dimensions
        self._client = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._model)

    def _ensure_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def embed(self, text: str) -> list[float]:
        if not self.available or not text.strip():
            return []
        try:
            client = self._ensure_client()
            resp = await client.embeddings.create(model=self._model, input=text)
        except Exception as exc:
            # 返回空列表由调用方降级（跳过向量写入 / 回落到时间倒序检索）。
            # 不抛：一次 embedding 失败不该让「存一条记忆」或「回答一个问题」失败。
            logger.warning(f"embedding 调用失败: {exc}")
            return []
        return list(resp.data[0].embedding)


def build_embedder(settings: Settings) -> Embedder:
    """按配置装配。凭据或模型名缺失即装空实现。

    api_key 缺省复用 `llm_api_key`：多数部署里 embedding 与对话走同一个网关、
    同一把 key，让用户为此多配一项没有必要。需要分开时配
    `embedding_api_key` 覆盖。
    """
    if not settings.embedding_model:
        logger.warning(f"未配置 embedding_model，{_DEGRADED_CONSEQUENCES}")
        return NullEmbedder()
    api_key = settings.embedding_api_key or settings.llm_api_key
    if not api_key:
        logger.warning(f"未配置 embedding 凭据，{_DEGRADED_CONSEQUENCES}")
        return NullEmbedder()
    return OpenAICompatibleEmbedder(
        model=settings.embedding_model,
        api_key=api_key,
        base_url=settings.embedding_base_url or settings.llm_base_url,
        dimensions=settings.embedding_dimensions,
    )
