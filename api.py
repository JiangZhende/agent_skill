#!/usr/bin/env python
"""
FastAPI 入口：agent run 的 SSE 流式接口。

用法:
    python -m core.auth add <用户名> --admin    # 首次使用先创建用户，拿到 token
    uvicorn api:app --port 8000

所有接口（除 /health）都需要 `Authorization: Bearer <token>`。

接口:
    POST   /run                                   执行 agent，返回 SSE 流；响应头 X-Session-Id 是会话 id
    GET    /sessions                              列出自己的会话（服务重启后仍在）
    GET    /sessions/{id}                         会话详情和历次运行；?events=1 带上每次运行的过程
    GET    /sessions/{id}/files                   会话的上传文件和产物
    GET    /sessions/{id}/files/{area}/{name}     下载一个文件（area 为 inputs 或 outputs）
    DELETE /sessions/{id}                         删除自己的会话（连同文件和记录）
    GET    /models                                可选的模型
    GET    /health                                健康检查

SSE 事件格式（每行 data: <json>\\n\\n）:
    {"type": "token",  "content": "..."}          # final_answer 逐字 token
    {"type": "step",   "role": "...", "content": "...", "metadata": {...}}  # 步骤完成
    {"type": "done",   "answer": "..."}            # 运行完成，附最终回答
    {"type": "error",  "content": "..."}           # 运行出错
"""
import asyncio
import json
import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core import auth, config, db, errors
from core.sessions import RunHandle, SessionError, SessionService
from smolagents.memory import ActionStep, PlanningStep, FinalAnswerStep
from smolagents.gradio_ui import pull_messages_from_step
from smolagents.models import ChatMessageStreamDelta, agglomerate_stream_deltas

logger = logging.getLogger("agent.api")

# 会话、对话记忆和文件都在数据库和文件存储里；这个对象只持有可以随时重建的运行时缓存
service = SessionService()


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

