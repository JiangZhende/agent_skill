#!/usr/bin/env python
"""
FastAPI 入口：agent run 的 SSE 流式接口。

用法:
    python -m core.auth add <用户名> --admin    # 首次使用先创建用户，拿到 token
    uvicorn api:app --port 8000

所有接口（除 /health）都需要 `Authorization: Bearer <token>`。

接口:
    POST   /run                    执行 agent，返回 SSE 流；响应头 X-Session-Id 是会话 id
    GET    /sessions               列出自己的会话
    DELETE /sessions/{session_id}  删除自己的会话
    GET    /models                 可选的模型
    GET    /health                 健康检查

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
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core import auth, config, errors
from core import trajectory as traj_recorder
from core.agent import build_agent
from core.sessions import Session, SessionError, SessionManager, UserLimiter
from tools import ALL_TOOLS
from smolagents.memory import ActionStep, PlanningStep, FinalAnswerStep
from smolagents.gradio_ui import pull_messages_from_step
from smolagents.models import ChatMessageStreamDelta, agglomerate_stream_deltas

logger = logging.getLogger("agent.api")

SKILLS_DIR = PROJECT_ROOT / "skills"
TRAJECTORIES_DIR = PROJECT_ROOT / "trajectories"


def _make_agent(model_id: str):
    """模型的地址和密钥只来自服务端配置，调用方只能在白名单里选模型名。"""
    outputs_dir = Path(tempfile.mkdtemp(prefix="agent_out_"))
    agent = build_agent(
        model_id=model_id,
        api_base=config.API_BASE,
        api_key=config.API_KEY,
        skills_dir=SKILLS_DIR,
        extra_tools=ALL_TOOLS,
        outputs_persist_to=outputs_dir,
        use_docker=config.USE_DOCKER,
        docker_image=config.DOCKER_IMAGE,
    )
    return agent, outputs_dir


# 会话在进程内存里，重启后清空
manager = SessionManager(
    agent_factory=_make_agent,
    limiter=UserLimiter(config.RATE_LIMIT_PER_MINUTE, config.MAX_CONCURRENT_RUNS_PER_USER),
    idle_ttl=config.SESSION_IDLE_TTL,
    max_per_user=config.MAX_SESSIONS_PER_USER,
)


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

def _run_agent_thread(agent, task: str, reset: bool, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop,
                      on_finish=None, session_id: str = None):
    """在子线程中运行 agent，通过 queue 把事件推送到异步生成器。"""
    final_answer = None
    accumulated_deltas: list[ChatMessageStreamDelta] = []

    def emit(payload: dict):
        asyncio.run_coroutine_threadsafe(queue.put(payload), loop).result()

    try:
        for event in agent.run(task, stream=True, reset=reset):
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
                    emit(payload)

                if isinstance(event, FinalAnswerStep):
                    final_answer = str(event.output) if event.output is not None else None

    except Exception as e:
        # 异常详情可能带模型服务地址等内部信息，只记日志，给调用方原因类别和错误编号
        try:
            emit({"type": "error", "content": errors.describe(e, session_id)})
        except Exception:
            pass
    finally:
        if on_finish:
            on_finish()  # 先释放会话，即使下面的推送因为事件循环已关闭而失败
        try:
            emit({"type": "done", "answer": final_answer or ""})
        except Exception:
            pass


def _start_run(session: Session, task: str) -> tuple[asyncio.Queue, threading.Thread]:
    """启动 agent 线程。在返回响应之前就启动，保证会话一定会被释放（即使客户端马上断开）。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    reset = session.is_new
    session.is_new = False

    def on_finish():
        # 先存轨迹再释放会话：释放后下一轮可能马上开始改 agent.memory
        try:
            traj_recorder.save(session.agent, task, TRAJECTORIES_DIR,
                               user=session.owner, session_id=session.id, source="api")
        except Exception:
            logger.exception("save trajectory failed")
        finally:
            manager.release(session)

    thread = threading.Thread(
        target=_run_agent_thread,
        args=(session.agent, task, reset, queue, loop),
        # 会话在 agent 线程真正结束时才释放：客户端提前断开时线程还在跑
        kwargs={"on_finish": on_finish, "session_id": session.id},
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        manager.release(session)
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
            await asyncio.to_thread(manager.sweep)
        except Exception:
            logger.exception("session sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.check_startup()
    if config.USE_DOCKER:
        from core.executor import cleanup_orphans
        cleanup_orphans()  # 上次进程崩溃后遗留的沙箱容器
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    manager.close_all()


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
    try:
        model_id = config.resolve_model(req.model_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    session = await asyncio.to_thread(manager.acquire, user, req.session_id, model_id)
    if req.model_id and session.model_id != model_id:
        manager.release(session)
        raise HTTPException(status_code=400, detail=f"该会话使用的模型是 {session.model_id}，不能中途更换")

    queue, thread = _start_run(session, req.task)
    return StreamingResponse(
        _event_stream(queue, thread),
        media_type="text/event-stream",
        headers={"X-Session-Id": session.id},
    )


@app.get("/sessions")
async def list_sessions(user: auth.User = Depends(current_user)):
    return {"sessions": [
        {"session_id": s.id, "model_id": s.model_id, "busy": s.busy, "created_at": int(s.created_at)}
        for s in manager.list(user)
    ]}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: auth.User = Depends(current_user)):
    """清除自己的某个会话（释放 workspace 和 agent 资源）。"""
    await asyncio.to_thread(manager.delete, user, session_id)
    return {"deleted": session_id}


@app.get("/models")
async def models(user: auth.User = Depends(current_user)):
    return {"default": config.MODEL_ID, "models": config.allowed_models()}


@app.get("/health")
async def health():
    return {"status": "ok"}
