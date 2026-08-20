"""Skill 仓储抽象。

两个「按频道」的方法有意分开，它们答的不是同一个问题：

- :meth:`list_for_channel` 答「agent 这次 run 能用哪些」→ 会过滤全局 enabled；
- :meth:`list_channel_skill_ids` 答「管理页该勾上哪些」→ 不过滤。

合成一个的话，管理员在全局停用一个 skill 后再打开频道页，会看到勾选被凭空
取消，误以为关联关系丢了 —— 而库里那行还在，重新勾一次是空操作。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.skill import Skill, SkillFile


class SkillRepository(ABC):
    """读取 skill 的方法一律**连附带文件一起装上**（``Skill.files``）。

    不给「要不要带文件」的开关：agent 侧必须带（工具执行时不能碰数据库），
    管理页也要显示文件列表。留一个开关就意味着有一条「忘了带」的路径，而它的
    表现是模型看不到文件清单 —— 没有报错，只是能力静默缺失。
    """

    @abstractmethod
    async def list_all(self) -> list[Skill]:
        """全局 skill 库，按 name 排序。管理页与清单展示都用它。"""
        ...

    @abstractmethod
    async def get(self, skill_id: str) -> Skill | None: ...

    @abstractmethod
    async def find_by_name(self, name: str) -> Skill | None:
        """按 name 查。name 全局唯一 —— 模型是照名字调 ``load_skill`` 的。"""
        ...

    @abstractmethod
    async def upsert(self, skill: Skill) -> None: ...

    @abstractmethod
    async def delete(self, skill_id: str) -> None:
        """删 skill，并一并删掉它的全部频道关联（实现方负责级联）。

        留下孤儿关联行的后果不是报错而是「幽灵启用」：换个 skill 复用了同一个
        id 时（不会发生，但迁移/导入场景下 id 是外部给的）那些频道会凭空启用它。
        """
        ...

    @abstractmethod
    async def list_for_channel(self, channel_instance_id: str) -> list[Skill]:
        """该频道**当前可用**的 skill：已关联 且 全局 enabled。

        agent 侧只该看到这个结果 —— 全局停用的 skill 不进清单、也载不进来。
        """
        ...

    @abstractmethod
    async def list_channel_skill_ids(self, channel_instance_id: str) -> list[str]:
        """该频道关联的全部 skill id，**不过滤** enabled。供管理页回显勾选。"""
        ...

    # ---- 附带文件 ----

    @abstractmethod
    async def get_file(self, skill_id: str, file_id: str) -> SkillFile | None:
        """按 id 取一个文件。``skill_id`` 一并给出以确认归属。"""
        ...

    @abstractmethod
    async def find_file_by_path(self, skill_id: str, path: str) -> SkillFile | None:
        """按 path 查。同一 skill 内 path 唯一，用于建时查重名。"""
        ...

    @abstractmethod
    async def upsert_file(self, file: SkillFile) -> None: ...

    @abstractmethod
    async def delete_file(self, skill_id: str, file_id: str) -> None: ...

    @abstractmethod
    async def set_channel_skills(self, channel_instance_id: str, skill_ids: list[str]) -> None:
        """覆盖式设置频道启用的 skill（先清后插）。

        覆盖而非增删两个端点：管理页交给用户的就是一组勾选框，提交的是「最终
        应该是这些」。做成 add/remove 的话，前端得自己算差集，两端各存一份状态
        就会不一致。
        """
        ...
