"""Skill 领域模型。

skill 是一份「怎么做某类事」的指令文本，全局定义一份、按频道启用（见
``channel_skills`` 关联表）。与 :class:`~teamai.domain.models.tag.TagTemplate`
的分工是触发方式，不是内容：

- tag 由**人**触发（用户打 ``/名字``），一次只可能有一个，正文直接进系统提示词；
- skill 由**模型**触发（看清单判断相关性后调 ``load_skill``），可同时启用多个，
  正文只在被载入时才进上下文。

所以 skill 走**渐进式披露**：系统提示词里只常驻 ``name: description`` 一行，
正文按需取。这不是省 token 的小优化，而是启用数量能否增长的前提 —— 全量注入时
第 N 个 skill 的正文要被前 N-1 个不相关的任务一起付钱，启用到十几个就不可用了。

``description`` 因此是本模型里最要紧的字段：它是模型判断「这件事该不该用这个
skill」的唯一依据，且每次 run 都常驻。写成「审查 PR」不够（模型无法判断适用
边界），要写成「按团队 Go 规范审查 PR，产出分级问题清单」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# name 的字符约束：模型要在 ``load_skill(name)`` 里原样打出这个名字，
# 且它出现在系统提示词的清单里。限成小写字母数字与连字符，理由与
# McpServer.name 相同 —— 大小写混排与空格会让模型抄错，而抄错就是一次
# 无谓的工具往返。
NAME_PATTERN = r"^[a-z0-9-]+$"

# description 的长度上限。它每次 run 都常驻系统提示词，长度直接乘以调用次数，
# 因此在入库时就拦住 —— 写成整段正文的话，渐进式披露的意义就没了。
DESCRIPTION_MAX_LEN = 200

# 单个附带文件的字节上限。
#
# 这个上限是「文件预加载进 ContextBundle」这个设计得以成立的前提：每次 agent run
# 都会把本频道全部启用 skill 的文件读进内存（理由见 ContextBundle.skills 的注释），
# 没有上限时一个人塞进来的 10 MB 文档会让每次 run 都多读一遍它。
#
# 64 KB 对参考文档、配置样例、脚本源码都够用；真需要更大的东西，那是「让 agent
# 去拉取」而不是「捆在 skill 里」该解决的问题。
FILE_MAX_BYTES = 64 * 1024

# 文件路径允许的字符：字母数字、下划线、连字符、点、斜杠。
#
# 这不是文件系统路径 —— 文件内容存在库里，path 只是给模型看的标识符。但仍禁掉
# ``..``（见 is_safe_path）：日后若有人把这些文件落到磁盘上（导出、给沙箱挂载），
# 一个带 ``../`` 的 path 就成了目录穿越。在入库处拦住比事后审计便宜。
FILE_PATH_PATTERN = r"^[A-Za-z0-9_./-]+$"


def is_safe_path(path: str) -> bool:
    """path 是否可安全用作标识符与（潜在的）相对文件名。

    四条都必须拦：``..`` 段是目录穿越；绝对路径与尾随斜杠是形态错误；
    空串会让清单里出现一个没有名字的条目。
    """
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    return ".." not in path.split("/")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _human_size(nbytes: int) -> str:
    """字节数渲染成人类可读的短串，供文件清单展示。

    给模型看大小是为了让它在读之前对代价有数：清单里写着 42 KB 的文档，它就能
    判断值不值得为当前任务把它读进来。
    """
    if nbytes < 1024:
        return f"{nbytes} B"
    return f"{nbytes / 1024:.1f} KB"


@dataclass
class SkillFile:
    """skill 的一个附带文件。

    **一律是文本。** 模型读不了二进制，存进来只会占空间；内容在入库时做 UTF-8
    解码校验，解不开的直接拒掉。参考文档、配置样例、脚本源码都属于文本。

    **只读。** 脚本（.sh/.py）对模型也只是可读的文本 —— 它可以解释、改写、或建议
    用户去执行，但本项目不提供任何执行路径。真要执行需要沙箱、资源限额、超时与
    凭据隔离，那是独立决策。
    """

    id: str
    skill_id: str
    # 给模型看的路径标识，同一 skill 内唯一。不是文件系统路径（内容存库里）。
    path: str
    # 这个文件是干什么的。与 Skill.description 同理，是模型判断「要不要读它」的
    # 依据 —— 但它只在 load_skill 之后才进上下文，故不像前者那样需要严格限长。
    description: str
    content: str
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def size_bytes(self) -> int:
        """UTF-8 字节数。按字节而非字符：上限是按存储与传输算的，
        而一个汉字占 3 字节。"""
        return len(self.content.encode("utf-8"))

    @property
    def manifest_line(self) -> str:
        """文件清单里的一行。形状与 ``read_skill_file`` 的入参约定是同一件事 ——
        模型照着清单里的 path 去调工具，两处必须一致。"""
        return f"- {self.path}（{_human_size(self.size_bytes)}）：{self.description}"


@dataclass
class Skill:
    id: str
    name: str
    description: str
    content: str
    # 全局停用开关。停用后**所有**频道立刻失效，与逐个频道取消勾选不同 ——
    # 这是「这个 skill 写坏了，先下线」的入口，不必去翻哪些频道启用过它。
    enabled: bool = True
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    # 附带文件。仓储在读 skill 时一并装上（见 SkillRepository 的说明）。
    #
    # 内容也带着 —— 与 content 同理：工具执行时不能碰数据库，故必须预加载。
    # 单文件有 FILE_MAX_BYTES 上限兜住总量。
    files: list[SkillFile] = field(default_factory=list)

    @property
    def catalog_line(self) -> str:
        """清单里的一行（``name: description``），供系统提示词渲染。

        放在领域模型上而非提示词模板里：这一行的形状与 ``load_skill`` 的入参
        约定是同一件事 —— 模型照着清单里的 name 去调工具，两处必须一致。

        有意**不**在这里提文件：系统提示词那一级只该回答「这个技能管什么事」。
        文件的存在与否是载入之后才需要知道的，写进来等于把第 3 级的信息提到
        第 1 级，每次 run 都要付钱。
        """
        return f"- {self.name}: {self.description}"

    @property
    def file_manifest(self) -> str:
        """附带文件清单（每行一个：路径、大小、用途），供 ``load_skill`` 返回。

        只有元信息，没有内容 —— 内容要模型再调 ``read_skill_file`` 点名取。
        无文件时返回空串，调用方据此省掉整个「附带文件」段落。
        """
        if not self.files:
            return ""
        return "\n".join(f.manifest_line for f in self.files)
