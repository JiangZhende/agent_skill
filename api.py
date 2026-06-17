#!/usr/bin/env python
"""
FastAPI 入口：agent run 的 SSE 流式接口。

用法:
    uvicorn api:app --reload --port 8000

接口:
    POST /run          执行 agent，返回 SSE 流
    DELETE /sessions/{session_id}   清除 session
    GET  /health       健康检查

SSE 事件格式（每行 data: <json>\\n\\n）:
    {"type": "token",  "content": "..."}          # final_answer 逐字 token
    {"type": "step",   "role": "...", "content": "...", "metadata": {...}}  # 步骤完成
    {"type": "done",   "answer": "..."}            # 运行完成，附最终回答
    {"type": "error",  "content": "..."}           # 运行出错
"""
import asyncio
import json
import os
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).parent.resolve()

import sys
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

MODEL_ID     = os.getenv("MODEL_ID", "Pro/zai-org/GLM-5.1")
API_BASE     = os.getenv("API_BASE", "https://api.siliconflow.cn/v1/")
API_KEY      = os.getenv("API_KEY") or ""
SKILLS_DIR   = PROJECT_ROOT / "skills"
USE_DOCKER   = os.getenv("USE_DOCKER", "0").strip() in ("1", "true", "yes")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "agent-sandbox:latest")

from core.agent import build_agent
from tools import ALL_TOOLS
from smolagents.memory import ActionStep, PlanningStep, FinalAnswerStep
from smolagents.gradio_ui import pull_messages_from_step
from smolagents.models import ChatMessageStreamDelta, agglomerate_stream_deltas

# ── 全局 session 存储（进程内存，重启后清空）────────────────────────────────
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _get_or_create_session(session_id: str | None, req_model_id, req_api_base, req_api_key) -> tuple[dict, str]:
    with _sessions_lock:
        if session_id and session_id in _sessions:
            return _sessions[session_id], session_id

        sid = session_id or uuid.uuid4().hex
        outputs_dir = Path(tempfile.mkdtemp(prefix="agent_out_"))
        agent = build_agent(
            model_id=(req_model_id or MODEL_ID).strip(),
            api_base=(req_api_base or API_BASE).strip(),
            api_key=req_api_key or API_KEY,
            skills_dir=SKILLS_DIR,
            extra_tools=ALL_TOOLS,
            outputs_persist_to=outputs_dir,
            use_docker=USE_DOCKER,
            docker_image=DOCKER_IMAGE,
        )
        session = {"agent": agent, "outputs_dir": str(outputs_dir), "is_new": True}
        _sessions[sid] = session
        return session, sid


# ── SSE 辅助 ────────────────────────────────────────────────────────────────

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _extract_answer_from_deltas(accumulated: list[ChatMessageStreamDelta]) -> str:
    """从累积 delta 中提取 final_answer.answer 文本（native tool call 和 content JSON 双路径）。"""
    msg = agglomerate_stream_deltas(accumulated)

    if msg.tool_calls:
        tc = msg.tool_calls[0]
        if tc.function.name != "final_answer":
            return ""
        raw = tc.function.arguments or ""
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
            return args.get("answer", "") if isinstance(args, dict) else ""
        except json.JSONDecodeError:
            import re
            m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
            if m:
                partial = m.group(1)
                partial = re.sub(r'\\u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), partial)
                return partial.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return ""

    if msg.content:
        content = msg.content.strip()
        if not content.startswith("{"):
            return content
        import re
        tool_m = re.search(r'"(?:name|tool)"\s*:\s*"([^"]+)"', content)
        if not tool_m or tool_m.group(1) != "final_answer":
            return ""
        m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', content)
        if m:
            partial = m.group(1)
            try:
                return json.loads(f'"{partial}"')
            except json.JSONDecodeError:
                partial = re.sub(r'\\u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), partial)
                return partial.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return ""


# ── 同步 → 异步桥接 ─────────────────────────────────────────────────────────

def _run_agent_thread(agent, task: str, reset: bool, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """在子线程中运行 agent，通过 queue 把事件推送到异步生成器。"""
    final_answer = None
    accumulated_deltas: list[ChatMessageStreamDelta] = []
    try:
        for event in agent.run(task, stream=True, reset=reset):
            if isinstance(event, ChatMessageStreamDelta):
                accumulated_deltas.append(event)
                content = _extract_answer_from_deltas(accumulated_deltas)
                if content:
                    asyncio.run_coroutine_threadsafe(
                        queue.put({"type": "token", "content": content}), loop
                    ).result()

            elif isinstance(event, (ActionStep, PlanningStep, FinalAnswerStep)):
                accumulated_deltas = []
                for msg in pull_messages_from_step(
                    event, skip_model_outputs=getattr(agent, "stream_outputs", False)
                ):
                    payload = {"type": "step", "role": msg.role, "content": msg.content or ""}
                    if msg.metadata:
                        payload["metadata"] = msg.metadata
                    asyncio.run_coroutine_threadsafe(queue.put(payload), loop).result()

                if isinstance(event, FinalAnswerStep):
                    final_answer = str(event.answer) if hasattr(event, "answer") else None

    except Exception as e:
        asyncio.run_coroutine_threadsafe(
            queue.put({"type": "error", "content": str(e)}), loop
        ).result()
    finally:
        asyncio.run_coroutine_threadsafe(
            queue.put({"type": "done", "answer": final_answer or ""}), loop
        ).result()


async def _event_stream(session: dict, session_id: str, task: str):
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    reset = session.pop("is_new", True)
    agent = session["agent"]

    thread = threading.Thread(
        target=_run_agent_thread,
        args=(agent, task, reset, queue, loop),
        daemon=True,
    )
    thread.start()

    while True:
        event = await queue.get()
        yield _sse(event)
        if event["type"] in ("done", "error"):
            break

    thread.join(timeout=5)


# ── FastAPI ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # 关闭时清理所有 session
    with _sessions_lock:
        for s in _sessions.values():
            try:
                s["agent"].cleanup()
            except Exception:
                pass
        _sessions.clear()


app = FastAPI(title="Agent Skills API", lifespan=lifespan)


class RunRequest(BaseModel):
    task: str
    session_id: str | None = None
    model_id: str | None = None
    api_base: str | None = None
    api_key: str | None = None


@app.post("/run")
async def run(req: RunRequest):
    """执行 agent 任务，返回 SSE 流。

    客户端示例：
        curl -X POST http://localhost:8000/run \\
             -H "Content-Type: application/json" \\
             -d '{"task": "北京负责人是谁", "session_id": "abc"}' \\
             --no-buffer
    """
    session, sid = _get_or_create_session(req.session_id, req.model_id, req.api_base, req.api_key)

    headers = {"X-Session-Id": sid}
    return StreamingResponse(
        _event_stream(session, sid, req.task),
        media_type="text/event-stream",
        headers=headers,
    )


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """清除指定 session（释放 workspace 和 agent 资源）。"""
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        session["agent"].cleanup()
    except Exception:
        pass
    return {"deleted": session_id}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}
