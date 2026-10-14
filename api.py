#!/usr/bin/env python
"""
FastAPI 入口。运行由 worker 执行（默认就在本进程里，见 WORKER_MODE），这里只负责排队和转发事件。

用法:
    python -m core.auth add <用户名> --admin    # 首次使用先创建用户，拿到 token
    uvicorn api:app --port 8000

所有接口（除 /health）都需要 `Authorization: Bearer <token>`。

运行:
    POST   /run                                   提交并等待：返回 SSE 流；响应头 X-Session-Id、X-Run-Id
    POST   /runs                                  只提交，立即返回 run_id；之后用下面的接口查看
    GET    /runs/{id}                             运行的状态、回答和统计
    GET    /runs/{id}/events                      运行事件的 SSE 流；断线后带 Last-Event-ID（或 ?after=序号）接着读
    POST   /runs/{id}/cancel                      取消运行

会话与文件:
    POST   /sessions                              新建一个空会话（先上传文件再提问时用）
    GET    /sessions                              列出自己的会话
    GET    /sessions/{id}                         会话详情和历次运行；?events=1 带上每次运行的过程
    DELETE /sessions/{id}                         删除自己的会话（连同文件和记录）
    POST   /sessions/{id}/files                   上传文件到会话的 inputs（multipart，字段名 files）
    GET    /sessions/{id}/files                   会话的上传文件和产物
    GET    /sessions/{id}/files/{area}/{name}     下载一个文件（area 为 inputs 或 outputs）

Skill 管理（仅管理员）:
    GET    /skills                                已生效的 skill
    GET    /skills/versions                       所有版本（含待审核的）
    POST   /skills                                上传 ZIP 或 SKILL.md，生成待审核版本（multipart，字段名 file）
    GET    /skills/{name}/versions/{v}            版本概要（联网声明、依赖、文件清单）
    POST   /skills/{name}/versions/{v}/publish    发布（或回滚到）该版本
    DELETE /skills/{name}/versions/{v}            删除一个未生效的版本
    DELETE /skills/{name}                         卸载 skill（连同所有版本）

其他:
    GET    /models                                可选的模型
    GET    /health                                健康检查

SSE 事件格式（每条带 id: <序号>）:
    {"type": "token",  "content": "..."}          # 最终回答的流式文本（到目前为止的全文）
    {"type": "step",   "role": "...", "content": "...", "metadata": {...}}  # 步骤完成
    {"type": "error",  "content": "..."}           # 运行出错
    {"type": "done",   "answer": "...", "status": "done|error|cancelled|interrupted"}
"""
import asyncio
import json
import logging
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core import auth, config, db, skill_installer
from core.sessions import SessionError, SessionService
from core.skill_loader import SkillRegistry
from core.worker import Worker

logger = logging.getLogger("agent.api")

SKILLS_DIR = PROJECT_ROOT / "skills"
EVENT_POLL_INTERVAL = 0.2
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 会话、运行、事件和文件都在数据库和文件存储里；这个对象只持有可以随时重建的运行时缓存
service = SessionService()


def _sse(obj: dict) -> str:
    seq = obj.get("seq")
    body = {k: v for k, v in obj.items() if k != "seq"}
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _event_stream(user: auth.User, run_id: str, after: int = 0, cancel_on_disconnect: bool = False):
    """从数据库按序号读运行事件推给客户端，直到 done。客户端断开时运行照常继续（除非要求取消）。"""
    finished = False
    try:
        while True:
            batch = await asyncio.to_thread(service.read_events, user, run_id, after)
            for event in batch:
                after = event["seq"]
                yield _sse(event)
                if event["type"] == "done":
                    finished = True
                    return
            if not batch:
                await asyncio.sleep(EVENT_POLL_INTERVAL)
    finally:
        if cancel_on_disconnect and not finished:
            try:
                await asyncio.to_thread(service.cancel, user, run_id)
            except Exception:
                logger.exception("cancel on disconnect failed (run=%s)", run_id)


# ── FastAPI ──────────────────────────────────────────────────────────────────

