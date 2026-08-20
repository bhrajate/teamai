"""SQLSkillRepository 的真 SQL 行为：CRUD、唯一约束、关联表与级联。

跑在内存 SQLite 上，与 test_mcp_repository.py 同思路：唯一约束与 join 语义
与方言无关。

重点覆盖 skill 与 MCP 不同的那部分 —— 多对多关联表：
- list_for_channel 过滤全局 enabled，list_channel_skill_ids 不过滤（两者答的
  不是同一个问题，混用会让管理页的勾选凭空消失）
- 删 skill 要级联清关联行（表间没有外键，级联是仓储显式做的）
- 覆盖式写入的去重与幂等
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models.skill import Skill, SkillFile
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.skill import ChannelSkillModel, SkillFileModel
from teamai.infrastructure.repositories.skill import SQLSkillRepository


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SQLSkillRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield SQLSkillRepository(s)
    await engine.dispose()


def _session(repo: SQLSkillRepository) -> AsyncSession:
    return repo._session  # noqa: SLF001  测试要直接查表核对落库结果


def _skill(name: str = "code-review", **kw) -> Skill:
    return Skill(
        id=kw.pop("id", f"skill_{name}"),
        name=name,
        description=kw.pop("description", f"{name} 的适用场景"),
        content=kw.pop("content", f"# {name}\n\n步骤一、步骤二。"),
        **kw,
    )


async def test_落库后能按_id_与_name_取回(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())

    by_id = await repo.get("skill_code-review")
    assert by_id is not None
    assert by_id.name == "code-review"
    assert "步骤一" in by_id.content

    by_name = await repo.find_by_name("code-review")
    assert by_name is not None
    assert by_name.id == "skill_code-review"


async def test_name_全局唯一(repo: SQLSkillRepository) -> None:
    """重名会让「load_skill 载入哪一个」取决于查询顺序，故在库层拦下。"""
    await repo.upsert(_skill(id="skill_a"))
    with pytest.raises(IntegrityError):
        await repo.upsert(_skill(id="skill_b"))


async def test_list_all_按name排序(repo: SQLSkillRepository) -> None:
    for name in ("weekly-report", "code-review", "incident"):
        await repo.upsert(_skill(name))

    assert [s.name for s in await repo.list_all()] == [
        "code-review",
        "incident",
        "weekly-report",
    ]


async def test_upsert_是覆盖写(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())
    updated = _skill(content="# 改过的正文", description="新描述")
    await repo.upsert(updated)

    got = await repo.get("skill_code-review")
    assert got is not None
    assert got.content == "# 改过的正文"
    assert got.description == "新描述"
    assert len(await repo.list_all()) == 1


# ---- 频道关联 ----


async def test_设置频道技能后能按频道取回(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill("code-review"))
    await repo.upsert(_skill("weekly-report"))

    await repo.set_channel_skills("ch_1", ["skill_code-review"])

    got = await repo.list_for_channel("ch_1")
    assert [s.name for s in got] == ["code-review"]
    # 正文要带回来 —— load_skill 靠它做内存查表
    assert "步骤一" in got[0].content


async def test_频道之间互不影响(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill("code-review"))
    await repo.upsert(_skill("weekly-report"))

    await repo.set_channel_skills("ch_1", ["skill_code-review"])
    await repo.set_channel_skills("ch_2", ["skill_weekly-report"])

    assert [s.name for s in await repo.list_for_channel("ch_1")] == ["code-review"]
    assert [s.name for s in await repo.list_for_channel("ch_2")] == ["weekly-report"]


async def test_全局停用后该频道取不到但勾选仍在(repo: SQLSkillRepository) -> None:
    """两个方法有意不同：一个给 agent 用（过滤 enabled），一个给管理页回显（不过滤）。

    若 list_channel_skill_ids 也过滤，管理员全局停用一个 skill 后再打开频道页，
    会看到勾选被凭空取消，以为关联关系丢了 —— 而库里那行还在。
    """
    await repo.upsert(_skill("code-review", enabled=False))
    await repo.set_channel_skills("ch_1", ["skill_code-review"])

    assert await repo.list_for_channel("ch_1") == []
    assert await repo.list_channel_skill_ids("ch_1") == ["skill_code-review"]


async def test_覆盖式写入替换整组(repo: SQLSkillRepository) -> None:
    for name in ("a", "b", "c"):
        await repo.upsert(_skill(name))

    await repo.set_channel_skills("ch_1", ["skill_a", "skill_b"])
    await repo.set_channel_skills("ch_1", ["skill_c"])

    assert [s.name for s in await repo.list_for_channel("ch_1")] == ["c"]


async def test_覆盖式写入去重(repo: SQLSkillRepository) -> None:
    """入参里重复的 id 不该撞唯一约束 —— 勾选框本就表达集合语义。"""
    await repo.upsert(_skill("code-review"))

    await repo.set_channel_skills("ch_1", ["skill_code-review", "skill_code-review"])

    assert len(await repo.list_for_channel("ch_1")) == 1


async def test_清空频道技能(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())
    await repo.set_channel_skills("ch_1", ["skill_code-review"])

    await repo.set_channel_skills("ch_1", [])

    assert await repo.list_for_channel("ch_1") == []
    assert await repo.list_channel_skill_ids("ch_1") == []


async def test_关联表不允许重复行(repo: SQLSkillRepository) -> None:
    """uq_channel_skills_pair 存在，否则重复行会让清单里同一 skill 出现两遍。"""
    await repo.upsert(_skill())
    now = (await repo.get("skill_code-review")).created_at  # type: ignore[union-attr]
    s = _session(repo)
    s.add(ChannelSkillModel(channel_instance_id="ch_1", skill_id="skill_x", created_at=now))
    await s.flush()
    s.add(ChannelSkillModel(channel_instance_id="ch_1", skill_id="skill_x", created_at=now))
    with pytest.raises(IntegrityError):
        await s.flush()


async def test_删skill级联清关联行(repo: SQLSkillRepository) -> None:
    """表间没有外键，级联是 delete() 显式做的。

    漏了的话关联行会成孤儿 —— 表现不是报错，而是 list_for_channel 的 join
    静默少一条，而管理页仍显示勾选。
    """
    await repo.upsert(_skill())
    await repo.set_channel_skills("ch_1", ["skill_code-review"])
    await repo.set_channel_skills("ch_2", ["skill_code-review"])

    await repo.delete("skill_code-review")

    assert await repo.get("skill_code-review") is None
    rows = (await _session(repo).execute(select(ChannelSkillModel))).scalars().all()
    assert rows == []


async def test_删一个skill不影响其他的关联(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill("a"))
    await repo.upsert(_skill("b"))
    await repo.set_channel_skills("ch_1", ["skill_a", "skill_b"])

    await repo.delete("skill_a")

    assert [s.name for s in await repo.list_for_channel("ch_1")] == ["b"]


# ---- 附带文件 ----


def _file(skill_id: str = "skill_code-review", path: str = "checklist.md", **kw) -> SkillFile:
    return SkillFile(
        id=kw.pop("id", f"skf_{path}"),
        skill_id=skill_id,
        path=path,
        description=kw.pop("description", f"{path} 的用途"),
        content=kw.pop("content", "# 清单\n\n- 一\n- 二"),
    )


async def test_文件随skill一起读出(repo: SQLSkillRepository) -> None:
    """读 skill 的方法一律带文件 —— agent 侧必须带（工具执行时不能碰库）。"""
    await repo.upsert(_skill())
    await repo.upsert_file(_file())

    for got in (
        await repo.get("skill_code-review"),
        await repo.find_by_name("code-review"),
        (await repo.list_all())[0],
    ):
        assert got is not None
        assert [f.path for f in got.files] == ["checklist.md"]
        # 内容也要带：read_skill_file 靠它做内存查表
        assert "- 一" in got.files[0].content


async def test_频道视图也带文件(repo: SQLSkillRepository) -> None:
    """这条路径最要紧：它就是 agent 每次 run 走的那条。"""
    await repo.upsert(_skill())
    await repo.upsert_file(_file())
    await repo.set_channel_skills("ch_1", ["skill_code-review"])

    (got,) = await repo.list_for_channel("ch_1")

    assert [f.path for f in got.files] == ["checklist.md"]


async def test_无文件的skill_files为空列表(repo: SQLSkillRepository) -> None:
    """不是 None —— 调用方直接迭代，None 会 TypeError。"""
    await repo.upsert(_skill())
    got = await repo.get("skill_code-review")
    assert got is not None
    assert got.files == []


async def test_文件按path排序(repo: SQLSkillRepository) -> None:
    """清单顺序要稳定，否则同一个 skill 每次载入给模型的清单顺序都不同。"""
    await repo.upsert(_skill())
    for path in ("zz.md", "aa.md", "mm.md"):
        await repo.upsert_file(_file(path=path))

    got = await repo.get("skill_code-review")
    assert got is not None
    assert [f.path for f in got.files] == ["aa.md", "mm.md", "zz.md"]


async def test_同skill内path唯一(repo: SQLSkillRepository) -> None:
    """模型照 path 调 read_skill_file，重复会让「读到哪一个」取决于查询顺序。"""
    await repo.upsert(_skill())
    await repo.upsert_file(_file(id="skf_a"))
    with pytest.raises(IntegrityError):
        await repo.upsert_file(_file(id="skf_b"))


async def test_不同skill可以有同名文件(repo: SQLSkillRepository) -> None:
    """两个技能各带一个 reference.md 是完全正常的 —— 唯一性只在 skill 内。"""
    await repo.upsert(_skill("a"))
    await repo.upsert(_skill("b"))
    await repo.upsert_file(_file("skill_a", "reference.md", id="skf_a", content="AAA"))
    await repo.upsert_file(_file("skill_b", "reference.md", id="skf_b", content="BBB"))

    a = await repo.get("skill_a")
    b = await repo.get("skill_b")
    assert a is not None and b is not None
    assert a.files[0].content == "AAA"
    assert b.files[0].content == "BBB"


async def test_按id与path取文件(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())
    await repo.upsert_file(_file())

    by_id = await repo.get_file("skill_code-review", "skf_checklist.md")
    assert by_id is not None and by_id.path == "checklist.md"

    by_path = await repo.find_file_by_path("skill_code-review", "checklist.md")
    assert by_path is not None and by_path.id == "skf_checklist.md"


async def test_取文件要求skill归属正确(repo: SQLSkillRepository) -> None:
    """拿别的 skill 的 id 来取应该取不到 —— 否则路由上的归属校验形同虚设。"""
    await repo.upsert(_skill("a"))
    await repo.upsert_file(_file("skill_a", id="skf_x"))

    assert await repo.get_file("skill_b", "skf_x") is None


async def test_覆盖写文件(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())
    await repo.upsert_file(_file(content="旧内容"))
    await repo.upsert_file(_file(content="新内容"))

    got = await repo.get_file("skill_code-review", "skf_checklist.md")
    assert got is not None
    assert got.content == "新内容"


async def test_删文件(repo: SQLSkillRepository) -> None:
    await repo.upsert(_skill())
    await repo.upsert_file(_file())

    await repo.delete_file("skill_code-review", "skf_checklist.md")

    assert await repo.get_file("skill_code-review", "skf_checklist.md") is None
    got = await repo.get("skill_code-review")
    assert got is not None and got.files == []


async def test_删skill级联清文件(repo: SQLSkillRepository) -> None:
    """没有外键，级联是 delete() 显式做的。漏了的话文件永久占库且无从发现。"""
    await repo.upsert(_skill())
    await repo.upsert_file(_file(path="a.md", id="skf_a"))
    await repo.upsert_file(_file(path="b.md", id="skf_b"))

    await repo.delete("skill_code-review")

    rows = (await _session(repo).execute(select(SkillFileModel))).scalars().all()
    assert rows == []


async def test_读多个skill的文件不串味(repo: SQLSkillRepository) -> None:
    """_with_files 用一条 IN 查询批量取，分组错了会把 A 的文件挂到 B 上。"""
    await repo.upsert(_skill("a"))
    await repo.upsert(_skill("b"))
    await repo.upsert(_skill("c"))
    await repo.upsert_file(_file("skill_a", "a1.md", id="skf_a1"))
    await repo.upsert_file(_file("skill_a", "a2.md", id="skf_a2"))
    await repo.upsert_file(_file("skill_c", "c1.md", id="skf_c1"))

    got = {s.name: [f.path for f in s.files] for s in await repo.list_all()}

    assert got == {"a": ["a1.md", "a2.md"], "b": [], "c": ["c1.md"]}
