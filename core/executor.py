"""
Skill 脚本的隔离执行器。

execute_skill_script 只依赖 SandboxExecutor 接口，不关心脚本跑在哪里：

- DockerExecutor：每个会话、每个 skill 一个常驻容器，docker exec 复用。
  容器只挂载该 skill 目录（只读）和本会话的 workspace，默认断网、只读根文件系统、
  非 root。生产与测试环境使用。
  声明 network: true 的 skill 接入一个没有外部路由的内部网络，只能经出口代理
  （deploy/egress-proxy）访问公网，访问不到内网。
  带 requirements.txt 的 skill 使用单独构建的镜像，运行时不安装任何依赖。
- LocalExecutor：宿主机子进程，仅用于本机开发；AGENT_ENV=production 时拒绝创建。

脚本一律视为不可信代码：读不到服务端密钥、读不到其他会话的文件、
超时与输出量都有硬上限。
"""
import atexit
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

MAX_OUTPUT_BYTES = 10 * 1024 * 1024      # stdout / stderr 各自的读取上限
DEFAULT_MAX_FILE_SIZE = 500 * 1024 * 1024

CONTAINER_IDLE_TTL = int(os.getenv("SANDBOX_IDLE_TTL", "900"))            # 秒
MAX_CONTAINERS_PER_SESSION = int(os.getenv("SANDBOX_MAX_PER_SESSION", "4"))
MAX_CONTAINERS_TOTAL = int(os.getenv("SANDBOX_MAX_CONTAINERS", "32"))

# 本机执行时透传给脚本的环境变量白名单（不含任何密钥）
_LOCAL_ENV_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)

# 联网 skill 的出口方式：proxy = 内部网络 + 出口代理（默认）；direct = 直接接默认网桥（能访问内网，仅调试用）
EGRESS_MODE = os.getenv("SANDBOX_EGRESS", "proxy").strip().lower() or "proxy"
EGRESS_NETWORK = "agent-sandbox-egress"
EGRESS_PROXY_NAME = "agent-egress-proxy"
EGRESS_PROXY_PORT = 3128
EGRESS_PROXY_DIR = PROJECT_ROOT / "deploy" / "egress-proxy"

