"""
会话的运行时资源：workspace 的工作副本和脚本执行器（沙箱容器）。

这些只是缓存，不是状态：对话和文件的权威来源是数据库和文件存储。
运行时被回收（空闲超时、超出每用户上限、服务重启）之后，下次用到该会话时
会从文件存储取回文件、重新起容器，用户感觉不到区别，只是第一步稍慢。
"""
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import session_files
from core.executor import make_executor
from core.workspace import Workspace

WORKSPACE_PREFIX = "s_"   # 会话的工作副本目录是 workspaces/agent_s_<session_id>
OWNER_FILE = ".owner"     # 工作副本目录里的标记：哪个进程正在使用它


def worker_id() -> str:
    """当前进程的标识。Web UI 和 API 是两个进程，各自只收拾自己（或已经死掉的进程）留下的东西。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def worker_alive(worker: str | None) -> bool:
    """这个标识对应的进程是否还活着。别的主机上的进程无从判断，一律当作活着。"""
    host, _, pid = (worker or "").rpartition(":")
    if not host or not pid.isdigit():
        return False
    if host != socket.gethostname():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def workspace_owner(root: Path) -> str | None:
    try:
        return (Path(root) / OWNER_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@dataclass
class SessionRuntime:
    session_id: str
    owner: str
    workspace: Workspace
    executor: object
    busy: bool = False
    last_used: float = field(default_factory=time.monotonic)


class RuntimePool:
    def __init__(self, use_docker: bool, docker_image: str, idle_ttl: int, max_per_user: int):
        self.use_docker = use_docker
        self.docker_image = docker_image
        self.idle_ttl = idle_ttl
        self.max_per_user = max_per_user
        self._lock = threading.Lock()
        self._runtimes: dict[str, SessionRuntime] = {}

    def acquire(self, session_id: str, owner: str) -> SessionRuntime:
        """取出（或重建）会话的运行时并标记为使用中。用完必须 release。"""
        with self._lock:
            rt = self._runtimes.get(session_id)
            victims = []
            if rt is None:
                # 每用户同时保留的运行时有上限：超出时回收最久未用的空闲者（它的内容都已持久化）
                mine = sorted((r for r in self._runtimes.values() if r.owner == owner and not r.busy),
                              key=lambda r: r.last_used)
                held = sum(1 for r in self._runtimes.values() if r.owner == owner)
                while held >= self.max_per_user and mine:
                    victims.append(self._runtimes.pop(mine.pop(0).session_id))
                    held -= 1
        for v in victims:
            _dispose(v)

        if rt is None:
            workspace = Workspace(run_id=WORKSPACE_PREFIX + session_id)
            (Path(workspace.root) / OWNER_FILE).write_text(worker_id(), encoding="utf-8")
            executor = make_executor(use_docker=self.use_docker, docker_image=self.docker_image,
                                     workspace=workspace)
            rt = SessionRuntime(session_id=session_id, owner=owner, workspace=workspace, executor=executor)
            with self._lock:
                rt = self._runtimes.setdefault(session_id, rt)
        with self._lock:
            rt.busy = True
            rt.last_used = time.monotonic()
        # 每次都和文件存储对一遍，不只是新建工作副本时：工作副本缓存期间，用户可能通过 API
        # 或另一个进程上传了同名的新文件。不更新的话，运行结束时旧内容会被同步回去，覆盖新上传的
        try:
            session_files.sync_down(session_id, rt.workspace)
        except BaseException:
            self.release(rt)
            raise
        return rt

    def release(self, rt: SessionRuntime):
        with self._lock:
            rt.busy = False
            rt.last_used = time.monotonic()

    def evict(self, session_id: str) -> bool:
        """回收某个会话的运行时（正在使用的不回收）。"""
        with self._lock:
            rt = self._runtimes.get(session_id)
            if rt is None or rt.busy:
                return False
            del self._runtimes[session_id]
        _dispose(rt)
        return True

    def evict_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [r for r in self._runtimes.values() if not r.busy and now - r.last_used > self.idle_ttl]
            for r in idle:
                del self._runtimes[r.session_id]
        for r in idle:
            _dispose(r)
        return len(idle)

    def close_all(self):
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for r in runtimes:
            _dispose(r)

    def __len__(self):
        return len(self._runtimes)


def _dispose(rt: SessionRuntime):
    """停掉沙箱并删除工作副本。先把文件同步出去：正常情况下每次运行结束都已同步，这里是兜底。"""
    try:
        session_files.sync_up(rt.session_id, rt.workspace)
    except Exception:
        return  # 同步失败就保留工作副本，不能为了回收资源丢用户的文件
    try:
        rt.executor.close()
    except Exception:
        pass
    rt.workspace.cleanup()
