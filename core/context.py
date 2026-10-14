"""
上下文管理：决定每次调用模型时，把多少历史放进去。

完整的记忆照样保存（数据库里一字不少），这里只是在"组装发给模型的消息"那一刻
生成一个压缩过的视图：

- 当前这一轮：原样保留；只有这一轮本身就很长时，才截短较早几步的工具输出
- 最近两轮：保留全部步骤，但工具输出截短（完整内容还能用 @r_xxx 再取）
- 更早的轮次：只保留用户的问题和最终回答
- 仍然超出预算：从最早的轮次开始整轮省略，并告诉模型省略了多少轮

例外：skill 正文（load_skill 的结果）保留最近加载的那一份；计划清单（update_plan）只保留最新的一份。
"""
import copy

from smolagents.memory import ActionStep, MemoryStep, TaskStep
from smolagents.models import ChatMessage, MessageRole

OBSERVATION_LIMIT = 800       # 截短后的工具输出长度
FULL_RECENT_RUNS = 2          # 保留全部步骤的最近轮数（不含当前轮）
RECENT_STEPS_IN_RUN = 3       # 当前轮过长时，原样保留的最近步数
CURRENT_RUN_SHARE = 0.7       # 当前轮超过预算的这个比例时，才压缩当前轮


class NoteStep(MemoryStep):
    """一段给模型看的说明（如"更早的 3 轮对话已省略"），不来自真实的对话。"""

    def __init__(self, text: str):
        self.text = text

    def to_messages(self, summary_mode: bool = False) -> list[ChatMessage]:
        return [ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": self.text}])]


def _tool(step) -> tuple[str | None, dict]:
    calls = getattr(step, "tool_calls", None) or []
    if not calls:
        return None, {}
    args = calls[0].arguments
    return calls[0].name, args if isinstance(args, dict) else {}


def _size(step) -> int:
    total = 0
    for m in step.to_messages():
        content = m.content
        if isinstance(content, str):
            total += len(content)
        else:
            total += sum(len(c.get("text", "")) for c in content or [] if isinstance(c, dict))
    return total


def _truncated(step: ActionStep, limit: int = OBSERVATION_LIMIT) -> ActionStep:
    obs = step.observations
    if not obs or len(obs) <= limit:
        return step
    short = copy.copy(step)
    short.observations = (
        obs[:limit] + f"\n…（这段工具输出较早，已省略后面 {len(obs) - limit} 个字符；"
        "上面若有 Result ID，需要时可以用 @r_xxx 再次引用完整结果）"
    )
    return short


def _segments(steps: list) -> list[list]:
    """按用户的每条消息（TaskStep）把记忆切成一轮一轮。"""
    segs: list[list] = []
    for step in steps:
        if isinstance(step, TaskStep) or not segs:
            segs.append([])
        segs[-1].append(step)
    return segs


def _final_steps(seg: list) -> list:
    """一轮里只留下问题和最终回答（没有最终回答时留最后一步）。"""
    task = [s for s in seg if isinstance(s, TaskStep)]
    actions = [s for s in seg if isinstance(s, ActionStep)]
    finals = [s for s in actions if s.is_final_answer or s.action_output is not None] or actions[-1:]
    kept = task + [_truncated(s) for s in finals]
    dropped = len(actions) - len(finals)
    if dropped > 0:
        kept.insert(len(task), NoteStep(f"（这一轮的 {dropped} 个中间步骤已省略）"))
    return kept


def compact(steps: list, budget_chars: int) -> list:
    """返回压缩后的步骤列表（不修改传入的步骤）。"""
    segs = _segments(steps)
    if not segs:
        return []

    # 每个 skill 只保留最近一次加载的正文；计划只保留最新一份
    latest_skill_load: dict[str, int] = {}
    latest_plan = None
    for i, step in enumerate(steps):
        name, args = _tool(step)
        if name == "load_skill":
            latest_skill_load[str(args.get("skill_name"))] = i
        elif name == "update_plan":
            latest_plan = i
    keep_whole = set(latest_skill_load.values()) | ({latest_plan} if latest_plan is not None else set())
    position = {id(s): i for i, s in enumerate(steps)}

    def shrink(step):
        if not isinstance(step, ActionStep):
            return step
        name, _ = _tool(step)
        idx = position.get(id(step))
        if idx in keep_whole:
            return step
        if name in ("load_skill", "update_plan"):
            replaced = copy.copy(step)
            replaced.observations = "（内容已过时，以后面更新的版本为准）"
            return replaced
        return _truncated(step)

    current, previous = segs[-1], segs[:-1]
    out_prev = []
    for i, seg in enumerate(previous):
        recent = i >= len(previous) - FULL_RECENT_RUNS
        out_prev.append([shrink(s) for s in seg] if recent else _final_steps(seg))

    # 当前轮本身就很长：截短较早几步的工具输出，最近几步原样保留
    if sum(_size(s) for s in current) > budget_chars * CURRENT_RUN_SHARE:
        actions = [s for s in current if isinstance(s, ActionStep)]
        recent_ids = {id(s) for s in actions[-RECENT_STEPS_IN_RUN:]}
        current = [s if id(s) in recent_ids else shrink(s) for s in current]

    # 还是超预算：从最早的轮次开始整轮省略
    used = sum(_size(s) for s in current) + sum(_size(s) for seg in out_prev for s in seg)
    omitted = 0
    while out_prev and used > budget_chars:
        used -= sum(_size(s) for s in out_prev.pop(0))
        omitted += 1

    result = [s for seg in out_prev for s in seg] + current
    if omitted:
        result.insert(0, NoteStep(f"（为控制长度，更早的 {omitted} 轮对话已省略。需要时可以请用户重新说明。）"))
    return result


def install(agent, budget_chars: int):
    """让 agent 组装消息时使用压缩视图。保存到数据库的仍是完整记忆。"""
    def write_memory_to_messages(summary_mode: bool = False):
        messages = agent.memory.system_prompt.to_messages(summary_mode=summary_mode)
        for step in compact(agent.memory.steps, budget_chars):
            messages.extend(step.to_messages(summary_mode=summary_mode))
        return messages

    agent.write_memory_to_messages = write_memory_to_messages
