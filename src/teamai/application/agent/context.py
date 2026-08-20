"""ContextBundle：Agent 单次 run 的上下文组装与压缩。"""

from __future__ import annotations

from dataclasses import dataclass, field

from teamai.domain.models import ChannelInstance, MemoryEntry, PermissionPolicy, Skill, TagTemplate
from teamai.domain.ports import ThreadMessage


@dataclass
class ContextBundle:
    task_id: str
    channel_instance_id: str
    user_prompt: str
    system_prompt: str
    model_level: str
    instance: ChannelInstance
    policy: PermissionPolicy | None
    allowed_tools: list[str] = field(default_factory=list)
    memory_hits: list[MemoryEntry] = field(default_factory=list)
    # 线程历史。存结构化的 ThreadMessage 而非裸字符串：渲染成提示词时要标出
    # 发言人、并区分机器人自己的上一轮输出 —— 两者混作一堆无署名的文本时，
    # 模型容易把自己说过的话当成用户诉求。
    thread_history: list[ThreadMessage] = field(default_factory=list)
    # 压缩掉的条数。>0 时提示词里会插一行说明，让模型知道自己看的是截断视图，
    # 而不是把「历史只有这么多」当成事实。
    dropped_history: int = 0
    tag: TagTemplate | None = None
    # 本频道已启用且全局未停用的 skill，含正文。
    #
    # 带正文而非只带 id/描述：正文要交给 load_skill 工具做内存查表。工具执行在
    # agent run 进行中，那时再查库会复用共享 AsyncSession（不允许并发使用）——
    # 详见 domain/ports/tools.py 的 for_channel 文档。
    #
    # 代价是每次 run 都把全部启用 skill 的正文读进内存，即便一个都没被载入。
    # 这是 DB 读而非 token 支出，且 skill 数量是「管理员手工配的」量级，
    # 相比让工具自己查库要承担的会话风险，这笔代价划算。
    skills: list[Skill] = field(default_factory=list)

    @property
    def memory_context(self) -> str:
        """将记忆命中拼为上下文文本，每条带写入日期。

        日期不是装饰，它是矛盾记忆的唯一裁决依据。写入侧的去重与取代只在蒸馏
        候选（语义 top-10，`CANDIDATE_TOP_K`）范围内生效，而三条路会绕过它：
        旧记忆没排进候选窗口、向量不可用时回落成「只看最近 10 条」、人工经
        Admin API 写入完全不过冲突检查。于是库里可能有两条并列现行而互相矛盾的
        记忆（三月定「超时 3 秒」、六月改成 5 秒），语义相似度几乎相同、会一起
        进 top_k。渲染成裸文本时模型没有任何依据分辨哪条是现行的 —— 这正是
        docs/Design-conversation-context.md §3.3.1 描述的缺陷，`superseded_by`
        只挡住了模型当场发现的那部分。裁决规则在系统提示词的行为规范里。

        只给日期不给时刻：矛盾记忆的间隔通常在天到月，精确到秒纯属白耗 token。

        ⚠️ 顺序不代表新旧。语义段按相似度排、回落段按时间倒序排，两者渲染出来
        长得一样，所以不能靠位置暗示时序，日期必须显式写出来。
        """
        if not self.memory_hits:
            return ""
        return "\n".join(
            f"- [{entry.created_at:%Y-%m-%d}] {entry.content}" for entry in self.memory_hits
        )

    @property
    def history_context(self) -> str:
        """将线程历史拼为上下文文本，必要时带截断说明。"""
        if not self.thread_history:
            return ""
        lines = [m.render() for m in self.thread_history]
        if self.dropped_history > 0:
            lines.insert(0, f"[更早的 {self.dropped_history} 条已省略，如需细节请明确询问]")
        return "\n".join(lines)

    @property
    def skill_catalog(self) -> str:
        """技能清单（每行 ``- name: description``），供系统提示词渲染。

        只有名字与描述，没有正文 —— 正文由模型判断相关后调 ``load_skill`` 取回。
        """
        if not self.skills:
            return ""
        return "\n".join(s.catalog_line for s in self.skills)

    @property
    def skill_ref_names(self) -> list[str]:
        """本次 run 挂上的技能名，供交互记录留痕。

        留的是「当时挂了哪些可选项」，不是「模型实际载入了哪几个」—— 后者发生在
        run 内部的工具往返里，用例层看不到。排查「为什么没用某个技能」时，先要能
        区分「它没挂上」与「挂上了但模型判断不相关」，这个字段答的是前者。
        """
        return [s.name for s in self.skills]

    @property
    def memory_ref_ids(self) -> list[str]:
        """本次引用到的记忆条目 id，供交互记录留痕。

        留 id 而非内容：记忆被删除后审计链仍指向它，但库里不会留第二份副本。
        """
        return [e.id for e in self.memory_hits]

    def compact(self, max_history: int, summary_threshold: int) -> ContextBundle:
        """上下文压缩：线程历史超阈值时丢弃最旧部分，只保留最近 max_history 条。

        返回新 bundle，不修改自身（保持可追溯）。被丢弃的条数记进
        `dropped_history`，由 `history_context` 渲染成一行说明 —— 此前的做法是
        把说明文案直接混进历史列表，于是那行字与真实消息在类型上不可区分，
        既没法统计丢了多少，也会被后续处理当成一条真消息。

        summary_threshold 目前不参与判定，保留形参是为了兼容调用方与配置项：
        真正的「摘要化」需要额外一次 LLM 调用，那是独立决策（见
        docs/Design-conversation-context.md §3.1），未做之前多留一个参数比
        改动全部调用点更划算。
        """
        if len(self.thread_history) <= max_history:
            return self
        dropped = len(self.thread_history) - max_history
        return ContextBundle(
            task_id=self.task_id,
            channel_instance_id=self.channel_instance_id,
            user_prompt=self.user_prompt,
            system_prompt=self.system_prompt,
            model_level=self.model_level,
            instance=self.instance,
            policy=self.policy,
            allowed_tools=list(self.allowed_tools),
            memory_hits=list(self.memory_hits),
            thread_history=self.thread_history[-max_history:],
            dropped_history=self.dropped_history + dropped,
            tag=self.tag,
            # ⚠️ 漏掉这行的表现极隐蔽：压缩只在历史超阈值时发生，于是「聊得久的
            # 线程里技能突然全部消失」，而短线程一切正常。
            skills=list(self.skills),
        )
