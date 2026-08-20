"""Skill 管理路由：全局库 CRUD、频道启用、附带文件。

内存 fake 仓储 + 真 TestClient（对齐 test_admin_mcp.py）：路由的校验、状态码与
响应形状是这里要锁的东西，仓储的真 SQL 行为在 test_skill_repository.py。

服务层用真的 SkillService —— 校验分布在路由与服务两侧，用真服务才能验到组合
后的行为（比如 set_channel_skills 丢弃不存在的 id）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamai.adapters.admin.skill import build_skill_router
from teamai.application.skill import SkillService
from teamai.domain.models import AuditLog
from teamai.domain.models.skill import FILE_MAX_BYTES, Skill, SkillFile
from teamai.domain.repositories.skill import SkillRepository
from teamai.domain.services import AuditLogWriter


class FakeSkillRepo(SkillRepository):
    """内存实现。文件与关联都存在字典里，语义对齐 SQL 实现。"""

    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}
        self.files: dict[str, SkillFile] = {}
        self.links: dict[str, list[str]] = {}

    def _attach(self, s: Skill) -> Skill:
        s.files = sorted(
            (f for f in self.files.values() if f.skill_id == s.id), key=lambda f: f.path
        )
        return s

    async def list_all(self) -> list[Skill]:
        return [self._attach(s) for s in sorted(self.skills.values(), key=lambda s: s.name)]

    async def get(self, skill_id: str) -> Skill | None:
        s = self.skills.get(skill_id)
        return self._attach(s) if s else None

    async def find_by_name(self, name: str) -> Skill | None:
        for s in self.skills.values():
            if s.name == name:
                return self._attach(s)
        return None

    async def upsert(self, skill: Skill) -> None:
        self.skills[skill.id] = skill

    async def delete(self, skill_id: str) -> None:
        self.skills.pop(skill_id, None)
        for fid in [k for k, v in self.files.items() if v.skill_id == skill_id]:
            del self.files[fid]
        for ch in self.links:
            self.links[ch] = [i for i in self.links[ch] if i != skill_id]

    async def list_for_channel(self, channel_instance_id: str) -> list[Skill]:
        ids = self.links.get(channel_instance_id, [])
        return [
            self._attach(s)
            for s in sorted(self.skills.values(), key=lambda s: s.name)
            if s.id in ids and s.enabled
        ]

    async def list_channel_skill_ids(self, channel_instance_id: str) -> list[str]:
        return list(self.links.get(channel_instance_id, []))

    async def set_channel_skills(self, channel_instance_id: str, skill_ids: list[str]) -> None:
        self.links[channel_instance_id] = list(dict.fromkeys(skill_ids))

    async def get_file(self, skill_id: str, file_id: str) -> SkillFile | None:
        f = self.files.get(file_id)
        return f if f and f.skill_id == skill_id else None

    async def find_file_by_path(self, skill_id: str, path: str) -> SkillFile | None:
        for f in self.files.values():
            if f.skill_id == skill_id and f.path == path:
                return f
        return None

    async def upsert_file(self, file: SkillFile) -> None:
        self.files[file.id] = file

    async def delete_file(self, skill_id: str, file_id: str) -> None:
        f = self.files.get(file_id)
        if f and f.skill_id == skill_id:
            del self.files[file_id]


class FakeAuditRepo:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        return [x for x in self.logs if x.channel_instance_id == channel_instance_id][:limit]


class NoopUow:
    """事务边界的空实现。真 UoW 的行为在 test_uow.py。"""

    async def __aenter__(self) -> NoopUow:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass
class FakeContainer:
    skills: SkillService
    audit_repo: FakeAuditRepo = field(default_factory=FakeAuditRepo)


@pytest.fixture
def repo() -> FakeSkillRepo:
    return FakeSkillRepo()


@pytest.fixture
def audit() -> FakeAuditRepo:
    return FakeAuditRepo()


@pytest.fixture
def client(repo: FakeSkillRepo, audit: FakeAuditRepo) -> AsyncIterator[TestClient]:
    service = SkillService(repo, AuditLogWriter(audit), NoopUow())
    app = FastAPI()
    app.include_router(build_skill_router(FakeContainer(skills=service, audit_repo=audit)))
    yield TestClient(app)


def _create(client: TestClient, **kw: Any):
    body = {
        "name": kw.pop("name", "code-review"),
        "description": kw.pop("description", "按团队规范审查 PR"),
        "content": kw.pop("content", "# 步骤\n\n1. 看 diff"),
        **kw,
    }
    return client.post("/skills", json=body)


# ---- 全局库 CRUD ----


def test_创建并列出(client: TestClient) -> None:
    r = _create(client)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "code-review"
    assert body["enabled"] is True
    assert body["files"] == []

    listed = client.get("/skills").json()
    assert [s["name"] for s in listed] == ["code-review"]
    # 列表带正文：管理页要直接编辑
    assert "1. 看 diff" in listed[0]["content"]


@pytest.mark.parametrize("name", ["Code-Review", "code_review", "code review", "代码审查", ""])
def test_非法name报422(client: TestClient, name: str) -> None:
    """name 要被模型原样打进 load_skill(name)，混排与空格会让它抄错。"""
    assert _create(client, name=name).status_code == 422


def test_description必填(client: TestClient) -> None:
    """它是模型判断该不该用这个技能的唯一依据，空着等于这个技能不可能被选中。"""
    assert _create(client, description="   ").status_code == 422


def test_description限长(client: TestClient) -> None:
    """每次 run 都常驻系统提示词，长度直接乘以调用次数。"""
    assert _create(client, description="很长" * 200).status_code == 422


def test_重名报409(client: TestClient) -> None:
    _create(client)
    assert _create(client).status_code == 409


def test_更新只改传入字段(client: TestClient) -> None:
    sid = _create(client).json()["id"]

    r = client.put(f"/skills/{sid}", json={"content": "# 新步骤"})

    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "# 新步骤"
    assert body["description"] == "按团队规范审查 PR"


def test_可以改名(client: TestClient) -> None:
    """与 MCP server 的 name 不同，skill 允许改名：模型每次都从当前清单读名字，
    不存在「白名单里残留旧名」的问题。"""
    sid = _create(client).json()["id"]

    r = client.put(f"/skills/{sid}", json={"name": "pr-review"})

    assert r.status_code == 200
    assert r.json()["name"] == "pr-review"


def test_改名撞已有报409(client: TestClient) -> None:
    _create(client, name="a")
    sid = _create(client, name="b").json()["id"]

    assert client.put(f"/skills/{sid}", json={"name": "a"}).status_code == 409


def test_改名成自己不报409(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    assert client.put(f"/skills/{sid}", json={"name": "code-review"}).status_code == 200


def test_全局停用(client: TestClient) -> None:
    sid = _create(client).json()["id"]

    r = client.put(f"/skills/{sid}", json={"enabled": False})

    assert r.json()["enabled"] is False


def test_删除(client: TestClient) -> None:
    sid = _create(client).json()["id"]

    assert client.delete(f"/skills/{sid}").json() == {"ok": True}
    assert client.get("/skills").json() == []


def test_操作不存在的skill报404(client: TestClient) -> None:
    assert client.put("/skills/skill_nope", json={"content": "x"}).status_code == 404
    assert client.delete("/skills/skill_nope").status_code == 404


# ---- 频道启用 ----


def test_频道页返回库与勾选状态(client: TestClient) -> None:
    """一次返回两者：分两次取会在「另一人正在增删技能」时让勾选指向不存在的行。"""
    sid = _create(client).json()["id"]
    _create(client, name="weekly-report", description="生成周报")

    body = client.get("/channels/ch_1/skills").json()

    assert [s["name"] for s in body["skills"]] == ["code-review", "weekly-report"]
    assert body["enabled_ids"] == []

    client.put("/channels/ch_1/skills", json={"skill_ids": [sid]})
    assert client.get("/channels/ch_1/skills").json()["enabled_ids"] == [sid]


def test_频道页不带正文(client: TestClient) -> None:
    """频道页只需要名字与描述来做勾选；正文是几 KB 散文，全带上会让这个响应
    比它承载的信息大两个数量级。"""
    _create(client)

    body = client.get("/channels/ch_1/skills").json()

    assert "content" not in body["skills"][0]
    assert set(body["skills"][0]) == {"id", "name", "description", "enabled"}


def test_全局停用后频道仍带勾(client: TestClient) -> None:
    """勾选答的是「这个频道勾了哪些」，不是「现在能不能用」。

    过滤掉的话，管理员全局停用后再打开频道页会看到勾选被凭空取消，
    以为关联关系丢了 —— 而库里那行还在，重新勾一次是空操作。
    """
    sid = _create(client).json()["id"]
    client.put("/channels/ch_1/skills", json={"skill_ids": [sid]})
    client.put(f"/skills/{sid}", json={"enabled": False})

    body = client.get("/channels/ch_1/skills").json()

    assert body["enabled_ids"] == [sid]
    assert body["skills"][0]["enabled"] is False


def test_覆盖式设置(client: TestClient) -> None:
    a = _create(client, name="a", description="A").json()["id"]
    b = _create(client, name="b", description="B").json()["id"]

    client.put("/channels/ch_1/skills", json={"skill_ids": [a, b]})
    r = client.put("/channels/ch_1/skills", json={"skill_ids": [b]})

    assert r.json()["enabled_ids"] == [b]


def test_不存在的id被静默丢弃(client: TestClient) -> None:
    """管理页的勾选基于它上一次拉到的列表；期间有人删了某个 skill 时，提交里
    就会有幽灵 id。为此报 422 会让用户面对一个自己无法理解也无法修正的错误。"""
    sid = _create(client).json()["id"]

    r = client.put("/channels/ch_1/skills", json={"skill_ids": [sid, "skill_ghost"]})

    assert r.json()["enabled_ids"] == [sid]


def test_skill_ids必须是字符串数组(client: TestClient) -> None:
    for bad in ({"skill_ids": "abc"}, {"skill_ids": [1, 2]}, {}):
        assert client.put("/channels/ch_1/skills", json=bad).status_code == 422


def test_删skill清掉频道关联(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    client.put("/channels/ch_1/skills", json={"skill_ids": [sid]})

    client.delete(f"/skills/{sid}")

    assert client.get("/channels/ch_1/skills").json()["enabled_ids"] == []


# ---- 附带文件 ----


def _add_file(client: TestClient, sid: str, **kw: Any):
    body = {
        "path": kw.pop("path", "checklist.md"),
        "description": kw.pop("description", "审查清单"),
        "content": kw.pop("content", "- 检查一\n- 检查二"),
        **kw,
    }
    return client.post(f"/skills/{sid}/files", json=body)


def test_创建文件(client: TestClient) -> None:
    sid = _create(client).json()["id"]

    r = _add_file(client, sid)

    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "checklist.md"
    assert body["skill_id"] == sid
    assert "检查一" in body["content"]
    # 字节数由后端算：前端按字符算会与 64 KB 上限对不上（一个汉字 3 字节）
    assert body["size_bytes"] == len("- 检查一\n- 检查二".encode())


def test_skill列表带文件摘要但不带内容(client: TestClient) -> None:
    """列表里每文件都带内容会让响应膨胀到不可用（64 KB × 文件数 × 技能数）。"""
    sid = _create(client).json()["id"]
    _add_file(client, sid, content="这段内容不该出现在列表里")

    (listed,) = client.get("/skills").json()

    (f,) = listed["files"]
    assert set(f) == {"id", "skill_id", "path", "description", "size_bytes"}
    assert "这段内容不该出现在列表里" not in str(listed)


def test_单取文件带内容(client: TestClient) -> None:
    """编辑某个文件时经此单取。"""
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid, content="完整正文").json()["id"]

    r = client.get(f"/skills/{sid}/files/{fid}")

    assert r.json()["content"] == "完整正文"


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "docs/", "../secret", "a/../../etc/passwd", "a b.md", "a$b.md"],
)
def test_非法路径报422(client: TestClient, path: str) -> None:
    sid = _create(client).json()["id"]
    assert _add_file(client, sid, path=path).status_code == 422


@pytest.mark.parametrize("path", ["a.md", "docs/a.md", "a/b/c.yaml", "run-it_2.sh"])
def test_合法路径(client: TestClient, path: str) -> None:
    sid = _create(client).json()["id"]
    assert _add_file(client, sid, path=path).status_code == 200


def test_超过64KB报422(client: TestClient) -> None:
    """上限是「文件预加载进 ContextBundle」得以成立的前提。"""
    sid = _create(client).json()["id"]

    r = _add_file(client, sid, content="x" * (FILE_MAX_BYTES + 1))

    assert r.status_code == 422
    assert "64 KB" in r.json()["detail"]


def test_大小按字节算而非字符(client: TestClient) -> None:
    """一个汉字 3 字节：22000 个汉字 = 66000 字节，超限；按字符算会放过它。"""
    sid = _create(client).json()["id"]

    r = _add_file(client, sid, content="中" * 22000)

    assert r.status_code == 422


def test_刚好等于上限可以存(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    assert _add_file(client, sid, content="x" * FILE_MAX_BYTES).status_code == 200


def test_同skill内路径重复报409(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    _add_file(client, sid)

    assert _add_file(client, sid).status_code == 409


def test_不同skill可以有同名文件(client: TestClient) -> None:
    a = _create(client, name="a", description="A").json()["id"]
    b = _create(client, name="b", description="B").json()["id"]

    assert _add_file(client, a, path="reference.md").status_code == 200
    assert _add_file(client, b, path="reference.md").status_code == 200


def test_更新文件(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid).json()["id"]

    r = client.put(f"/skills/{sid}/files/{fid}", json={"content": "改过的内容"})

    assert r.status_code == 200
    assert r.json()["content"] == "改过的内容"
    # 未传的字段不动
    assert r.json()["path"] == "checklist.md"


def test_改路径撞已有报409(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    _add_file(client, sid, path="a.md")
    fid = _add_file(client, sid, path="b.md").json()["id"]

    assert client.put(f"/skills/{sid}/files/{fid}", json={"path": "a.md"}).status_code == 409


def test_改路径成自己不报409(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid, path="a.md").json()["id"]

    assert client.put(f"/skills/{sid}/files/{fid}", json={"path": "a.md"}).status_code == 200


def test_更新时也校验大小(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid).json()["id"]

    r = client.put(f"/skills/{sid}/files/{fid}", json={"content": "x" * (FILE_MAX_BYTES + 1)})

    assert r.status_code == 422


def test_删文件(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid).json()["id"]

    assert client.delete(f"/skills/{sid}/files/{fid}").json() == {"ok": True}
    assert client.get("/skills").json()[0]["files"] == []


def test_文件操作要求skill存在(client: TestClient) -> None:
    assert _add_file(client, "skill_nope").status_code == 404
    assert client.get("/skills/skill_nope/files/skf_x").status_code == 404
    assert client.put("/skills/skill_nope/files/skf_x", json={}).status_code == 404
    assert client.delete("/skills/skill_nope/files/skf_x").status_code == 404


def test_操作不存在的文件报404(client: TestClient) -> None:
    sid = _create(client).json()["id"]

    assert client.get(f"/skills/{sid}/files/skf_nope").status_code == 404
    assert client.put(f"/skills/{sid}/files/skf_nope", json={}).status_code == 404
    assert client.delete(f"/skills/{sid}/files/skf_nope").status_code == 404


def test_不能跨skill操作文件(client: TestClient) -> None:
    """拿 B 的 skill_id 去动 A 的文件应该 404 —— 归属校验不能只靠 file_id。"""
    a = _create(client, name="a", description="A").json()["id"]
    b = _create(client, name="b", description="B").json()["id"]
    fid = _add_file(client, a).json()["id"]

    assert client.get(f"/skills/{b}/files/{fid}").status_code == 404
    assert client.delete(f"/skills/{b}/files/{fid}").status_code == 404


def test_删skill级联删文件(client: TestClient) -> None:
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid).json()["id"]

    client.delete(f"/skills/{sid}")
    sid2 = _create(client).json()["id"]

    # 新建的同名 skill 不该继承上一个的文件
    assert client.get(f"/skills/{sid2}/files/{fid}").status_code == 404


def test_文件变更写审计(client: TestClient, audit: FakeAuditRepo) -> None:
    """全局资源的变更记在 GLOBAL_SCOPE 下，不混进任意频道的流水。"""
    sid = _create(client).json()["id"]
    fid = _add_file(client, sid).json()["id"]
    client.put(f"/skills/{sid}/files/{fid}", json={"content": "改了"})
    client.delete(f"/skills/{sid}/files/{fid}")

    events = [x.detail["event"] for x in audit.logs]
    assert events == [
        "skill_create",
        "skill_file_create",
        "skill_file_update",
        "skill_file_delete",
    ]
    assert {x.channel_instance_id for x in audit.logs} == {"global"}
