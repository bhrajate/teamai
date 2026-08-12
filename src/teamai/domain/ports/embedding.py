"""文本向量化端口。

此前 `MemoryService` 收一个 duck-typed 的 `embedder` 可调用对象，但组合根从未
注入过它 —— 于是向量写入与语义检索整条链路从未运行，记忆检索一直走无界全表
扫描的回落路径。声明成端口是为了让「有没有装配它」这件事在类型上可见。

`dimensions` 必须由实现方声明：向量库建集合时要用它，而各模型维度不同
（text-embedding-3-small 是 1536，bge-small 是 384）。硬编码某个值会让换模型
时建出维度不匹配的集合，写入时才报错。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    @property
    @abstractmethod
    def dimensions(self) -> int:
        """向量维度。向量库据此建集合。"""
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """是否真能产出向量。

        未配置凭据时装配的空实现返回 False，用例层据此跳过向量检索、直接走
        有界的时间倒序回落 —— 而不是发一次注定失败的请求再靠 except 兜住。
        """
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """把文本转成向量。不可用或失败时返回空列表，由调用方降级。"""
        ...