def _run_agent_thread(handle: RunHandle, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """在子线程中运行 agent，通过 queue 把事件推送到异步生成器；结束时把结果交回会话服务。"""
    agent = handle.agent
    final_answer = error_text = None
    events: list[dict] = []
    accumulated_deltas: list[ChatMessageStreamDelta] = []

    def emit(payload: dict):
        asyncio.run_coroutine_threadsafe(queue.put(payload), loop).result()

    try:
        # 记忆已经从数据库恢复，所以总是接着之前的对话继续（新会话时记忆本来就是空的）
        for event in agent.run(handle.task, stream=True, reset=False):
            if isinstance(event, ChatMessageStreamDelta):
                accumulated_deltas.append(event)
                content = _extract_answer_from_deltas(accumulated_deltas)
                if content:
                    emit({"type": "token", "content": content})

            elif isinstance(event, (ActionStep, PlanningStep, FinalAnswerStep)):
                accumulated_deltas = []
                for msg in pull_messages_from_step(
                    event, skip_model_outputs=getattr(agent, "stream_outputs", False)
                ):
                    payload = {"type": "step", "role": msg.role, "content": msg.content or ""}
                    if msg.metadata:
                        payload["metadata"] = msg.metadata
                    events.append({k: v for k, v in payload.items() if k != "type"})
                    emit(payload)

                if isinstance(event, FinalAnswerStep):
                    final_answer = str(event.output) if event.output is not None else None

    except Exception as e:
        # 异常详情可能带模型服务地址等内部信息，只记日志，给调用方原因类别和错误编号
        error_text = errors.describe(e, handle.session_id)
        try:
            emit({"type": "error", "content": error_text})
        except Exception:
            pass
    finally:
        # 先落库并释放会话，再通知客户端结束：客户端收到 done 后马上发下一条也不会撞上"正在运行"
        try:
            service.finish_run(handle, answer=final_answer, error=error_text, events=events)
        except Exception:
            logger.exception("finish_run failed (session=%s)", handle.session_id)
        try:
            emit({"type": "done", "answer": final_answer or ""})
        except Exception:
            pass


def _start_run(handle: RunHandle) -> tuple[asyncio.Queue, threading.Thread]:
    """启动 agent 线程。在返回响应之前就启动，保证会话一定会被释放（即使客户端马上断开）。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    thread = threading.Thread(target=_run_agent_thread, args=(handle, queue, loop), daemon=True)
    try:
        thread.start()
    except BaseException:
        service.finish_run(handle, error="运行未能启动")
        raise
    return queue, thread


async def _event_stream(queue: asyncio.Queue, thread: threading.Thread):
    while True:
        event = await queue.get()
        yield _sse(event)
        if event["type"] == "done":
            break

    # 不能在事件循环里直接 join：线程正等着事件循环确认最后一次推送，会互相卡住 5 秒
    await asyncio.to_thread(thread.join, 5)


# ── FastAPI ──────────────────────────────────────────────────────────────────

async def _sweep_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(service.sweep)
        except Exception:
            logger.exception("session sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_engine()           # 连接数据库并升级表结构
    auth.check_startup()
    if config.USE_DOCKER:
        from core.executor import cleanup_orphans
        cleanup_orphans()     # 上次进程崩溃后遗留的沙箱容器
    service.startup()
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    service.close()


app = FastAPI(title="Agent Skills API", lifespan=lifespan)


@app.exception_handler(SessionError)
async def _session_error(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


def current_user(request: Request) -> auth.User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    user = auth.authenticate(token.strip() if scheme.lower() == "bearer" else None)
    if user is None:
        raise HTTPException(status_code=401, detail="未提供有效的 token",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


class RunRequest(BaseModel):
    # 多余的字段（如 api_base / api_key）直接拒绝，而不是悄悄忽略
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=config.MAX_TASK_CHARS)
    session_id: str | None = None   # 不传则新建会话；只能是 X-Session-Id 返回过的值
    model_id: str | None = None     # 只在新建会话时生效，必须在 GET /models 的列表里


@app.post("/run")
async def run(req: RunRequest, user: auth.User = Depends(current_user)):
    """执行 agent 任务，返回 SSE 流。

    客户端示例：
        curl -X POST http://localhost:8000/run \\
             -H "Authorization: Bearer $TOKEN" \\
             -H "Content-Type: application/json" \\
             -d '{"task": "北京负责人是谁"}' \\
             --no-buffer -D -
    """
    handle = await asyncio.to_thread(
        service.start_run, user, req.session_id, req.model_id, req.task, "api")
    queue, thread = _start_run(handle)
    return StreamingResponse(
        _event_stream(queue, thread),
        media_type="text/event-stream",
        headers={"X-Session-Id": handle.session_id, "X-Run-Id": handle.run_id},
    )


@app.get("/sessions")
async def list_sessions(user: auth.User = Depends(current_user)):
    return {"sessions": await asyncio.to_thread(service.list_sessions, user)}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, events: bool = False, user: auth.User = Depends(current_user)):
    """会话详情和历次运行（任务、回答、状态）。events=1 时带上每次运行的过程。"""
    return await asyncio.to_thread(service.get_session, user, session_id, events)


@app.get("/sessions/{session_id}/files")
async def list_files(session_id: str, user: auth.User = Depends(current_user)):
    return {"files": await asyncio.to_thread(service.list_files, user, session_id)}


@app.get("/sessions/{session_id}/files/{area}/{name:path}")
async def download_file(session_id: str, area: str, name: str, user: auth.User = Depends(current_user)):
    """下载会话的一个上传文件（area=inputs）或产物（area=outputs）。"""
    import shutil
    path = await asyncio.to_thread(service.fetch_file, user, session_id, area, name)
    return FileResponse(path, filename=path.name,
                        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: auth.User = Depends(current_user)):
    """删除自己的某个会话，连同它的文件、对话记忆和运行记录。"""
    await asyncio.to_thread(service.delete_session, user, session_id)
    return {"deleted": session_id}


@app.get("/models")
async def models(user: auth.User = Depends(current_user)):
    return {"default": config.MODEL_ID, "models": config.allowed_models()}


@app.get("/health")
async def health():
    return {"status": "ok"}
