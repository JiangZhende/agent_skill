"""
会话服务：两个入口（api.py、app.py）和 worker 共用。

会话、运行、对话记忆、运行事件和文件清单都在数据库里，文件内容在文件存储里。
一次运行的生命周期：

    submit()   入口：校验归属和上限，给会话加锁，写入一条排队中的运行
    claim()    worker：领取最早排队的运行（多个进程同时领也只有一个能拿到）
    prepare()  worker：取出会话的 workspace 和沙箱，现建 agent 并从数据库恢复记忆
    execute()  worker：运行 agent，把过程写成事件；结束时 finish_run() 落库并解锁
    入口按序号读取事件推给客户端（客户端断线后可以接着读）

因为一切都经过数据库，入口进程和 worker 进程可以分开部署、各自重启；
同一会话同时只能有一个运行，这把锁在数据库里，跨进程、跨副本都有效。
"""
import logging
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select, update

from core import config, db, errors, events, history, session_files, trajectory
from core import events as events_mod
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
    """每用户的请求频率和并发运行数上限。计数来自数据库，所以对所有进程一起生效。"""

    def __init__(self, rate_per_minute: int, max_concurrent: int):
        self.rate_per_minute = rate_per_minute
        self.max_concurrent = max_concurrent

    def check(self, s, user: User, now: float):
        active = s.scalar(select(func.count()).select_from(db.Run).where(
            db.Run.owner == user.name, db.Run.status.in_(("queued", "running"))))
        if active >= self.max_concurrent:
            raise SessionError(429, f"同时运行的任务数已达上限（{self.max_concurrent}），请等待已有任务结束")
        recent = s.scalar(select(func.count()).select_from(db.Run).where(
            db.Run.owner == user.name, db.Run.started_at > now - 60))
        if recent >= self.rate_per_minute:
            raise SessionError(429, f"请求过于频繁（每分钟最多 {self.rate_per_minute} 次），请稍后再试")


