"""skill 的两个取回工具：load_skill（正文 + 文件清单）与 read_skill_file（文件内容）。

与其余工具的两点不同：

**按 run 构造，不进全局注册表。** 其他工具（github / monitoring / MCP）是启动时
注册一次的全局单例，而这两个闭包捕获「本频道启用了哪些 skill」—— 那是 per-run
的。ToolRegistry.for_channel 因此在每次裁剪工具集时新建它们。这是安全的：
PydanticAIGateway 每次 run 都新建 Agent（见其模块文档），工具对象不跨 run 复用。

**闭包里带的是完整数据，不是仓储。** 工具执行发生在 agent run 进行中，此时去查
库会复用组合根那个共享 AsyncSession，而它不允许并发使用 —— 这类故障的完整描述
见 container.open_job_scope 的文档。正文与文件在组装 ContextBundle 时就已取回，
这里只是内存查表。

skill 的载入没有留痕：审计要写库，会撞上同一个 session 问题。想知道某次 run
载入了什么，看交互记录里的响应正文（工具往返会体现在那里）。

## 三级渐进式披露

捆绑文件让披露从两级变三级，每级只付上一级点名了的代价：

1. 系统提示词：``name: description``（每个 skill 约 30~50 token，常驻）
2. ``load_skill``：正文 + **文件清单**（路径、用途、大小），不含文件内容
3. ``read_skill_file``：某个文件的内容

第 2 级只给清单是关键。把文件内容一并内联的话，一个带 3 个参考文档的 skill
每次载入都要付全部文档的代价，而典型任务只用得上其中一个 —— 那等于把第 1 级
好不容易省下的东西在第 2 级又花掉了。

## 只读

文件对模型是**可读文本**，本模块不提供任何执行路径。脚本（.sh/.py）也一样：
模型读到源码后可以解释它、改写它、或建议用户去跑，但不会有进程被起来。真要执行
需要沙箱、资源限额、超时与凭据隔离，那是独立决策，不在本模块范围内。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import ModelRetry, Tool

from teamai.domain.models.skill import Skill

# 工具描述。技能清单本身不写在这里 —— 它在系统提示词里
# （application/agent/prompts.py），那是「模型被告知了什么」的唯一装配处。
_LOAD_DESCRIPTION = (
    "载入一个技能的完整操作说明。系统提示词的「可用技能」清单里列出了可选的名字"
    "与各自的适用场景；判断某个技能与当前任务相关时，先用本工具取回它的完整说明，"
    "再按说明执行。可以载入多个技能。若该技能带有附带文件，返回内容里会列出文件"
    "清单（只有路径与用途，没有内容）—— 需要某个文件时再用 read_skill_file 取。"
)

_READ_DESCRIPTION = (
    "读取某个技能的附带文件内容。文件清单由 load_skill 返回 —— 需要先载入技能，"
    "才知道有哪些文件可读。文件一律是文本（参考文档、配置样例、脚本源码等）；"
    "脚本对你而言也只是可读的文本，本工具不执行任何东西。"
)


def build_skill_tool(skills: Sequence[Skill]) -> Tool:
    """构造本次 run 可用的 load_skill 工具。

    调用方须保证 ``skills`` 非空 —— 空清单时挂一个「什么都载不到」的工具，
    只会诱导模型做无谓的往返。
    """
    by_name = {s.name: s for s in skills}

    async def load_skill(name: str) -> str:
        """载入指定技能的完整操作说明。

        Args:
            name: 技能名，取自系统提示词「可用技能」清单里的名字。
        """
        skill = by_name.get(name)
        if skill is None:
            # 名字错了模型能自己改（清单就在它的上下文里），故用 ModelRetry
            # 把有效取值回灌给它，而不是 fail() 那种「到此为止」的语义。
            raise ModelRetry(f"没有名为 {name} 的技能。本频道可用的技能：{_names(by_name)}")
        # 正文原样返回，不套 ok() 的 JSON 包装 —— 这是本项目工具返回值的一处
        # 有意例外。其余工具返回的是**数据**（issue 列表、指标值），JSON 便于
        # 模型解析；而这里返回的是**要照着做的散文**，JSON 编码会把几千字
        # Markdown 里的每个换行变成 \n 字面量，既多花 token 又更难读。
        out = f"# 技能：{skill.name}\n\n{skill.content}"
        # 文件清单跟着正文一起给，但只有路径与用途 —— 内容要模型再点名。
        # 拼在正文之后而非之前：正文是「怎么做」，清单是「还有哪些料可取」，
        # 后者是补充。
        if manifest := skill.file_manifest:
            out += f"\n\n## 附带文件\n\n{manifest}\n\n（用 read_skill_file 读取需要的文件。）"
        return out

    return Tool(load_skill, description=_LOAD_DESCRIPTION)


def build_skill_file_tool(skills: Sequence[Skill]) -> Tool:
    """构造本次 run 可用的 read_skill_file 工具。

    调用方须保证 ``skills`` 里**至少有一个带文件**（见 ToolRegistry.for_channel）：
    没有任何文件时挂上它只是多一个永远返回错误的工具。
    """
    # 双层索引：(技能名, 路径) → 文件。用技能名限定作用域，而不是只按路径查 ——
    # 两个技能各带一个 reference.md 是完全正常的，只按路径索引会撞车，而撞车的
    # 表现是模型读到另一个技能的文件（跨技能串味，且很难看出来）。
    by_skill = {s.name: {f.path: f for f in s.files} for s in skills if s.files}

    async def read_skill_file(skill: str, path: str) -> str:
        """读取某个技能的附带文件内容。

        Args:
            skill: 技能名，与 load_skill 用的名字相同。
            path: 文件路径，取自 load_skill 返回的附带文件清单。
        """
        files = by_skill.get(skill)
        if files is None:
            # 分两种错因给不同提示：技能名不对 vs 技能没带文件。合成一句
            # 「找不到」会让模型不知道该改哪个参数。
            known = _names(by_skill)
            raise ModelRetry(
                f"技能 {skill} 不存在或没有附带文件。带文件的技能：{known}"
            )
        file = files.get(path)
        if file is None:
            available = "、".join(sorted(files)) or "（无）"
            raise ModelRetry(f"技能 {skill} 没有文件 {path}。它的文件：{available}")
        # 同 load_skill：原样返回文本，不做 JSON 包装。
        # 带上头部两行，让模型在一次 run 读了多个文件后仍能分清哪段是哪个。
        return f"# {skill}/{file.path}\n\n{file.content}"

    return Tool(read_skill_file, description=_READ_DESCRIPTION)


def _names(mapping: dict) -> str:
    """把可选名字拼成一句给模型看的提示。空时给出明确的「无」。"""
    return "、".join(sorted(mapping)) or "（无）"
