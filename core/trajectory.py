"""
Trajectory recorder: serializes agent.memory to JSONL after each run.

File layout: trajectories/<YYYYMMDD_HHMMSS>_<task_slug>_<run_id 前 6 位>.jsonl
Each line is one record:
  - {"type": "system_prompt","run_id", "content"}
  - {"type": "task",         "run_id", "timestamp", "task", "user", "session_id", "source"}
  - {"type": "tool_call",    "run_id", "step", "tool", "args"}
  - {"type": "observation",  "run_id", "step", "content"}
  - {"type": "error",        "run_id", "step", "content"}
  - {"type": "final_answer", "run_id", "step", "content"}
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path


def _task_slug(task: str, max_len: int = 30) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "_", task).strip("_")
    return slug[:max_len]


def build_records(agent, task: str, run_id: str = None, user: str = None, session_id: str = None,
                  source: str = None, steps_from: int = 0) -> list[dict]:
    """把 agent.memory 里的步骤转成轨迹记录。

    Args:
        steps_from: 只取 agent.memory.steps 中这个下标之后的步骤（本次运行新增的部分）；
                    0 表示整个会话到目前为止的全部步骤
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    records = []

    system_prompt = getattr(agent.memory.system_prompt, "system_prompt", None)
    if system_prompt:
        records.append({"type": "system_prompt", "run_id": run_id, "content": system_prompt})

    records.append({
        "type": "task", "run_id": run_id, "timestamp": ts, "task": task,
        "user": user, "session_id": session_id, "source": source,
    })

    for step in agent.memory.steps[steps_from:]:
        if type(step).__name__ != "ActionStep":
            continue  # TaskStep 已经体现在上面的 task 记录里
        step_num = step.step_number
        for tc in (step.tool_calls or []):
            records.append({
                "type": "tool_call", "run_id": run_id, "step": step_num, "tool": tc.name,
                "args": tc.arguments if isinstance(tc.arguments, dict) else {},
                "duration": step.timing.duration if step.timing else None,
            })
        if step.error:
            records.append({"type": "error", "run_id": run_id, "step": step_num, "content": str(step.error)})
        elif step.action_output is not None:
            records.append({"type": "final_answer", "run_id": run_id, "step": step_num,
                            "content": str(step.action_output)})
        elif step.observations:
            records.append({"type": "observation", "run_id": run_id, "step": step_num,
                            "content": step.observations})
    return records


def write_jsonl(records: list[dict], trajectories_dir: Path = Path("trajectories")) -> Path:
    """把一组轨迹记录写成一个 JSONL 文件，返回路径。"""
    trajectories_dir = Path(trajectories_dir)
    trajectories_dir.mkdir(parents=True, exist_ok=True)
    task = next((r for r in records if r["type"] == "task"), {})
    run_id = task.get("run_id") or uuid.uuid4().hex[:12]
    ts = task.get("timestamp") or datetime.now().strftime("%Y%m%d_%H%M%S")
    # 带上 run_id，避免两个用户同一秒问同样的问题时互相覆盖
    out_path = trajectories_dir / f"{ts}_{_task_slug(task.get('task', ''))}_{run_id[:6]}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out_path


def save(agent, task: str, trajectories_dir: Path = Path("trajectories"),
         user: str = None, session_id: str = None, source: str = None) -> Path:
    """把 agent 的全部记忆写成一个 JSONL 文件（命令行和脚本用）。"""
    return write_jsonl(build_records(agent, task, user=user, session_id=session_id, source=source),
                       trajectories_dir)
