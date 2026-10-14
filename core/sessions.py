"""
会话服务：两个入口（api.py、app.py）共用。

会话、每次运行、对话记忆和文件清单都在数据库里，文件内容在文件存储里；
这里负责把它们和一次运行串起来：

    handle = service.start_run(user, session_id, model_id, task, source)
    ... handle.agent.run(task, reset=False) ...
    service.finish_run(handle, answer=..., events=...)

agent 每次运行时现建，从数据库恢复记忆；进程重启后会话可以原样继续。
同一会话同时只能有一个运行（目前是进程内的锁，多副本下的锁见路线图阶段四）。
"""
import logging
import secrets
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select, update

from core import config, db, history, session_files, trajectory
from core.auth import User
from core.runtime import (
    WORKSPACE_PREFIX, RuntimePool, SessionRuntime, worker_alive, worker_id, workspace_owner,
)
from core.workspace import Workspace, workspaces_root

logger = logging.getLogger("agent.sessions")


class SessionError(Exception):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class UserLimiter:
    """每用户的请求频率和并发运行数。"""

    def __init__(self, rate_per_minute: int, max_concurrent: int):
        self.rate_per_minute = rate_per_minute
        self.max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = defaultdict(deque)
        self._running: dict[str, int] = defaultdict(int)

    def start_run(self, user: User):
        """登记一次运行；超过频率或并发上限时抛 SessionError(429)。成功后必须配对调用 end_run。"""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[user.name]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.rate_per_minute:
                raise SessionError(429, f"请求过于频繁（每分钟最多 {self.rate_per_minute} 次），请稍后再试")
            if self._running[user.name] >= self.max_concurrent:
                raise SessionError(429, f"同时运行的任务数已达上限（{self.max_concurrent}），请等待已有任务结束")
            hits.append(now)
            self._running[user.name] += 1

    def end_run(self, user: User):
        with self._lock:
            self._running[user.name] = max(0, self._running[user.name] - 1)


@dataclass
class RunHandle:
    """一次进行中的运行。由 start_run 返回，必须交回 finish_run。"""
    session_id: str
    run_id: str
    user: User
    task: str
    source: str
    agent: object
    runtime: SessionRuntime
    is_new_session: bool
    restored_steps: int                      # 运行开始时已保存的记忆步数
    new_inputs: list = field(default_factory=list)   # 本次随消息上传的文件名
    finished: bool = False


def _default_agent_factory(runtime: SessionRuntime, model_id: str, skills_dir: Path):
    from core.agent import assemble_agent
    from core.skill_loader import SkillRegistry
    from tools import ALL_TOOLS

    return assemble_agent(
        registry=SkillRegistry(skills_dir),   # 每次运行重新扫描：新发布的 skill 下一条消息就生效
        skills_dir=skills_dir,
        workspace=runtime.workspace,
        executor=runtime.executor,
        model_id=model_id,
        api_base=config.API_BASE,
        api_key=config.API_KEY,
        extra_tools=ALL_TOOLS,
    )