async def _sweep_loop():
    while True:
        await asyncio.sleep(15)   # 包括收拾无主的运行：worker 崩溃后十几秒内就能发现
        try:
            await asyncio.to_thread(service.sweep)
        except Exception:
            logger.exception("session sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_engine()           # 连接数据库并升级表结构
    auth.check_startup()
    worker = None
    if config.WORKER_MODE == "embedded":
        if config.USE_DOCKER:
            from core.executor import cleanup_orphans
            cleanup_orphans()     # 上次进程崩溃后遗留的沙箱容器
        service.startup()
        worker = Worker(service).start()
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    if worker is not None:
        await asyncio.to_thread(worker.stop, 10)
    service.close()


app = FastAPI(title="Agent Skills API", lifespan=lifespan)


@app.exception_handler(SessionError)
async def _session_error(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.exception_handler(skill_installer.SkillInstallError)
async def _skill_error(request: Request, exc: skill_installer.SkillInstallError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def current_user(request: Request) -> auth.User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    user = auth.authenticate(token.strip() if scheme.lower() == "bearer" else None)
    if user is None:
        raise HTTPException(status_code=401, detail="未提供有效的 token",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def admin_user(user: auth.User = Depends(current_user)) -> auth.User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="只有管理员可以管理 skill")
    return user


# ── 运行 ─────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    # 多余的字段（如 api_base / api_key）直接拒绝，而不是悄悄忽略
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=config.MAX_TASK_CHARS)
    session_id: str | None = None   # 不传则新建会话；只能是服务端返回过的会话 id
    model_id: str | None = None     # 只在新建会话时生效，必须在 GET /models 的列表里


@app.post("/run")
async def run(req: RunRequest, cancel_on_disconnect: bool = False, user: auth.User = Depends(current_user)):
    """提交运行并以 SSE 流返回过程。断线后可以用 GET /runs/{id}/events 接着读，运行不受影响。

    客户端示例：
        curl -N -D - -X POST http://localhost:8000/run \\
             -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
             -d '{"task": "北京负责人是谁"}'
    """
    session_id, run_id = await asyncio.to_thread(
        service.submit, user, req.session_id, req.model_id, req.task, "api")
    return StreamingResponse(
        _event_stream(user, run_id, 0, cancel_on_disconnect),
        media_type="text/event-stream",
        headers={"X-Session-Id": session_id, "X-Run-Id": run_id},
    )


@app.post("/runs", status_code=202)
async def submit_run(req: RunRequest, user: auth.User = Depends(current_user)):
    """只提交，立即返回。适合长任务：之后轮询 GET /runs/{id}，或订阅 GET /runs/{id}/events。"""
    session_id, run_id = await asyncio.to_thread(
        service.submit, user, req.session_id, req.model_id, req.task, "api")
    return {"session_id": session_id, "run_id": run_id, "status": "queued"}


@app.get("/runs/{run_id}")
async def get_run(run_id: str, user: auth.User = Depends(current_user)):
    return await asyncio.to_thread(service.get_run, user, run_id)


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = None, cancel_on_disconnect: bool = False,
                     user: auth.User = Depends(current_user)):
    """运行事件的 SSE 流。从序号 after 之后开始；没给时取 Last-Event-ID 请求头，都没有则从头。"""
    await asyncio.to_thread(service.get_run, user, run_id)   # 先校验归属，不存在直接 404
    if after is None:
        last = request.headers.get("last-event-id", "")
        after = int(last) if last.isdigit() else 0
    return StreamingResponse(_event_stream(user, run_id, after, cancel_on_disconnect),
                             media_type="text/event-stream")


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: auth.User = Depends(current_user)):
    status = await asyncio.to_thread(service.cancel, user, run_id)
    return {"run_id": run_id, "status": status}


# ── 会话与文件 ───────────────────────────────────────────────────────────────

class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = None
    title: str = ""


@app.post("/sessions", status_code=201)
async def create_session(req: NewSession = None, user: auth.User = Depends(current_user)):
    req = req or NewSession()
    sid = await asyncio.to_thread(service.ensure_session, user, None, req.model_id, "api", req.title)
    return {"session_id": sid}


@app.get("/sessions")
async def list_sessions(user: auth.User = Depends(current_user)):
    return {"sessions": await asyncio.to_thread(service.list_sessions, user)}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, events: bool = False, user: auth.User = Depends(current_user)):
    """会话详情和历次运行（任务、回答、状态、统计）。events=1 时带上每次运行的过程。"""
    return await asyncio.to_thread(service.get_session, user, session_id, events)


