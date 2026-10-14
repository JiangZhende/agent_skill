"""
单次运行的预算与防空转。

- 步数、token、时长三种预算，任一用完就停止，并让模型交代"做完了什么、还剩什么"，
  而不是凭不完整的信息硬给答案
- 同一工具、同一参数连续失败 2 次，或连续调用 3 次，视为在原地打转，停止
- update_plan 工具：任务较长时让模型维护一份计划清单，随过程推送给用户，停止时用来列出未完成项
"""
import json
import time

from smolagents import tool
from smolagents.memory import ActionStep

FAILED_MARKERS = ("[错误]", "[超时]", "[参数引用失败]", "[执行错误]", "[输出超限]", "[配额超限]", "[次数已用完]")
STATUS_ICONS = {"done": "✔", "in_progress": "▶", "pending": "☐"}

_STOPPED_PROMPT = """\
你在执行用户的任务，但因为{reason}，任务在完成之前被停止了。下面是到目前为止的过程。"""

_STOPPED_POST = """\
请用中文直接回复用户（不要再调用任何工具），按以下结构：

1. **已完成**：到目前为止做成了什么、得到了哪些确定的结果（只写过程里真实出现过的内容）
2. **未完成**：还差哪些事没做{plan_hint}
3. **说明**：为什么停下（{reason}），以及用户可以怎么继续（比如缩小范围、补充信息后再发一次）

用户最初的任务是：
{{{{task}}}}"""


def _signature(step: ActionStep) -> str | None:
    calls = step.tool_calls or []
    if not calls or calls[0].name == "final_answer":
        return None
    args = calls[0].arguments
    return calls[0].name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)


def _failed(step: ActionStep) -> bool:
    if step.error is not None:
        return True
    obs = step.observations or ""
    if obs.lstrip().startswith(FAILED_MARKERS):
        return True
    return "**Exit Code**: " in obs and "**Exit Code**: 0" not in obs


class RunGuard:
    """装到 agent 上的步骤回调：检查预算和打转，需要时让本次运行提前收尾。"""

    def __init__(self, max_steps: int, token_budget: int, time_budget: float):
        self.agent = None
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.time_budget = time_budget
        self._default_final = None
        self.reset()

    def attach(self, agent):
        """装到 agent 上：注册为步骤回调，并在每次 run 开始时清零。"""
        self.agent = agent
        self._default_final = dict(agent.prompt_templates["final_answer"])
        agent.step_callbacks.register(ActionStep, self)
        original_run = agent.run

        def run(*args, **kwargs):
            self.reset()
            return original_run(*args, **kwargs)

        agent.run = run
        agent.guard = self

    def reset(self):
        self.started = time.monotonic()
        self.tokens = 0
        self.history: list[tuple[str | None, bool]] = []
        self.plan: list[dict] = []
        self.stop_reason: str | None = None
        if self.agent is not None and self._default_final is not None:
            self.agent.prompt_templates["final_answer"] = dict(self._default_final)

    def __call__(self, step, agent=None):
        if not isinstance(step, ActionStep) or step.is_final_answer:
            return
        if step.token_usage:
            self.tokens += step.token_usage.input_tokens + step.token_usage.output_tokens
        self.history.append((_signature(step), _failed(step)))

        reason = None
        if self.token_budget and self.tokens >= self.token_budget:
            reason = f"本次任务消耗的 token 已达预算上限（{self.token_budget}）"
        elif self.time_budget and time.monotonic() - self.started >= self.time_budget:
            reason = f"本次任务的执行时间已达上限（{int(self.time_budget)} 秒）"
        elif self._looping():
            reason = "同一个操作反复执行却没有进展"
        elif self.agent.step_number >= self.max_steps:
            reason = f"本次任务的步数已达上限（{self.max_steps} 步）"
        if reason:
            self.stop(reason)

    def _looping(self) -> bool:
        sig, failed = self.history[-1]
        if sig is None:
            return False
        same = 0
        for s, f in reversed(self.history):
            if s != sig:
                break
            same += 1
        last_two_failed = same >= 2 and failed and self.history[-2][1]
        return last_two_failed or same >= 3

    def stop(self, reason: str):
        """让本次运行在这一步之后收尾：smolagents 发现步数用完，会调用模型按下面的模板作总结。"""
        if self.stop_reason:
            return
        self.stop_reason = reason
        pending = [p["step"] for p in self.plan if p.get("status") != "done"]
        plan_hint = f"（计划里未完成的有：{'；'.join(pending)}）" if pending else ""
        self.agent.prompt_templates["final_answer"] = {
            "pre_messages": _STOPPED_PROMPT.format(reason=reason),
            "post_messages": _STOPPED_POST.format(reason=reason, plan_hint=plan_hint),
        }
        # 循环条件是 step_number <= max_steps；这一步结束后会再 +1，正好越过上限
        self.agent.step_number = self.agent.max_steps


def make_plan_tool(guard: RunGuard):
    @tool
    def update_plan(items: list) -> str:
        """维护本次任务的计划清单。任务需要 3 步以上时，开始前先列出计划；每完成一项就再调用一次更新状态。
        简单的任务不需要计划。

        Args:
            items: 计划项列表，每项是 {"step": "要做的事", "status": "pending" | "in_progress" | "done"}
        """
        plan = []
        for item in items or []:
            if isinstance(item, dict) and str(item.get("step", "")).strip():
                status = item.get("status") if item.get("status") in STATUS_ICONS else "pending"
                plan.append({"step": str(item["step"]).strip(), "status": status})
            elif isinstance(item, str) and item.strip():
                plan.append({"step": item.strip(), "status": "pending"})
        if not plan:
            return "[错误] 计划为空。items 应是 [{\"step\": \"...\", \"status\": \"pending\"}, ...]"
        guard.plan = plan
        done = sum(1 for p in plan if p["status"] == "done")
        lines = [f"计划（{done}/{len(plan)} 已完成）："]
        lines += [f"{STATUS_ICONS[p['status']]} {p['step']}" for p in plan]
        return "\n".join(lines)

    return update_plan
