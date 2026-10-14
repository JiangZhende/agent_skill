"""
对话记忆的保存与恢复。

smolagents 把一个会话的全部上下文放在 agent.memory.steps 里（只在进程内存中）。
这里把每一步存成 JSON，下次运行时重建出等价的步骤对象，agent 就能接着之前的对话继续，
不依赖某个一直活着的 agent 实例。
"""
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.monitoring import Timing, TokenUsage
from sqlalchemy import func, select

from core import db


class RestoredError(Exception):
    """恢复出来的步骤错误：只需要能还原成当时给模型看的那段文字。"""


def dump_step(step) -> tuple[str, dict] | None:
    """把一步记忆转成 (kind, 可存 JSON 的 payload)；不需要保存的步骤返回 None。"""
    if isinstance(step, TaskStep):
        return "task", {"task": step.task}
    if isinstance(step, ActionStep):
        usage = step.token_usage
        return "action", {
            "step_number": step.step_number,
            "start_time": step.timing.start_time if step.timing else None,
            "end_time": step.timing.end_time if step.timing else None,
            "model_output": step.model_output if isinstance(step.model_output, str) else None,
            "tool_calls": [
                {"name": tc.name, "arguments": tc.arguments if _jsonable(tc.arguments) else str(tc.arguments),
                 "id": tc.id}
                for tc in (step.tool_calls or [])
            ] or None,
            "observations": step.observations,
            "error": str(step.error) if step.error else None,
            "action_output": None if step.action_output is None else str(step.action_output),
            "is_final_answer": bool(step.is_final_answer),
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
        }
    return None


def load_step(kind: str, payload: dict):
    if kind == "task":
        return TaskStep(task=payload["task"])
    if kind == "action":
        step = ActionStep(
            step_number=payload.get("step_number") or 0,
            timing=Timing(start_time=payload.get("start_time") or 0.0, end_time=payload.get("end_time")),
        )
        step.model_output = payload.get("model_output")
        calls = payload.get("tool_calls")
        step.tool_calls = [ToolCall(name=c["name"], arguments=c["arguments"], id=c["id"]) for c in calls] if calls else None
        step.observations = payload.get("observations")
        step.error = RestoredError(payload["error"]) if payload.get("error") else None
        step.action_output = payload.get("action_output")
        step.is_final_answer = bool(payload.get("is_final_answer"))
        if payload.get("input_tokens") is not None:
            step.token_usage = TokenUsage(input_tokens=payload["input_tokens"],
                                          output_tokens=payload.get("output_tokens") or 0)
        return step
    return None


def _jsonable(value) -> bool:
    return isinstance(value, (dict, list, str, int, float, bool, type(None)))


def restore(agent, session_id: str) -> int:
    """把会话已保存的步骤装回 agent.memory，返回步骤数。之后用 agent.run(..., reset=False) 继续。"""
    with db.session_scope() as s:
        rows = s.execute(
            select(db.Step.kind, db.Step.payload).where(db.Step.session_id == session_id).order_by(db.Step.seq)
        ).all()
    steps = [st for st in (load_step(kind, payload) for kind, payload in rows) if st is not None]
    agent.memory.steps = steps
    return len(steps)


def save_new_steps(agent, session_id: str, run_id: str, already_saved: int) -> int:
    """保存本次运行新增的步骤（agent.memory.steps 中第 already_saved 个之后的），返回新增个数。"""
    new = agent.memory.steps[already_saved:]
    with db.session_scope() as s:
        seq = s.scalar(select(func.coalesce(func.max(db.Step.seq), -1)).where(db.Step.session_id == session_id)) + 1
        count = 0
        for step in new:
            dumped = dump_step(step)
            if dumped is None:
                continue
            s.add(db.Step(session_id=session_id, run_id=run_id, seq=seq + count, kind=dumped[0], payload=dumped[1]))
            count += 1
    return count
