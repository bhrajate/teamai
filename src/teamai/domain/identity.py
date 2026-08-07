"""实体标识生成。

放在 domain 顶层而非某个子包：ID 是各层共用的词汇（application 与
adapters 铸新实体时都要用），而 domain 是最底层，谁都已经依赖它，
无需为它开「任何层可导入」的特例。
"""

from __future__ import annotations

import uuid


def gen_id(prefix: str = "id") -> str:
    """生成带前缀的随机 ID。

    注意：uuid4 截断，纯随机，**不可按时间排序**。需要按创建顺序排列时
    请用实体自己的 created_at / ts 字段。
    """
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