@dataclass
class RunHandle:
    """worker 手里一次进行中的运行。由 prepare / start_run 返回，必须交回 finish_run。"""
    session_id: str
    run_id: str
    user: User
    task: str                                # 用户发的原文
    source: str
    agent: object
    runtime: SessionRuntime
    restored_steps: int                      # 运行开始时已保存的记忆步数
    new_inputs: list = field(default_factory=list)   # 上一轮之后新上传或更新的文件
    agent_task: str = ""                     # 实际交给 agent 的任务（附带了新文件的提示）
    writer: events.EventWriter = None
    finished: bool = False
    _cancel_checked_at: float = 0.0

    @property
    def is_new_session(self) -> bool:
        return self.restored_steps == 0


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
        self._active_runs: set[str] = set()   # 本进程正在执行的运行（需要续租）
        self._heartbeat_started = False
        self._closed = False

    # ── 提交（入口进程）────────────────────────────────────────────────────

    def ensure_session(self, user: User, session_id: str | None, model_id: str | None = None,
                       source: str = "api", title: str = "") -> str:
        """返回一个属于该用户的会话 id：给了就校验归属，没给就新建一个空会话。"""
        if session_id:
            with db.session_scope() as s:
                row = self._owned(s, user, session_id)
                if model_id and model_id.strip():
                    try:
                        model = config.resolve_model(model_id)
                    except ValueError as e:
                        raise SessionError(400, str(e))
                    if row.model_id != model:
                        raise SessionError(400, f"该会话使用的模型是 {row.model_id}，不能中途更换")
            return session_id
        try:
            model = config.resolve_model(model_id)
        except ValueError as e:
            raise SessionError(400, str(e))
        now = time.time()
        session_id = secrets.token_urlsafe(16)
        with db.session_scope() as s:
            s.add(db.ChatSession(id=session_id, owner=user.name, model_id=model, source=source,
                                 title=title.strip().splitlines()[0][:80] if title.strip() else "",
                                 created_at=now, last_used_at=now))
        return session_id

    def add_inputs(self, user: User, session_id: str, paths: list) -> list[str]:
        """把上传的文件存进会话的 inputs，返回其中新增或内容有变化的文件名。"""
        with db.session_scope() as s:
            self._owned(s, user, session_id)
        changed = []
        for p in paths or []:
            name, is_new = session_files.put_input(session_id, Path(p))
            if is_new:
                changed.append(name)
        return changed

    def submit(self, user: User, session_id: str | None, model_id: str | None, task: str,
               source: str = "api", upload_files: list = None) -> tuple[str, str]:
        """提交一次运行，返回 (session_id, run_id)。运行由 worker 领取执行。

        Raises:
            SessionError: 404（会话不存在或不属于该用户）/ 409（该会话已有运行在排队或进行）/
                          429（频率或并发超限）/ 400（模型不可用或中途换模型）
        """
        now = time.time()
        with db.session_scope() as s:
            self.limiter.check(s, user, now)
        session_id = self.ensure_session(user, session_id, model_id, source, title=task)
        if upload_files:
            self.add_inputs(user, session_id, upload_files)

        run_id = uuid.uuid4().hex[:16]
        # 运行的开始时间取在文件存好之后：下一轮按"上一轮开始之后更新过的文件"判断哪些是新上传的
        now = time.time()
        with db.session_scope() as s:
            # 会话锁：只有当前没有活动运行时才能占上（一条带条件的 UPDATE，多进程同时提交也只有一个成功）
            locked = s.execute(update(db.ChatSession).where(
                db.ChatSession.id == session_id, db.ChatSession.active_run_id.is_(None),
                db.ChatSession.deleted_at.is_(None),
            ).values(active_run_id=run_id, last_used_at=now)).rowcount
            if not locked:
                raise SessionError(409, "该会话有任务正在运行")
            s.add(db.Run(id=run_id, session_id=session_id, owner=user.name, task=task, status="queued",
                         source=source, started_at=now, queued_at=now))
        return session_id, run_id

    def cancel(self, user: User, run_id: str) -> str:
        """取消一次运行。排队中的直接取消；运行中的在下一个检查点停下。返回运行当前的状态。"""
        with db.session_scope() as s:
            run = self._owned_run(s, user, run_id)
            if run.status == "queued":
                claimed = s.execute(update(db.Run).where(db.Run.id == run_id, db.Run.status == "queued")
                                    .values(status="cancelled", finished_at=time.time(),
                                            error="已取消")).rowcount
                if claimed:
                    self._unlock(s, run.session_id, run_id)
                    session_id = run.session_id
                else:
                    session_id = None
            else:
                session_id = None
                if run.status == "running":
                    run.cancel_requested = True
            status = "cancelled" if session_id else run.status
        if session_id:
            events.EventWriter(run_id).emit("done", {"answer": "", "status": "cancelled"})
        return status

    # ── 执行（worker 进程）─────────────────────────────────────────────────

    def claim(self, run_id: str = None) -> str | None:
        """领取一个排队中的运行（不指定时取最早的），成功返回 run_id。"""
        now = time.time()
        with db.session_scope() as s:
            if run_id is None:
                run_id = s.scalar(select(db.Run.id).where(db.Run.status == "queued")
                                  .order_by(db.Run.queued_at).limit(1))
                if run_id is None:
                    return None
            # 带条件的 UPDATE：多个 worker 抢同一条时只有一个能改成功
            got = s.execute(update(db.Run).where(db.Run.id == run_id, db.Run.status == "queued").values(
                status="running", worker=worker_id(), claimed_at=now,
                lease_until=now + config.RUN_LEASE_SECONDS)).rowcount
        if got:
            self._start_heartbeat()
            with self._lock:
                self._active_runs.add(run_id)
        return run_id if got else None

    def prepare(self, run_id: str) -> RunHandle:
        """为领到的运行准备 agent：取出会话的运行时，现建 agent 并恢复记忆。"""
        with db.session_scope() as s:
            run = s.get(db.Run, run_id)
            session = s.get(db.ChatSession, run.session_id)
            user = User(name=run.owner)
            task, source, session_id, model = run.task, run.source or "api", run.session_id, session.model_id
            previous = s.scalar(select(func.max(db.Run.started_at)).where(
                db.Run.session_id == session_id, db.Run.id != run_id, db.Run.started_at < run.started_at))
        runtime = None
        try:
            runtime = self.pool.acquire(session_id, user.name)
            agent = self.agent_factory(runtime, model)
            restored = history.restore(agent, session_id)
        except BaseException:
            if runtime is not None:
                self.pool.release(runtime)
            raise
        new_inputs = session_files.inputs_changed_since(session_id, previous or 0.0)
        agent_task = task
        if new_inputs:
            names = "、".join(f"`@input:{n}`" for n in new_inputs)
            agent_task = f"[本轮新上传的文件：{names}]\n{task}"
        return RunHandle(session_id=session_id, run_id=run_id, user=user, task=task, source=source,
                         agent=agent, runtime=runtime, restored_steps=restored, new_inputs=new_inputs,
                         agent_task=agent_task, writer=events.EventWriter(run_id))

    def start_run(self, user: User, session_id: str | None, model_id: str | None, task: str,
                  source: str = "api", upload_files: list = None) -> RunHandle:
        """在本进程里立即执行一次运行（提交 + 领取 + 准备），调用方自己驱动 handle.agent，最后交回 finish_run。

        命令行、测试等不经过 worker 的场景用。
        """
        session_id, run_id = self.submit(user, session_id, model_id, task, source, upload_files)
        if self.claim(run_id) is None:
            raise SessionError(409, "运行已被其他 worker 领取")
        try:
            return self.prepare(run_id)
        except BaseException as e:
            self._fail_unprepared(run_id, session_id, e)
            raise

    def run_next(self) -> bool:
        """worker 的一次循环：领取并执行一个排队中的运行。没有可领的返回 False。"""
        run_id = self.claim()
        if run_id is None:
            return False
        try:
            handle = self.prepare(run_id)
        except Exception as e:
            with db.session_scope() as s:
                session_id = s.get(db.Run, run_id).session_id
            self._fail_unprepared(run_id, session_id, e)
            return True
        self.execute(handle)
        return True

    def execute(self, handle: RunHandle):
        """运行 agent，把过程写成事件，最后落库。无论成功、出错还是被取消，都会走到 finish_run。"""
        from smolagents.gradio_ui import pull_messages_from_step
        from smolagents.memory import ActionStep, FinalAnswerStep, PlanningStep
        from smolagents.models import ChatMessageStreamDelta

        agent, writer = handle.agent, handle.writer
        final_answer = error_text = status = None
        shown: list[dict] = []
        deltas = []
        stream = None
        try:
            # 记忆已经从数据库恢复，所以总是接着之前的对话继续
            stream = agent.run(handle.agent_task or handle.task, stream=True, reset=False)
            for event in stream:
                if self._cancel_requested(handle):
                    status = "cancelled"
                    break
                if isinstance(event, ChatMessageStreamDelta):
                    deltas.append(event)
                    content = events.extract_answer_from_deltas(deltas)
                    if content:
                        writer.emit("token", {"content": content})
                elif isinstance(event, (ActionStep, PlanningStep, FinalAnswerStep)):
                    deltas = []
                    for msg in pull_messages_from_step(event, skip_model_outputs=getattr(agent, "stream_outputs", False)):
                        payload = {"role": msg.role, "content": msg.content or ""}
                        if msg.metadata:
                            payload["metadata"] = msg.metadata
                        shown.append(payload)
                        writer.emit("step", payload)
                    if isinstance(event, FinalAnswerStep):
                        final_answer = str(event.output) if event.output is not None else None
        except Exception as e:
            # 异常原文可能带模型服务地址等内部信息，只记日志，给用户原因类别和错误编号
            error_text = errors.describe(e, handle.session_id)
            writer.emit("error", {"content": error_text})
        finally:
            if stream is not None:
                stream.close()   # 被取消时停止仍在进行的模型输出
            self.finish_run(handle, answer=final_answer, error=error_text, events=shown, status=status)

    def finish_run(self, handle: RunHandle, answer: str = None, error: str = None, events: list = None,
                   status: str = None):
        """结束一次运行：保存记忆、轨迹和文件，解锁会话，最后写 done 事件。可重复调用。

        Args:
            events: 展示给用户的过程（step 事件的内容），存进运行记录，重新打开会话时用
            status: 不传时按有无 error 判为 done / error；被取消时传 "cancelled"
        """
        if handle.finished:
            return
        handle.finished = True
        shown = events
        status = status or ("error" if error else "done")
        try:
            self._persist(handle, answer, error, shown, status)
        finally:
            self.pool.release(handle.runtime)
            with self._lock:
                self._active_runs.discard(handle.run_id)
            try:
                with db.session_scope() as s:
                    self._unlock(s, handle.session_id, handle.run_id)
            except Exception:
                logger.exception("unlock session failed (session=%s)", handle.session_id)
            # 先落库、解锁，再通知客户端：客户端收到 done 后马上发下一条也不会撞上"正在运行"
            try:
                writer = handle.writer or events_mod.EventWriter(handle.run_id)
                writer.flush_token()
                writer.emit("done", {"answer": answer or "", "status": status})
                events_mod.drop_tokens(handle.run_id)
            except Exception:
                logger.exception("write done event failed (run=%s)", handle.run_id)

    def _persist(self, handle: RunHandle, answer, error, shown, status):
        # 各项相互独立：一项失败不应该让其余的也丢掉
        try:
            history.save_new_steps(handle.agent, handle.session_id, handle.run_id, handle.restored_steps)
        except Exception:
            logger.exception("save steps failed (session=%s run=%s)", handle.session_id, handle.run_id)

        records = None
        try:
            records = trajectory.build_records(
                handle.agent, handle.agent_task or handle.task, run_id=handle.run_id, user=handle.user.name,
                session_id=handle.session_id, source=handle.source, steps_from=handle.restored_steps)
            if config.TRAJECTORY_JSONL:
                trajectory.write_jsonl(records, self.trajectories_dir)
        except Exception:
            logger.exception("save trajectory failed (session=%s run=%s)", handle.session_id, handle.run_id)

        try:
            now = time.time()
            with db.session_scope() as s:
                s.execute(update(db.Run).where(db.Run.id == handle.run_id).values(
                    status=status, answer=answer, error=error or ("已取消" if status == "cancelled" else None),
                    events=shown, trajectory=records, finished_at=now, lease_until=None, **_run_stats(handle)))
                s.execute(update(db.ChatSession).where(db.ChatSession.id == handle.session_id)
                          .values(last_used_at=now))
        except Exception:
            logger.exception("save run failed (session=%s run=%s)", handle.session_id, handle.run_id)

        try:
            session_files.sync_up(handle.session_id, handle.runtime.workspace)
        except Exception:
            logger.exception("sync files failed (session=%s run=%s)", handle.session_id, handle.run_id)

    def _fail_unprepared(self, run_id: str, session_id: str, exc: BaseException):
        """运行在准备阶段（取运行时、建 agent）就失败了：标记出错、解锁、通知客户端。"""
        text = errors.describe(exc, session_id) if isinstance(exc, Exception) else "运行未能开始"
        with self._lock:
            self._active_runs.discard(run_id)
        with db.session_scope() as s:
            s.execute(update(db.Run).where(db.Run.id == run_id).values(
                status="error", error=text, finished_at=time.time(), lease_until=None))
            self._unlock(s, session_id, run_id)
        writer = events.EventWriter(run_id)
        writer.emit("error", {"content": text})
        writer.emit("done", {"answer": "", "status": "error"})

    def _cancel_requested(self, handle: RunHandle) -> bool:
        """最多每秒查一次数据库，看是否有人请求取消。"""
        now = time.monotonic()
        if now - handle._cancel_checked_at < 1.0:
            return False
        handle._cancel_checked_at = now
        with db.session_scope() as s:
            return bool(s.scalar(select(db.Run.cancel_requested).where(db.Run.id == handle.run_id)))

    @staticmethod
    def _unlock(s, session_id: str, run_id: str):
        s.execute(update(db.ChatSession).where(
            db.ChatSession.id == session_id, db.ChatSession.active_run_id == run_id).values(active_run_id=None))

    # ── 租约 ──────────────────────────────────────────────────────────────

    def _start_heartbeat(self):
        """后台线程：定期给本进程正在执行的运行续租。租约过期的运行会被判定为 worker 已不在。"""
        with self._lock:
            if self._heartbeat_started:
                return
            self._heartbeat_started = True

        def loop():
            while not self._closed:
                time.sleep(max(1.0, config.RUN_LEASE_SECONDS / 3))
                with self._lock:
                    active = list(self._active_runs)
                if not active:
                    continue
                try:
                    with db.session_scope() as s:
                        s.execute(update(db.Run).where(db.Run.id.in_(active), db.Run.status == "running")
                                  .values(lease_until=time.time() + config.RUN_LEASE_SECONDS))
                except Exception:
                    logger.exception("heartbeat failed")

        threading.Thread(target=loop, daemon=True, name="run-heartbeat").start()

    def reap(self) -> dict:
        """收拾无主的运行：租约过期的（worker 没了）标为中断，排队太久没人领的标为出错。都会解锁会话并通知客户端。"""
        now = time.time()
        finished = []
        with db.session_scope() as s:
            for run in s.scalars(select(db.Run).where(db.Run.status == "running", db.Run.lease_until < now)):
                run.status, run.error, run.finished_at = "interrupted", "执行它的 worker 已停止，运行被中断", now
                self._unlock(s, run.session_id, run.id)
                finished.append((run.id, "interrupted", run.error))
            for run in s.scalars(select(db.Run).where(
                    db.Run.status == "queued", db.Run.queued_at < now - config.QUEUE_TIMEOUT)):
                run.status, run.error, run.finished_at = "error", "排队超时：没有可用的 worker", now
                self._unlock(s, run.session_id, run.id)
                finished.append((run.id, "error", run.error))
        for run_id, status, text in finished:
            writer = events.EventWriter(run_id)
            writer.seq = _last_seq(run_id)
            writer.emit("error", {"content": text})
            writer.emit("done", {"answer": "", "status": status})
        return {"interrupted": sum(1 for f in finished if f[1] == "interrupted"),
                "expired": sum(1 for f in finished if f[1] == "error")}

    # ── 运行的查询 ────────────────────────────────────────────────────────

    def _owned_run(self, s, user: User, run_id: str) -> db.Run:
        run = s.get(db.Run, run_id)
        if run is None or run.owner != user.name:
            raise SessionError(404, "运行不存在")
        return run

    def get_run(self, user: User, run_id: str) -> dict:
        with db.session_scope() as s:
            r = self._owned_run(s, user, run_id)
            return {
                "run_id": r.id, "session_id": r.session_id, "task": r.task, "status": r.status,
                "answer": r.answer, "error": r.error, "started_at": int(r.started_at),
                "finished_at": int(r.finished_at) if r.finished_at else None,
                "steps": r.steps, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "max_input_tokens": r.max_input_tokens, "stop_reason": r.stop_reason,
            }

    def read_events(self, user: User, run_id: str, after_seq: int = 0) -> list[dict]:
        with db.session_scope() as s:
            self._owned_run(s, user, run_id)
        return events.read_events(run_id, after_seq)

    # ── 查询 ──────────────────────────────────────────────────────────────

    def _owned(self, s, user: User, session_id: str) -> db.ChatSession:
        row = s.get(db.ChatSession, session_id)
        if row is None or row.owner != user.name or row.deleted_at is not None:
            raise SessionError(404, "会话不存在或已删除")
        return row

    def is_busy(self, session_id: str) -> bool:
        with db.session_scope() as s:
            return s.scalar(select(db.ChatSession.active_run_id).where(db.ChatSession.id == session_id)) is not None

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
                "busy": r.active_run_id is not None,
            } for r in rows]

    def get_session(self, user: User, session_id: str, with_events: bool = False) -> dict:
        """会话详情和它的各次运行（按时间顺序）。"""
        with db.session_scope() as s:
            row = self._owned(s, user, session_id)
            runs = s.scalars(select(db.Run).where(db.Run.session_id == session_id).order_by(db.Run.started_at))
            return {
                "session_id": row.id, "title": row.title, "model_id": row.model_id, "source": row.source,
                "created_at": int(row.created_at), "last_used_at": int(row.last_used_at),
                "busy": row.active_run_id is not None,
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
            if self._owned(s, user, session_id).active_run_id is not None:
                raise SessionError(409, "该会话有任务正在运行")
        self._purge(session_id)

    def _purge(self, session_id: str):
        """彻底删除一个会话：沙箱、工作副本、文件、对话记忆和运行记录。"""
        self.pool.evict(session_id)
        shutil.rmtree(_workspace_dir(session_id), ignore_errors=True)
        session_files.delete_all(session_id)
        with db.session_scope() as s:
            s.execute(delete(db.Step).where(db.Step.session_id == session_id))
            run_ids = select(db.Run.id).where(db.Run.session_id == session_id)
            s.execute(delete(db.RunEvent).where(db.RunEvent.run_id.in_(run_ids)))
            s.execute(delete(db.Run).where(db.Run.session_id == session_id))
            s.execute(delete(db.ChatSession).where(db.ChatSession.id == session_id))

    def sweep(self) -> dict:
        """定期调用：收拾无主的运行，回收空闲会话的沙箱和工作副本，删除超过保留期的会话。"""
        reaped = self.reap()
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
        return {"evicted": evicted, "purged": purged, **reaped}

    def startup(self) -> dict:
        """服务启动时调用：收拾上次进程没来得及收尾的东西。"""
        # 执行它的进程已经不在了的运行，不会再有结果了。
        # 只看进程是否还活着：Web UI 和 API 是两个进程，不能把对方正在跑的运行标成中断
        interrupted = 0
        with db.session_scope() as s:
            for run in s.scalars(select(db.Run).where(db.Run.status == "running")):
                if not worker_alive(run.worker):
                    run.status, run.error, run.finished_at = "interrupted", "服务重启，运行被中断", time.time()
                    self._unlock(s, run.session_id, run.id)
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
        self._closed = True
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


def _last_seq(run_id: str) -> int:
    with db.session_scope() as s:
        return s.scalar(select(func.coalesce(func.max(db.RunEvent.seq), 0)).where(db.RunEvent.run_id == run_id))


def _workspace_dir(session_id: str) -> Path:
    return workspaces_root() / f"agent_{WORKSPACE_PREFIX}{session_id}"