@app.post("/sessions/{session_id}/files")
async def upload_files(session_id: str, files: list[UploadFile] = File(...),
                       user: auth.User = Depends(current_user)):
    """上传文件到会话的 inputs，脚本里用 @input:<文件名> 引用。下一次运行开始时会提示模型有新文件。"""
    tmp = Path(tempfile.mkdtemp(prefix="agent_up_"))
    try:
        paths = []
        for f in files:
            name = Path(f.filename or "").name
            if not name or name in (".", ".."):
                raise HTTPException(status_code=400, detail="文件名不合法")
            dest = tmp / name
            size = 0
            with dest.open("wb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail=f"单个文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
                    out.write(chunk)
            paths.append(dest)
        changed = await asyncio.to_thread(service.add_inputs, user, session_id, paths)
        return {"uploaded": [p.name for p in paths], "changed": changed}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/sessions/{session_id}/files")
async def list_files(session_id: str, user: auth.User = Depends(current_user)):
    return {"files": await asyncio.to_thread(service.list_files, user, session_id)}


@app.get("/sessions/{session_id}/files/{area}/{name:path}")
async def download_file(session_id: str, area: str, name: str, user: auth.User = Depends(current_user)):
    """下载会话的一个上传文件（area=inputs）或产物（area=outputs）。"""
    path = await asyncio.to_thread(service.fetch_file, user, session_id, area, name)
    return FileResponse(path, filename=path.name,
                        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: auth.User = Depends(current_user)):
    """删除自己的某个会话，连同它的文件、对话记忆和运行记录。"""
    await asyncio.to_thread(service.delete_session, user, session_id)
    return {"deleted": session_id}


# ── Skill 管理（仅管理员）────────────────────────────────────────────────────

def _build_skill_image(skill_dir, name):
    if config.USE_DOCKER:
        from core.executor import build_skill_image
        build_skill_image(skill_dir, config.DOCKER_IMAGE, name=name)


@app.get("/skills")
async def list_skills(user: auth.User = Depends(current_user)):
    reg = await asyncio.to_thread(SkillRegistry, SKILLS_DIR)
    return {"skills": [{
        "name": s.name, "description": s.meta.description, "network": s.meta.network,
        "version": skill_installer.active_version(s.name, SKILLS_DIR),
    } for s in reg.skills.values()]}


@app.get("/skills/versions")
async def list_skill_versions(user: auth.User = Depends(admin_user)):
    return {"versions": skill_installer.list_versions(SKILLS_DIR)}


@app.post("/skills", status_code=201)
async def upload_skill(file: UploadFile = File(...), user: auth.User = Depends(admin_user)):
    """上传 ZIP 或 SKILL.md，生成一个待审核版本。发布后才生效。"""
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".zip", ".md"):
        raise HTTPException(status_code=400, detail="只支持 .zip 或 SKILL.md")
    tmp = Path(tempfile.mkdtemp(prefix="agent_skill_"))
    try:
        dest = tmp / name
        data = await file.read(skill_installer.MAX_ZIP_BYTES + 1)
        if len(data) > skill_installer.MAX_ZIP_BYTES:
            raise HTTPException(status_code=413, detail="文件过大")
        dest.write_bytes(data)
        install = skill_installer.install_from_zip if suffix == ".zip" else skill_installer.install_from_skill_md
        skill, version, msg = await asyncio.to_thread(install, dest, SKILLS_DIR, user.name)
        return {"name": skill, "version": version, "status": "pending", "message": msg}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/skills/{name}/versions/{version}")
async def skill_version(name: str, version: int, user: auth.User = Depends(admin_user)):
    return {"name": name, "version": version,
            "summary": await asyncio.to_thread(skill_installer.review_summary, name, version, SKILLS_DIR)}


@app.post("/skills/{name}/versions/{version}/publish")
async def publish_skill(name: str, version: int, user: auth.User = Depends(admin_user)):
    """发布（或回滚到）某个版本。带依赖的 skill 会先构建镜像，可能需要一两分钟。"""
    try:
        msg = await asyncio.to_thread(skill_installer.publish, name, version, SKILLS_DIR, user.name,
                                      _build_skill_image)
    except RuntimeError as e:   # 镜像构建失败：版本保持原状态
        raise HTTPException(status_code=422, detail=str(e))
    return {"name": name, "version": version, "message": msg}


@app.delete("/skills/{name}/versions/{version}")
async def discard_skill_version(name: str, version: int, user: auth.User = Depends(admin_user)):
    return {"message": await asyncio.to_thread(skill_installer.discard_version, name, version, SKILLS_DIR)}


@app.delete("/skills/{name}")
async def uninstall_skill(name: str, user: auth.User = Depends(admin_user)):
    _, msg = await asyncio.to_thread(skill_installer.uninstall_skill, name, SKILLS_DIR)
    return {"message": msg}


@app.get("/models")
async def models(user: auth.User = Depends(current_user)):
    return {"default": config.MODEL_ID, "models": config.allowed_models()}


@app.get("/health")
async def health():
    return {"status": "ok"}
