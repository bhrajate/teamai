"""ULID 生成的格式、可排序性与长度约束。

gen_id 原本是 uuid4 截断（纯随机、不可排序），改为 ULID 后「字典序 == 生成
时间序」成了对外承诺，这里把它固化住。另外 id 列是 String(32)，最长前缀
恰好顶满，需要一条守卫防止新增前缀撑爆列宽。
"""

from __future__ import annotations

import re
import time

import pytest

from teamai.domain.identity import ULID_LENGTH, gen_id, new_ulid

# Crockford base32，与实现里的字母表一致（剔除 I、L、O、U）
ULID_RE = re.compile(rf"^[0-9A-HJKMNP-TV-Z]{{{ULID_LENGTH}}}$")

# ORM 里 id 列统一为 String(32)
ID_COLUMN_LENGTH = 32

# 代码库里实际用到的全部前缀，新增时同步更新
KNOWN_PREFIXES = ["task", "mem", "tag", "ch", "ai", "pol", "bq", "audit", "itr", "obx"]


def test_ulid_长度与字母表() -> None:
    assert ULID_LENGTH == 26
    for _ in range(200):
        assert ULID_RE.match(new_ulid())


def test_gen_id_形如_前缀下划线ulid() -> None:
    value = gen_id("task")
    prefix, _, ulid = value.partition("_")
    assert prefix == "task"
    assert ULID_RE.match(ulid)


def test_同一毫秒内不重复() -> None:
    """80 bit 随机保证同毫秒内的唯一性。"""
    batch = [new_ulid() for _ in range(5_000)]
    assert len(set(batch)) == len(batch)


def test_字典序等于生成时间序() -> None:
    """跨毫秒生成的 ULID，按字符串排序即按时间排序。"""
    samples: list[str] = []
    for _ in range(12):
        samples.append(new_ulid())
        time.sleep(0.002)  # 跨过毫秒边界
    assert samples == sorted(samples)


def test_时间戳段随时间单调不减() -> None:
    """只比较前 10 字符（时间戳段），排除随机段干扰。"""
    first = new_ulid()[:10]
    time.sleep(0.005)
    second = new_ulid()[:10]
    assert second > first


@pytest.mark.parametrize("prefix", KNOWN_PREFIXES)
def test_全部前缀不超出_id_列长度(prefix: str) -> None:
    value = gen_id(prefix)
    assert len(value) <= ID_COLUMN_LENGTH, (
        f"前缀 {prefix!r} 产出 {len(value)} 字符，超出 id 列的 "
        f"String({ID_COLUMN_LENGTH})。前缀最长 {ID_COLUMN_LENGTH - ULID_LENGTH - 1} 字符，"
        "再长需要先加宽 ORM 里的 id 列。"
    )


def test_代码库里的前缀已全部登记() -> None:
    """gen_id 的调用点若用了未登记的前缀，上面那条长度守卫就形同虚设。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "teamai"
    used = set()
    for path in src.rglob("*.py"):
        used.update(re.findall(r"""gen_id\(\s*["']([^"']+)["']""", path.read_text(encoding="utf-8")))
    unregistered = used - set(KNOWN_PREFIXES)
    assert not unregistered, f"这些前缀未登记到 KNOWN_PREFIXES: {sorted(unregistered)}"
