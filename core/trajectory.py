"""
Trajectory recorder: serializes agent.memory to JSONL after each run.

File layout: trajectories/<YYYYMMDD_HHMMSS>_<task_slug>.jsonl
Each line is one record:
  - {"type": "system_prompt","run_id", "content"}
  - {"type": "task",         "run_id", "timestamp", "task"}
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


def save(agent, task: str, trajectories_dir: Path = Path("trajectories")) -> Path:
    """Dump agent.memory to a JSONL file and return the path."""
    trajectories_dir.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{_task_slug(task)}.jsonl"
    out_path = trajectories_dir / filename

    records = []

    system_prompt = getattr(agent.memory.system_prompt, "system_prompt", None)
    if system_prompt:
        records.append({
            "type": "system_prompt",
            "run_id": run_id,
            "content": system_prompt,
        })

    records.append({
        "type": "task",
        "run_id": run_id,
        "timestamp": ts,
        "task": task,
    })

    for step in agent.memory.steps:
        cls = type(step).__name__

        if cls == "TaskStep":
            continue  # already captured as "task" record above

        if cls == "ActionStep":
            step_num = step.step_number

            if step.tool_calls:
                for tc in step.tool_calls:
                    records.append({
                        "type": "tool_call",
                        "run_id": run_id,
                        "step": step_num,
                        "tool": tc.name,
                        "args": tc.arguments if isinstance(tc.arguments, dict) else {},
                        "duration": step.duration,
                    })

            if step.error:
                records.append({
                    "type": "error",
                    "run_id": run_id,
                    "step": step_num,
                    "content": str(step.error),
                })
            elif step.action_output is not None:
                records.append({
                    "type": "final_answer",
                    "run_id": run_id,
                    "step": step_num,
                    "content": str(step.action_output),
                })
            elif step.observations:
                records.append({
                    "type": "observation",
                    "run_id": run_id,
                    "step": step_num,
                    "content": step.observations,
                })

    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return out_path