PIP_INDEX = os.getenv("SANDBOX_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
QUOTA_POLL_SECONDS = 1.0   # 脚本运行期间检查 workspace 用量的间隔

_WORKSPACE_MOUNT = "/agent_workspace"
_RESULTS_MOUNT = "/agent_results"
_SKILL_MOUNT = "/skill"


@dataclass
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False
    quota_exceeded: bool = False


class SandboxExecutor:
    """脚本执行器接口。一个实例对应一个会话。"""

    #: 宿主机上存放 stdout 缓存（@r_xxx）的目录，脚本可读
    results_dir: Path

    def path_for(self, host_path) -> str:
        """把宿主机上的 workspace / results 路径翻译成脚本能看到的路径。"""
        raise NotImplementedError

    def run(self, skill, script_rel: str, argv: list, stdin: bytes | None, timeout: int) -> ExecResult:
        """执行 skill 目录下的 script_rel（相对路径），工作目录为 skill 目录。"""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _run_capped(cmd, stdin, timeout, cwd=None, env=None, over_quota=None) -> ExecResult:
    """运行子进程，stdout/stderr 各自最多读 MAX_OUTPUT_BYTES，超限或超时即杀掉进程组。

    over_quota: 可选的无参函数，运行期间定期调用；返回真时立即杀掉进程（磁盘配额）。
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    out, err = bytearray(), bytearray()
    overflow = threading.Event()

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def pump(stream, buf):
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            room = MAX_OUTPUT_BYTES - len(buf)
            if len(chunk) > room:
                buf += chunk[:room]
                overflow.set()
                kill()
                return
            buf += chunk

    def feed():
        try:
            proc.stdin.write(stdin)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err), daemon=True),
    ]
    if stdin is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()

    timed_out = quota_exceeded = False
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            kill()
            proc.wait()
            break
        try:
            proc.wait(timeout=min(QUOTA_POLL_SECONDS, remaining) if over_quota else remaining)
            break
        except subprocess.TimeoutExpired:
            if over_quota and over_quota():
                quota_exceeded = True
                kill()
                proc.wait()
                break
    for t in threads:
        t.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass

    return ExecResult(
        exit_code=proc.returncode,
        stdout=bytes(out),
        stderr=bytes(err),
        timed_out=timed_out and not overflow.is_set(),
        output_truncated=overflow.is_set(),
        quota_exceeded=quota_exceeded,
    )


def _quota_check(workspace):
    """返回"workspace 是否已超出总配额"的检查函数；没有 workspace 时返回 None。"""
    if workspace is None:
        return None
    return lambda: workspace.check_quota() is not None


# ── 本机执行（仅开发）────────────────────────────────────────────────────────

class LocalExecutor(SandboxExecutor):
    """宿主机子进程执行。没有文件系统和网络隔离，只用于本机开发。"""

    def __init__(self, workspace=None):
        self.workspace = workspace
        if workspace is not None:
            self.results_dir = Path(workspace.root) / "results"
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._owns_results = False
        else:
            self.results_dir = Path(tempfile.mkdtemp(prefix="agent_run_"))
            self._owns_results = True

    def path_for(self, host_path) -> str:
        return str(host_path)

    def run(self, skill, script_rel, argv, stdin, timeout) -> ExecResult:
        # 只透传白名单变量：脚本读不到 API_KEY 等服务端配置
        env = {k: os.environ[k] for k in _LOCAL_ENV_PASSTHROUGH if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.workspace is not None:
            env["AGENT_INPUTS_DIR"] = str(self.workspace.inputs_dir)
            env["AGENT_WORKSPACE_DIR"] = str(self.workspace.workspace_dir)
            env["AGENT_OUTPUTS_DIR"] = str(self.workspace.outputs_dir)
        skill_dir = Path(skill.dir).resolve()
        cmd = [sys.executable, str(skill_dir / script_rel)] + list(argv)
        return _run_capped(cmd, stdin, timeout, cwd=str(skill_dir), env=env,
                           over_quota=_quota_check(self.workspace))

    def close(self) -> None:
        if self._owns_results:
            shutil.rmtree(self.results_dir, ignore_errors=True)


# ── Docker 执行 ──────────────────────────────────────────────────────────────

# 模块级：跨所有会话共享，防止并发重复构建同一镜像
_built_images: set = set()
_build_lock = threading.Lock()

# 容器池的簿记锁：只保护字典增删和计数，不在持锁期间调用 docker
_POOL_LOCK = threading.RLock()
_EXECUTORS: "weakref.WeakSet[DockerExecutor]" = weakref.WeakSet()
_reaper_started = False


@dataclass
class _Container:
    id: str
    last_used: float
    busy: int = 0


def _docker(*args, check=False, timeout=120):
    return subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=check, timeout=timeout,
    )


def _remove_container(container_id: str):
    try:
        _docker("rm", "-f", container_id, timeout=30)
    except Exception:
        pass


def _ensure_image(image: str):
    """镜像不存在时自动 build，模块级锁防止多用户并发重复构建。"""
    if image in _built_images:
        return
    with _build_lock:
        if image in _built_images:  # 双重检查
            return
        if _docker("image", "inspect", image).returncode != 0:
            print(f"[sandbox] 镜像 {image} 不存在，正在自动构建...", flush=True)
            build = subprocess.run(
                ["docker", "build", "-t", image, str(PROJECT_ROOT)],
                check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(
                    f"镜像构建失败（exit {build.returncode}）。"
                    f"请手动运行: docker build -t {image} {PROJECT_ROOT}"
                )
            print(f"[sandbox] 镜像构建完成：{image}", flush=True)
        _built_images.add(image)


def skill_image_tag(skill_dir, base_image: str, name: str = None) -> str | None:
    """带 requirements.txt 的 skill 对应的镜像名；没有依赖文件则返回 None（直接用基础镜像）。

    名字里带依赖内容的哈希：依赖一变就是新镜像，不会用到旧的。
    """
    req = Path(skill_dir) / "requirements.txt"
    try:
        content = req.read_bytes()
    except OSError:
        return None
    if not content.strip():
        return None
    name = name or Path(skill_dir).name   # 发布前内容在版本目录里，目录名不是 skill 名
    safe = re.sub(r"[^a-z0-9_.-]", "", name.lower()).strip("._-") or "skill"
    digest = hashlib.sha256(base_image.encode() + b"\0" + name.encode() + b"\0" + content).hexdigest()[:12]
    return f"agent-skill-{safe}:{digest}"


def build_skill_image(skill_dir, base_image: str = "agent-sandbox:latest", name: str = None) -> str | None:
    """为带 requirements.txt 的 skill 构建镜像（已存在则跳过），返回镜像名。

    在发布 skill 时调用；依赖在这里一次装好，沙箱里运行时不联网装包。
    """
    tag = skill_image_tag(skill_dir, base_image, name)
    if tag is None:
        return None
    _ensure_image(base_image)
    if tag in _built_images:
        return tag
    with _build_lock:
        if tag in _built_images:
            return tag
        if _docker("image", "inspect", tag).returncode != 0:
            print(f"[sandbox] 正在为 skill '{name or Path(skill_dir).name}' 构建镜像 {tag} ...", flush=True)
            ctx = Path(tempfile.mkdtemp(prefix="skill_image_"))
            try:
                shutil.copy2(Path(skill_dir) / "requirements.txt", ctx / "requirements.txt")
                (ctx / "Dockerfile").write_text(
                    f"FROM {base_image}\n"
                    "COPY requirements.txt /tmp/requirements.txt\n"
                    f"RUN pip install --no-cache-dir -i {PIP_INDEX} -r /tmp/requirements.txt\n",
                    encoding="utf-8",
                )
                build = _docker("build", "-t", tag, str(ctx), timeout=900)
            finally:
                shutil.rmtree(ctx, ignore_errors=True)
            if build.returncode != 0:
                tail = (build.stderr or build.stdout).decode("utf-8", errors="replace").strip().splitlines()[-8:]
                raise RuntimeError("skill 依赖安装失败，镜像未构建:\n" + "\n".join(tail))
            print(f"[sandbox] 镜像构建完成：{tag}", flush=True)
        _built_images.add(tag)
    return tag


_egress_lock = threading.Lock()
_egress_ready_at = 0.0


_EGRESS_ENV = ("EGRESS_ALLOWED_PORTS", "EGRESS_ALLOW_DOMAINS", "EGRESS_UPSTREAM_PROXY")


def _proxy_revision() -> str:
    """代理代码和配置的指纹：任一变化后，下次用到时自动重建代理容器。"""
    h = hashlib.sha256((EGRESS_PROXY_DIR / "proxy.py").read_bytes())
    for k in _EGRESS_ENV:
        h.update(f"\0{k}={os.getenv(k, '')}".encode())
    return h.hexdigest()[:12]


def ensure_egress(image: str) -> None:
    """确保内部网络和出口代理容器存在。代理由所有会话（以及多个服务进程）共用，不随会话销毁。"""
    global _egress_ready_at
    with _egress_lock:
        # 近期确认过就不再每次都问 docker
        if time.monotonic() - _egress_ready_at < 30:
            return
        if _docker("network", "inspect", EGRESS_NETWORK).returncode != 0:
            # --internal：这个网络没有通往外部的路由，容器只能访问同网络里的代理
            created = _docker("network", "create", "--internal", "--label", "agent.role=egress", EGRESS_NETWORK)
            if created.returncode != 0 and b"already exists" not in created.stderr:
                raise RuntimeError(f"创建沙箱内部网络失败: {created.stderr.decode('utf-8', 'replace').strip()}")

        rev = _proxy_revision()
        state = _docker("inspect", "-f", '{{.State.Running}} {{index .Config.Labels "agent.proxy_rev"}}',
                        EGRESS_PROXY_NAME)
        if state.returncode != 0 or state.stdout.decode().split() != ["true", rev]:
            _ensure_image(image)
            _docker("rm", "-f", EGRESS_PROXY_NAME, timeout=30)
            run = _docker(
                "run", "-d", "--rm", "--init",
                "--name", EGRESS_PROXY_NAME,
                "--label", "agent.role=egress-proxy",
                "--label", f"agent.proxy_rev={rev}",
                "--memory=128m", "--cpus=0.5", "--pids-limit=512",
                "--security-opt=no-new-privileges", "--cap-drop=ALL",
                "--read-only", "--user=65534:65534",
                "-e", f"EGRESS_PORT={EGRESS_PROXY_PORT}",
                *[x for k in _EGRESS_ENV if os.getenv(k)
                  for x in ("-e", f"{k}={os.environ[k]}")],
                "-v", f"{EGRESS_PROXY_DIR}:/proxy:ro",
                image, "python", "-u", "/proxy/proxy.py",
            )
            # 另一个服务进程可能刚好同时启动了同名代理，那样也算成功
            if run.returncode != 0 and b"already in use" not in run.stderr:
                raise RuntimeError(f"出口代理启动失败: {run.stderr.decode('utf-8', 'replace').strip()}")
            print(f"[sandbox] 出口代理已启动: {EGRESS_PROXY_NAME}", flush=True)

        # 代理先接在默认网桥上（能出网），再接入内部网络（沙箱能找到它）
        connect = _docker("network", "connect", EGRESS_NETWORK, EGRESS_PROXY_NAME)
        if connect.returncode != 0 and b"already exists" not in connect.stderr:
            raise RuntimeError(f"出口代理接入内部网络失败: {connect.stderr.decode('utf-8', 'replace').strip()}")
        _egress_ready_at = time.monotonic()


def _reap_idle():
    """后台线程：回收空闲超过 CONTAINER_IDLE_TTL 的容器。"""
    while True:
        time.sleep(60)
        now = time.monotonic()
        victims = []
        with _POOL_LOCK:
            for ex in list(_EXECUTORS):
                for name, c in list(ex._containers.items()):
                    if c.busy == 0 and now - c.last_used > CONTAINER_IDLE_TTL:
                        victims.append(ex._containers.pop(name))
        for c in victims:
            _remove_container(c.id)
            print(f"[sandbox] 空闲容器已回收: {c.id[:12]}", flush=True)


def _close_all():
    with _POOL_LOCK:
        executors = list(_EXECUTORS)
    for ex in executors:
        ex._stop_containers()


def _start_background():
    global _reaper_started
    with _POOL_LOCK:
        if _reaper_started:
            return
        _reaper_started = True
    threading.Thread(target=_reap_idle, daemon=True, name="sandbox-reaper").start()
    atexit.register(_close_all)


def cleanup_orphans() -> int:
    """清理宿主机上属主进程已退出的沙箱容器（服务崩溃后遗留的）。服务启动时调用。"""
    try:
        ps = _docker(
            "ps", "--filter", "label=agent.managed=1",
            "--format", '{{.ID}} {{.Label "agent.owner_pid"}}',
            timeout=30,
        )
    except Exception:
        return 0
    if ps.returncode != 0:
        return 0
    removed = 0
    for line in ps.stdout.decode().splitlines():
        cid, _, pid = line.strip().partition(" ")
        if not cid or _pid_alive(pid):
            continue
        _remove_container(cid)
        removed += 1
    if removed:
        print(f"[sandbox] 已清理 {removed} 个遗留容器", flush=True)
    return removed


def _pid_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


class DockerExecutor(SandboxExecutor):
    """每会话每 skill 一个常驻容器，按需启动，docker exec 执行脚本。"""

    def __init__(
        self,
        workspace=None,
        image: str = "agent-sandbox:latest",
        runtime: str = "runc",
        egress: str = None,
        session_id: str = None,
    ):
        self.workspace = workspace
        self.image = image
        self.runtime = runtime
        self.egress = egress or EGRESS_MODE   # "proxy" 或 "direct"
        self.session_id = session_id or getattr(workspace, "run_id", None) or uuid.uuid4().hex[:8]
        self.max_file_size = getattr(workspace, "max_file_size", DEFAULT_MAX_FILE_SIZE)

        if workspace is not None:
            # 放在 workspace 根目录下，随 workspace 一起挂进容器
            self.results_dir = Path(workspace.root) / "results"
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._owns_results = False
        else:
            self.results_dir = Path(tempfile.mkdtemp(prefix="agent_run_"))
            self._owns_results = True

        self._containers: dict[str, _Container] = {}
        self._start_lock = threading.Lock()  # 串行化本会话内的容器启动
        with _POOL_LOCK:
            _EXECUTORS.add(self)
        _start_background()

    # ── 路径映射 ──

    def path_for(self, host_path) -> str:
        host_path = Path(host_path)
        mappings = []
        if self.workspace is not None:
            mappings.append((Path(self.workspace.root), _WORKSPACE_MOUNT))
        if self._owns_results:
            mappings.append((self.results_dir, _RESULTS_MOUNT))
        for host_dir, container_dir in mappings:
            try:
                rel = host_path.relative_to(host_dir)
            except ValueError:
                continue
            return f"{container_dir}/{rel.as_posix()}"
        raise ValueError(f"路径不在沙箱挂载范围内: {host_path}")

    # ── 容器生命周期 ──

    def _run_command(self, skill, image: str = None) -> list:
        wants_network = getattr(skill.meta, "network", False)
        via_proxy = wants_network and self.egress == "proxy"
        if not wants_network:
            network = "none"
        elif via_proxy:
            network = EGRESS_NETWORK
        else:
            network = "bridge"
        cmd = [
            "docker", "run", "-d", "--rm", "--init",
            f"--runtime={self.runtime}",
            "--memory=512m",
            "--cpus=0.5",
            "--pids-limit=100",
            f"--ulimit=fsize={self.max_file_size}",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--network={network}",
            "--label", "agent.managed=1",
            "--label", f"agent.owner_pid={os.getpid()}",
            "--label", f"agent.session={self.session_id}",
            "--label", f"agent.skill={skill.name}",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{Path(skill.dir).resolve()}:{_SKILL_MOUNT}:ro",
            f"--workdir={_SKILL_MOUNT}",
        ]
        if self.workspace is not None:
            cmd += [
                "-v", f"{self.workspace.root}:{_WORKSPACE_MOUNT}:rw",
                "-e", f"AGENT_INPUTS_DIR={_WORKSPACE_MOUNT}/inputs",
                "-e", f"AGENT_WORKSPACE_DIR={_WORKSPACE_MOUNT}/workspace",
                "-e", f"AGENT_OUTPUTS_DIR={_WORKSPACE_MOUNT}/outputs",
            ]
        if self._owns_results:
            cmd += ["-v", f"{self.results_dir}:{_RESULTS_MOUNT}:ro"]
        if via_proxy:
            proxy = f"http://{EGRESS_PROXY_NAME}:{EGRESS_PROXY_PORT}"
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "DDGS_PROXY"):
                cmd += ["-e", f"{var}={proxy}"]
        cmd += [image or self.image, "tail", "-f", "/dev/null"]
        return cmd

    def _start(self, skill) -> str:
        _ensure_image(self.image)
        # 带依赖的 skill 用它自己的镜像：正常在发布时已构建好，这里只在缺失时补建
        image = build_skill_image(skill.dir, self.image) or self.image
        if getattr(skill.meta, "network", False) and self.egress == "proxy":
            ensure_egress(self.image)
        cmd = self._run_command(skill, image)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"沙箱容器启动失败: {result.stderr.decode('utf-8', errors='replace').strip()}"
            )
        container_id = result.stdout.decode().strip()
        print(f"[sandbox] 容器已启动: {container_id[:12]}（skill={skill.name}）", flush=True)
        return container_id

    def _pick_victims(self) -> list:
        """为新容器腾位置：调用方持有 _POOL_LOCK。返回已从池中摘除、待删除的容器。"""
        victims = []
        while len(self._containers) >= MAX_CONTAINERS_PER_SESSION:
            idle = [(c.last_used, n) for n, c in self._containers.items() if c.busy == 0]
            if not idle:
                raise RuntimeError("本会话的沙箱容器数已达上限")
            victims.append(self._containers.pop(min(idle)[1]))

        total = sum(len(ex._containers) for ex in _EXECUTORS)
        if total >= MAX_CONTAINERS_TOTAL:
            idle = [
                (c.last_used, id(ex), n, ex)
                for ex in _EXECUTORS for n, c in ex._containers.items() if c.busy == 0
            ]
            if not idle:
                raise RuntimeError("沙箱容器数已达全局上限，请稍后重试")
            _, _, name, ex = min(idle)
            victims.append(ex._containers.pop(name))
        return victims

    def _acquire(self, skill) -> _Container:
        with self._start_lock:
            with _POOL_LOCK:
                c = self._containers.get(skill.name)
                if c is not None:
                    c.busy += 1
                    c.last_used = time.monotonic()
                    return c
                victims = self._pick_victims()
            for v in victims:
                _remove_container(v.id)
            c = _Container(id=self._start(skill), last_used=time.monotonic(), busy=1)
            with _POOL_LOCK:
                self._containers[skill.name] = c
            return c

    def _release(self, c: _Container):
        with _POOL_LOCK:
            c.busy -= 1
            c.last_used = time.monotonic()

    def _discard(self, skill_name: str, c: _Container):
        """销毁容器（连同其中所有残留进程），下次用到该 skill 时重新启动。"""
        with _POOL_LOCK:
            if self._containers.get(skill_name) is c:
                del self._containers[skill_name]
        _remove_container(c.id)

    def _stop_containers(self):
        with _POOL_LOCK:
            containers = list(self._containers.values())
            self._containers.clear()
        for c in containers:
            _remove_container(c.id)
            print(f"[sandbox] 容器已停止: {c.id[:12]}", flush=True)

    # ── 执行 ──

    def _exec(self, skill, script_rel, argv, stdin, timeout) -> tuple[ExecResult, bool]:
        """返回 (结果, 容器是否已不存在)。"""
        c = self._acquire(skill)
        try:
            cmd = ["docker", "exec"]
            if stdin:
                cmd.append("-i")
            # 超时在容器内部强制执行；宿主机侧多留 5 秒兜底
            cmd += [c.id, "timeout", "-s", "KILL", str(timeout),
                    "python", f"{_SKILL_MOUNT}/{script_rel}"] + list(argv)
            started = time.monotonic()
            result = _run_capped(cmd, stdin if stdin else None, timeout + 5,
                                 over_quota=_quota_check(self.workspace))
            elapsed = time.monotonic() - started
        finally:
            self._release(c)

        if result.exit_code == 137 and elapsed >= timeout:
            result.timed_out = True
        gone = (
            result.exit_code != 0
            and not result.stdout
            and (b"No such container" in result.stderr or b"is not running" in result.stderr)
        )
        # 这几种情况下宿主机侧只杀掉了 docker exec 客户端，脚本还在容器里：连容器一起销毁
        if gone or result.timed_out or result.output_truncated or result.quota_exceeded:
            self._discard(skill.name, c)
        return result, gone

    def run(self, skill, script_rel, argv, stdin, timeout) -> ExecResult:
        result, gone = self._exec(skill, script_rel, argv, stdin, timeout)
        if gone:  # 容器被回收或意外退出：重新启动后再试一次
            result, _ = self._exec(skill, script_rel, argv, stdin, timeout)
        return result

    def close(self) -> None:
        self._stop_containers()
        if self._owns_results:
            shutil.rmtree(self.results_dir, ignore_errors=True)


def make_executor(use_docker: bool = False, docker_image: str = "agent-sandbox:latest", workspace=None) -> SandboxExecutor:
    """按配置创建执行器。SANDBOX_RUNTIME 选容器运行时（runc / runsc）。"""
    if use_docker:
        return DockerExecutor(
            workspace=workspace,
            image=docker_image,
            runtime=os.getenv("SANDBOX_RUNTIME", "runc").strip() or "runc",
        )
    if os.getenv("AGENT_ENV", "").strip().lower() == "production":
        raise RuntimeError("生产环境必须启用沙箱：请设置 USE_DOCKER=1（AGENT_ENV=production 时禁止本机执行脚本）")
    return LocalExecutor(workspace=workspace)
