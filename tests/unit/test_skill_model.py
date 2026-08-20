"""Skill / SkillFile 领域模型：清单渲染、大小计算、路径安全。

这些是纯函数，但都直接决定模型看到什么或能不能存进来，故单独锁住。
"""

from __future__ import annotations

import pytest

from teamai.domain.models.skill import (
    DESCRIPTION_MAX_LEN,
    FILE_MAX_BYTES,
    Skill,
    SkillFile,
    is_safe_path,
)


def _skill(**kw) -> Skill:
    return Skill(
        id=kw.pop("id", "skill_1"),
        name=kw.pop("name", "code-review"),
        description=kw.pop("description", "按团队规范审查 PR"),
        content=kw.pop("content", "# 步骤"),
        **kw,
    )


def _file(**kw) -> SkillFile:
    return SkillFile(
        id=kw.pop("id", "skf_1"),
        skill_id=kw.pop("skill_id", "skill_1"),
        path=kw.pop("path", "checklist.md"),
        description=kw.pop("description", "审查清单"),
        content=kw.pop("content", "- 一"),
    )


# ---- 清单行 ----


def test_catalog_line形状() -> None:
    """形状与 load_skill 的入参约定是同一件事：模型照清单里的 name 调工具。"""
    assert _skill().catalog_line == "- code-review: 按团队规范审查 PR"


def test_catalog_line不提文件() -> None:
    """文件的存在与否是载入之后才需要知道的。

    写进 catalog_line 等于把第 3 级的信息提到第 1 级 —— 每次 run 都要付钱。
    """
    line = _skill(files=[_file()]).catalog_line
    assert "checklist.md" not in line
    assert line == _skill().catalog_line


def test_manifest_line带路径大小与用途() -> None:
    # 默认内容 "- 一" 是 2 个 ASCII + 一个 3 字节汉字 = 5 字节
    assert _file().manifest_line == "- checklist.md（5 B）：审查清单"


def test_file_manifest每文件一行() -> None:
    s = _skill(files=[_file(path="a.md"), _file(path="b.md")])
    assert s.file_manifest.splitlines() == [
        "- a.md（5 B）：审查清单",
        "- b.md（5 B）：审查清单",
    ]


def test_无文件时file_manifest为空串() -> None:
    """调用方据此整段省掉「附带文件」段落。"""
    assert _skill().file_manifest == ""


# ---- 大小 ----


def test_size_bytes按utf8字节算() -> None:
    """按字节而非字符：上限是按存储算的，而一个汉字占 3 字节。

    按字符算会让 64 KB 的上限在中文文档上实际放过 192 KB。
    """
    assert _file(content="abc").size_bytes == 3
    assert _file(content="中文").size_bytes == 6


@pytest.mark.parametrize(
    ("nbytes", "expect"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (2048, "2.0 KB"), (1536, "1.5 KB")],
)
def test_大小渲染(nbytes: int, expect: str) -> None:
    assert _file(content="x" * nbytes).manifest_line.split("（")[1].split("）")[0] == expect


def test_文件上限是64KB() -> None:
    """这个上限是「文件预加载进 ContextBundle」得以成立的前提，不是随意取的。"""
    assert FILE_MAX_BYTES == 64 * 1024


def test_描述上限() -> None:
    """description 每次 run 都常驻系统提示词，故必须限长。"""
    assert DESCRIPTION_MAX_LEN == 200


# ---- 路径安全 ----


@pytest.mark.parametrize(
    "path",
    ["a.md", "docs/a.md", "a/b/c.md", "a-b_c.md", "a..b.md", "..a.md", "a.b..c"],
)
def test_合法路径(path: str) -> None:
    """``..`` 只在作为完整一段时才危险；``a..b.md`` 这种文件名是正常的。"""
    assert is_safe_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",  # 空：清单里会出现一个没名字的条目
        "/etc/passwd",  # 绝对路径
        "docs/",  # 尾随斜杠
        "../secret",  # 目录穿越
        "a/../../etc/passwd",
        "..",
        "a/..",
    ],
)
def test_非法路径(path: str) -> None:
    """path 目前只是库里的标识符，但仍要拦 —— 日后若把文件落到磁盘
    （导出、给沙箱挂载），带 ``../`` 的 path 就成了目录穿越。"""
    assert not is_safe_path(path)
