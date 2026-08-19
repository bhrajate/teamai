"""McpServerRepository 的 SQLAlchemy 实现。

headers 在库里是 JSON 对象字符串，读写在此处转换（对齐 SQLPolicyRepository）。
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.mcp import McpServer
from teamai.domain.repositories.mcp import McpServerRepository
from teamai.infrastructure.orm.mcp import McpServerModel


def _server_to_model(s: McpServer) -> McpServerModel:
    return McpServerModel(
        id=s.id,
        channel_instance_id=s.channel_instance_id,
        name=s.name,
        url=s.url,
        headers=json.dumps(s.headers),
        enabled=s.enabled,
        last_error=s.last_error,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _model_to_server(m: McpServerModel) -> McpServer:
    return McpServer(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        name=m.name,
        url=m.url,
        headers=json.loads(m.headers or "{}"),
        enabled=m.enabled,
        last_error=m.last_error,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SQLMcpServerRepository(McpServerRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_channel(self, channel_instance_id: str) -> list[McpServer]:
        stmt = (
            select(McpServerModel)
            .where(McpServerModel.channel_instance_id == channel_instance_id)
            .order_by(McpServerModel.name)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_server(m) for m in rows]

    async def list_enabled(self) -> list[McpServer]:
        stmt = select(McpServerModel).where(McpServerModel.enabled.is_(True))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_server(m) for m in rows]

    async def get(self, channel_instance_id: str, server_id: str) -> McpServer | None:
        stmt = select(McpServerModel).where(
            McpServerModel.id == server_id,
            McpServerModel.channel_instance_id == channel_instance_id,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_server(m) if m else None

    async def find_by_name(self, channel_instance_id: str, name: str) -> McpServer | None:
        stmt = select(McpServerModel).where(
            McpServerModel.channel_instance_id == channel_instance_id,
            McpServerModel.name == name,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_server(m) if m else None

    async def upsert(self, server: McpServer) -> None:
        # 只 flush 不 commit：事务边界由用例层（UoW）声明，
        # 见 tests/unit/test_repository_commit.py 的约束说明。
        await self._session.merge(_server_to_model(server))
        await self._session.flush()

    async def delete(self, channel_instance_id: str, server_id: str) -> None:
        stmt = delete(McpServerModel).where(
            McpServerModel.id == server_id,
            McpServerModel.channel_instance_id == channel_instance_id,
        )
        await self._session.execute(stmt)
        await self._session.flush()
