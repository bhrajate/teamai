"""平台连接器抽象。

进程入口只认识连接器：挂载 HTTP 入口、启动/关闭长连接都由连接器自理，
新增平台只需登记 build_connector，不再改动进程入口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import FastAPI


class PlatformConnector(ABC):
    name: str

    def mount(self, app: FastAPI) -> None:
        """挂 HTTP 入口。长连接模式下为 no-op。"""
        return None

    async def startup(self) -> None:
        """建立长连接等。HTTP 模式下为 no-op。"""
        return None

    @abstractmethod
    async def shutdown(self) -> None:
        """释放本平台持有的长连接与资源。"""