class SessionService:
    def __init__(self, limiter: UserLimiter = None, pool: RuntimePool = None, agent_factory=None,
                 trajectories_dir: Path = None, skills_dir: Path = None):
        """
        Args:
            agent_factory: (runtime, model_id) -> agent。默认用服务端配置装配真实的 agent。
            skills_dir: 默认 agent 使用的 skill 目录，不传为项目下的 skills/。
        """
        self.skills_dir = Path(skills_dir) if skills_dir else config.PROJECT_ROOT / "skills"
        self.limiter = limiter if limiter is not None else UserLimiter(
            config.RATE_LIMIT_PER_MINUTE, config.MAX_CONCURRENT_RUNS_PER_USER)
        # 注意不能写成 `pool or ...`：空的池有 __len__，会被当成假值
        self.pool = pool if pool is not None else RuntimePool(
            use_docker=config.USE_DOCKER, docker_image=config.DOCKER_IMAGE,
            idle_ttl=config.SESSION_IDLE_TTL, max_per_user=config.MAX_SESSIONS_PER_USER,
        )
        self.agent_factory = agent_factory or (lambda rt, model: _default_agent_factory(rt, model, self.skills_dir))
        self.trajectories_dir = trajectories_dir or config.PROJECT_ROOT / "trajectories"
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    # ── 运行 ──────────────────────────────────────────────────────────────

    def start_run(self, user: User, session_id: str | None, model_id: str | None, task: str,
                  source: str = "api", upload_files: list = None) -> RunHandle:
        """开始一次运行：校验归属和上限，准备好恢复了记忆的 agent。

        Args:
            session_id: 不传则新建会话；只能是属于该用户的会话，别人的和不存在的一律 404
            model_id: 新建会话时使用的模型；已有会话不能中途换模型
            upload_files: 随这条消息上传的本地文件路径，会放进会话的 inputs
        Raises:
            SessionError: 404 / 409（该会话有运行在进行）/ 429（频率或并发超限）/ 400
        """
        self.limiter.start_run(user)
        claimed = runtime = None
        try:
            try:
                model = config.resolve_model(model_id)
            except ValueError as e:
                raise SessionError(400, str(e))

            now = time.time()
            with db.session_scope() as s:
                if session_id:
                    row = s.get(db.ChatSession, session_id)
                    if row is None or row.owner != user.name or row.deleted_at is not None:
                        raise SessionError(404, "会话不存在或已删除")
                    if model_id and model_id.strip() and row.model_id != model:
                        raise SessionError(400, f"该会话使用的模型是 {row.model_id}，不能中途更换")
                    model, is_new = row.model_id, False
                else:
                    session_id = secrets.token_urlsafe(16)
                    s.add(db.ChatSession(id=session_id, owner=user.name, model_id=model, source=source,
                                         title=task.strip().splitlines()[0][:80] if task.strip() else "",
                                         created_at=now, last_used_at=now))
                    is_new = True

            with self._lock:
                if session_id in self._busy:
                    raise SessionError(409, "该会话有任务正在运行")
                self._busy.add(session_id)
                claimed = session_id

            runtime = self.pool.acquire(session_id, user.name)
            new_inputs = _add_inputs(runtime.workspace, upload_files or [])
            agent = self.agent_factory(runtime, model)
            restored = history.restore(agent, session_id)

            run_id = uuid.uuid4().hex[:16]
            with db.session_scope() as s:
                s.add(db.Run(id=run_id, session_id=session_id, owner=user.name, task=task,
                             status="running", started_at=now, worker=worker_id()))
                s.execute(update(db.ChatSession).where(db.ChatSession.id == session_id)
                          .values(last_used_at=now))
            return RunHandle(session_id=session_id, run_id=run_id, user=user, task=task, source=source,
                             agent=agent, runtime=runtime, is_new_session=is_new,
                             restored_steps=restored, new_inputs=new_inputs)
        except BaseException:
            if runtime is not None:
                self.pool.release(runtime)
            if claimed:
                with self._lock:
                    self._busy.discard(claimed)
            self.limiter.end_run(user)
            raise

    def finish_run(self, handle: RunHandle, answer: str = None, error: str = None, events: list = None):
        """结束一次运行：保存记忆、轨迹和文件，释放会话。无论运行成功与否都必须调用，可重复调用。"""
        if handle.finished:
            return
        handle.finished = True
        try:
            self._persist(handle, answer, error, events)
        finally:
            self.pool.release(handle.runtime)
            with self._lock:
                self._busy.discard(handle.session_id)
            self.limiter.end_run(handle.user)

    def _persist(self, handle: RunHandle, answer, error, events):
        # 各项相互独立：一项失败不应该让其余的也丢掉
        try:
            history.save_new_steps(handle.agent, handle.session_id, handle.run_id, handle.restored_steps)
        except Exception:
            logger.exception("save steps failed (session=%s run=%s)", handle.session_id, handle.run_id)

        records = None
        try:
            records = trajectory.build_records(
                handle.agent, handle.task, run_id=handle.run_id, user=handle.user.name,
                session_id=handle.session_id, source=handle.source, steps_from=handle.restored_steps)
            if config.TRAJECTORY_JSONL:
                trajectory.write_jsonl(records, self.trajectories_dir)
        except Exception:
            logger.exception("save trajectory failed (session=%s run=%s)", handle.session_id, handle.run_id)

        try:
            now = time.time()
            with db.session_scope() as s:
                s.execute(update(db.Run).where(db.Run.id == handle.run_id).values(
                    status="error" if error else "done", answer=answer, error=error,
                    events=events, trajectory=records, finished_at=now, **_run_stats(handle)))
                s.execute(update(db.ChatSession).where(db.ChatSession.id == handle.session_id)
                          .values(last_used_at=now))
        except Exception:
            logger.exception("save run failed (session=%s run=%s)", handle.session_id, handle.run_id)

        try:
            session_files.sync_up(handle.session_id, handle.runtime.workspace)
        except Exception:
            logger.exception("sync files failed (session=%s run=%s)", handle.session_id, handle.run_id)

    # ── 查询 ──────────────────────────────────────────────────────────────

    def _owned(self, s, user: User, session_id: str) -> db.ChatSession:
        row = s.get(db.ChatSession, session_id)
        if row is None or row.owner != user.name or row.deleted_at is not None:
            raise SessionError(404, "会话不存在或已删除")
        return row

    def is_busy(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._busy

    def list_sessions(self, user: User, limit: int = 50) -> list[dict]:
        with db.session_scope() as s:
            rows = s.scalars(
                select(db.ChatSession)
                .where(db.ChatSession.owner == user.name, db.ChatSession.deleted_at.is_(None))
                .order_by(db.ChatSession.last_used_at.desc()).limit(limit)
            )
            return [{
                "session_id": r.id, "title": r.title, "model_id": r.model_id, "source": r.source,
                "created_at": int(r.created_at), "last_used_at": int(r.last_used_at),
                "busy": self.is_busy(r.id),
            } for r in rows]

    def get_session(self, user: User, session_id: str, with_events: bool = False) -> dict:
        """会话详情和它的各次运行（按时间顺序）。"""
        with db.session_scope() as s:
            row = self._owned(s, user, session_id)
            runs = s.scalars(select(db.Run).where(db.Run.session_id == session_id).order_by(db.Run.started_at))
            return {
                "session_id": row.id, "title": row.title, "model_id": row.model_id, "source": row.source,
                "created_at": int(row.created_at), "last_used_at": int(row.last_used_at),
                "busy": self.is_busy(row.id),
                "runs": [{
                    "run_id": r.id, "task": r.task, "status": r.status, "answer": r.answer, "error": r.error,
                    "started_at": int(r.started_at),
                    "finished_at": int(r.finished_at) if r.finished_at else None,
                    "steps": r.steps, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                    "max_input_tokens": r.max_input_tokens, "stop_reason": r.stop_reason,
                    **({"events": r.events or []} if with_events else {}),
                } for r in runs],
            }

    def list_files(self, user: User, session_id: str) -> list[dict]:
        with db.session_scope() as s:
            self._owned(s, user, session_id)
        return session_files.list_files(session_id)

    def fetch_file(self, user: User, session_id: str, area: str, name: str, dest_dir: Path = None) -> Path:
        """把会话的一个上传文件或产物取到本地，返回路径。"""
        with db.session_scope() as s:
            self._owned(s, user, session_id)
        if area not in session_files.DOWNLOADABLE:
            raise SessionError(404, "文件不存在")
        try:
            return session_files.fetch(session_id, area, name, dest_dir)
        except FileNotFoundError:
            raise SessionError(404, "文件不存在")

    # ── 删除与回收 ────────────────────────────────────────────────────────

    def delete_session(self, user: User, session_id: str):
        with db.session_scope() as s:
            self._owned(s, user, session_id)
        with self._lock:
            if session_id in self._busy:
                raise SessionError(409, "该会话有任务正在运行")
        self._purge(session_id)

    def _purge(self, session_id: str):
        """彻底删除一个会话：沙箱、工作副本、文件、对话记忆和运行记录。"""
        self.pool.evict(session_id)
        shutil.rmtree(_workspace_dir(session_id), ignore_errors=True)
        session_files.delete_all(session_id)
        with db.session_scope() as s:
            s.execute(delete(db.Step).where(db.Step.session_id == session_id))
            s.execute(delete(db.Run).where(db.Run.session_id == session_id))
            s.execute(delete(db.ChatSession).where(db.ChatSession.id == session_id))

    def sweep(self) -> dict:
        """定期调用：回收空闲会话的沙箱和工作副本，删除超过保留期的会话。"""
        evicted = self.pool.evict_idle()
        purged = 0
        if config.SESSION_RETENTION_DAYS > 0:
            cutoff = time.time() - config.SESSION_RETENTION_DAYS * 86400
            with db.session_scope() as s:
                expired = list(s.scalars(select(db.ChatSession.id).where(db.ChatSession.last_used_at < cutoff)))
            for sid in expired:
                if not self.is_busy(sid):
                    self._purge(sid)
                    purged += 1
        return {"evicted": evicted, "purged": purged}

    def startup(self) -> dict:
        """服务启动时调用：收拾上次进程没来得及收尾的东西。"""
        # 执行它的进程已经不在了的运行，不会再有结果了。
        # 只看进程是否还活着：Web UI 和 API 是两个进程，不能把对方正在跑的运行标成中断
        interrupted = 0
        with db.session_scope() as s:
            for run in s.scalars(select(db.Run).where(db.Run.status == "running")):
                if not worker_alive(run.worker):
                    run.status, run.error, run.finished_at = "interrupted", "服务重启，运行被中断", time.time()
                    interrupted += 1

        # 上次留下的工作副本：先把里面的文件同步进文件存储，再删掉（会话下次用到时会取回）
        recovered = 0
        for d in workspaces_root().glob(f"agent_{WORKSPACE_PREFIX}*"):
            session_id = d.name[len(f"agent_{WORKSPACE_PREFIX}"):]
            if worker_alive(workspace_owner(d)):
                continue  # 另一个还活着的进程正在用它
            with db.session_scope() as s:
                exists = s.get(db.ChatSession, session_id) is not None
            if not exists:
                continue  # 不是这个数据库里的会话（比如换过 DATABASE_URL）：不认识的东西不动
            try:
                session_files.sync_up(session_id, Workspace(run_id=WORKSPACE_PREFIX + session_id))
                shutil.rmtree(d, ignore_errors=True)
                recovered += 1
            except Exception:
                logger.exception("recover workspace failed: %s", d)
        return {"interrupted_runs": interrupted, "recovered_workspaces": recovered}

    def close(self):
        self.pool.close_all()


def _run_stats(handle: RunHandle) -> dict:
    """本次运行的步数、token 用量、上下文峰值，以及（如果被提前停止）原因。"""
    from smolagents.memory import ActionStep
    steps = [s for s in handle.agent.memory.steps[handle.restored_steps:] if isinstance(s, ActionStep)]
    usages = [s.token_usage for s in steps if getattr(s, "token_usage", None)]
    guard = getattr(handle.agent, "guard", None)
    return {
        "steps": len(steps),
        "input_tokens": sum(u.input_tokens for u in usages) if usages else None,
        "output_tokens": sum(u.output_tokens for u in usages) if usages else None,
        "max_input_tokens": max(u.input_tokens for u in usages) if usages else None,
        "stop_reason": getattr(guard, "stop_reason", None),
    }


def _add_inputs(workspace, paths: list) -> list[str]:
    """把随消息上传的文件放进 inputs，返回其中真正新增或内容有变化的文件名。

    页面上的文件框在多轮对话里会一直带着同样的文件，没变的不用重复告诉模型。
    """
    added = []
    for path in paths:
        src = Path(path)
        dest = workspace.inputs_dir / src.name
        if dest.is_file() and dest.stat().st_size == src.stat().st_size \
                and dest.read_bytes() == src.read_bytes():
            continue
        added.append(workspace.add_input(src))
    return added


def _workspace_dir(session_id: str) -> Path:
    return workspaces_root() / f"agent_{WORKSPACE_PREFIX}{session_id}"
