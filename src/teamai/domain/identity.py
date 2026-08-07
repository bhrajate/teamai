"""实体标识生成（ULID）。

放在 domain 顶层而非某个子包：ID 是各层共用的词汇（application 与
adapters 铸新实体时都要用），而 domain 是最底层，谁都已经依赖它，
无需为它开「任何层可导入」的特例。

用标准库自实现而非引三方 ULID 库：domain 层不得导入三方库（由
tests/unit/test_layering.py::test_domain_不导入三方库 把关），而 ULID
规范稳定、编码逻辑二十来行，自实现比为此破例更划算。
"""

from __future__ import annotations

import secrets
import time

# Crockford base32：剔除 I、L、O、U 以免与 1、0 混读。
# 顺序即字典序，故编码结果的字典序等于数值序 —— ULID 可排序性的来源。
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TIMESTAMP_CHARS = 10  # 48 bit 毫秒时间戳
_RANDOM_CHARS = 16  # 80 bit 随机
ULID_LENGTH = _TIMESTAMP_CHARS + _RANDOM_CHARS  # 26


def _encode(value: int, length: int) -> str:
    """把整数编码为定长 Crockford base32 字符串（高位在前）。"""
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid() -> str:
    """生成 26 字符 ULID：48 bit 毫秒时间戳 + 80 bit 随机。

    字典序 == 生成时间序（毫秒精度）。同一毫秒内生成的多个 ULID 之间不保证
    先后 —— 规范里的单调递增选项需要模块级可变状态与加锁，而本项目没有按
    ID 游标分页的地方（审计按 ts 排、向量按分数排），排序只需到毫秒即可。
    """
    timestamp_ms = time.time_ns() // 1_000_000
    randomness = int.from_bytes(secrets.token_bytes(10), "big")
    return _encode(timestamp_ms, _TIMESTAMP_CHARS) + _encode(randomness, _RANDOM_CHARS)


def gen_id(prefix: str = "id") -> str:
    """生成 `<prefix>_<ULID>` 形式的实体 ID，按生成时间可排序。

    ⚠️ 库里可能残留本函数改用 ULID 之前生成的旧 ID（`<prefix>_<20 位小写
    十六进制>`，uuid4 截断、纯随机）。两种格式共存时排序只在新 ID 之间成立，
    需要严格时间序请用实体自己的 created_at / ts 字段。

    ⚠️ 长度：最长前缀 `audit` 产出 32 字符，正好顶满 ORM 里 id 列的
    String(32)。新增前缀若超过 5 字符会撑爆该列，由
    tests/unit/test_identity.py::test_全部前缀不超出_id_列长度 拦住。
    """
    return f"{prefix}_{new_ulid()}"
