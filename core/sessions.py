"""
会话管理：归属校验、空闲回收、每用户的数量 / 并发 / 频率上限。

会话仍在进程内存里（落库是路线图阶段三的事），这里只负责"谁能用哪个会话、能用多少"。
"""
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from core.auth import User


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
class Session:
    id: str
    owner: str
    agent: object
    model_id: str
    outputs_dir: str = ""
    is_new: bool = True
    busy: bool = False
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.monotonic)


class SessionManager:
    def __init__(self, agent_factory, limiter: UserLimiter, idle_ttl: int, max_per_user: int):
        """
        Args:
            agent_factory: (model_id) -> (agent, outputs_dir)
        """
        self.agent_factory = agent_factory
        self.limiter = limiter
        self.idle_ttl = idle_ttl
        self.max_per_user = max_per_user
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    # ── 运行 ──────────────────────────────────────────────────────────────

    def acquire(self, user: User, session_id: str | None, model_id: str) -> Session:
        """取出（或新建）会话并标记为运行中。结束后必须调用 release。

        session_id 只能是服务端发过的、且属于该用户的 id；别人的和不存在的一律 404。
        """
        self.limiter.start_run(user)
        try:
            self.sweep()
            with self._lock:
                if session_id:
                    session = self._sessions.get(session_id)
                    if session is None or session.owner != user.name:
                        raise SessionError(404, "会话不存在或已过期")
                    if session.busy:
                        raise SessionError(409, "该会话有任务正在运行")
                else:
                    owned = sum(1 for s in self._sessions.values() if s.owner == user.name)
                    if owned >= self.max_per_user:
                        raise SessionError(429, f"会话数已达上限（{self.max_per_user}），请先删除不用的会话")
                    agent, outputs_dir = self.agent_factory(model_id)
                    session = Session(
                        id=secrets.token_urlsafe(24), owner=user.name,
                        agent=agent, model_id=model_id, outputs_dir=str(outputs_dir),
                    )
                    self._sessions[session.id] = session
                session.busy = True
                session.last_used = time.monotonic()
                return session
        except BaseException:
            self.limiter.end_run(user)
            raise

    def release(self, session: Session):
        with self._lock:
            if not session.busy:
                return
            session.busy = False
            session.last_used = time.monotonic()
        self.limiter.end_run(User(name=session.owner))

    # ── 查询与删除 ────────────────────────────────────────────────────────

    def list(self, user: User) -> list[Session]:
        with self._lock:
            return [s for s in self._sessions.values() if s.owner == user.name]

    def delete(self, user: User, session_id: str):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner != user.name:
                raise SessionError(404, "会话不存在或已过期")
            if session.busy:
                raise SessionError(409, "该会话有任务正在运行")
            del self._sessions[session_id]
        _cleanup(session)

    # ── 回收 ──────────────────────────────────────────────────────────────

    def sweep(self) -> int:
        """关闭空闲超时的会话，返回关闭的数量。"""
        now = time.monotonic()
        with self._lock:
            expired = [s for s in self._sessions.values()
                       if not s.busy and now - s.last_used > self.idle_ttl]
            for s in expired:
                del self._sessions[s.id]
        for s in expired:
            _cleanup(s)
        return len(expired)

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            _cleanup(s)

    def __len__(self):
        return len(self._sessions)


def _cleanup(session: Session):
    try:
        session.agent.cleanup()
    except Exception:
        pass
