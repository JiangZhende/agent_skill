"""
用户与鉴权。

用户表是一个 JSON 文件（默认项目根目录的 users.json，不进 git），只保存 token 的 SHA-256：

    {"users": [{"name": "alice", "role": "admin", "token_sha256": "..."}]}

管理用户（token 只在创建时显示一次）:

    python -m core.auth add alice --admin
    python -m core.auth add bob
    python -m core.auth add bob --token <自定义>   # 自己指定 token，仅建议本机开发用
    python -m core.auth list
    python -m core.auth remove bob

API 用 `Authorization: Bearer <token>`；Gradio 登录时用户名填 name，密码填 token。
换成 JWT 或统一登录时，只需要替换 authenticate()。
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from core import config

ROLES = ("user", "admin")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass(frozen=True)
class User:
    name: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# 鉴权关闭时（仅开发）所有请求都算这个用户
DEV_USER = User(name="dev", role="admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserStore:
    """从 JSON 文件读用户表；文件变化后自动重新加载，增删用户不用重启服务。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime = None
        self._by_hash: dict[str, User] = {}

    def _load(self) -> dict[str, User]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime, self._by_hash = None, {}
            return self._by_hash
        if mtime != self._mtime:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            by_hash = {}
            for u in data.get("users", []):
                role = u.get("role", "user")
                if role not in ROLES or not _NAME_RE.match(u.get("name", "")):
                    raise ValueError(f"用户表里有非法条目: {u.get('name')!r}")
                by_hash[u["token_sha256"]] = User(name=u["name"], role=role)
            self._mtime, self._by_hash = mtime, by_hash
        return self._by_hash

    def verify(self, token: str | None) -> User | None:
        if not token:
            return None
        with self._lock:
            return self._load().get(_hash(token))

    def users(self) -> list[User]:
        with self._lock:
            return sorted(self._load().values(), key=lambda u: u.name)

    # ── 管理 ──────────────────────────────────────────────────────────────

    def _read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8")).get("users", [])

    def _write_raw(self, users: list[dict]):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, name: str, role: str = "user", token: str = None) -> str:
        """新增用户（已存在则换发 token），返回明文 token。明文不落盘。

        token 不传则随机生成；自己指定的 token 强度由调用方负责。
        """
        if not _NAME_RE.match(name):
            raise ValueError("用户名只能包含字母、数字、下划线、点和连字符，最长 64 位")
        if role not in ROLES:
            raise ValueError(f"role 必须是 {ROLES} 之一")
        token = token or secrets.token_urlsafe(32)
        with self._lock:
            users = self._read_raw()
            if any(u.get("token_sha256") == _hash(token) and u.get("name") != name for u in users):
                raise ValueError("这个 token 已被其他用户使用")
            users = [u for u in users if u.get("name") != name]
            users.append({"name": name, "role": role, "token_sha256": _hash(token)})
            self._write_raw(users)
        return token

    def remove(self, name: str) -> bool:
        with self._lock:
            users = self._read_raw()
            kept = [u for u in users if u.get("name") != name]
            if len(kept) == len(users):
                return False
            self._write_raw(kept)
            return True


def _default_path() -> Path:
    return Path(os.getenv("AUTH_USERS_FILE", str(config.PROJECT_ROOT / "users.json")))


_store = UserStore(_default_path())


def get_store() -> UserStore:
    return _store


def configure(path: Path) -> UserStore:
    """改用另一份用户表（测试用）。"""
    global _store
    _store = UserStore(path)
    return _store


def auth_disabled() -> bool:
    return os.getenv("AUTH_DISABLED", "0").strip().lower() in ("1", "true", "yes")


def check_startup():
    """启动前检查：没有配置任何用户、又没有显式关闭鉴权时拒绝启动。"""
    production = os.getenv("AGENT_ENV", "development").strip().lower() == "production"
    if auth_disabled():
        if production:
            raise RuntimeError("AGENT_ENV=production 时不允许 AUTH_DISABLED=1")
        print("[auth] 警告：鉴权已关闭（AUTH_DISABLED=1），所有请求都以管理员身份执行，仅限本机开发", file=sys.stderr)
        return
    if not get_store().users():
        raise RuntimeError(
            f"还没有任何用户（{get_store().path}）。先创建一个管理员:\n"
            f"    python -m core.auth add <用户名> --admin\n"
            f"只在本机开发时，可以设 AUTH_DISABLED=1 跳过鉴权。"
        )


def authenticate(token: str | None) -> User | None:
    """校验 token，返回用户；失败返回 None。"""
    if auth_disabled():
        return DEV_USER
    return get_store().verify(token)


def authenticate_login(name: str, token: str) -> bool:
    """用户名 + token 登录（Gradio 用）。"""
    user = get_store().verify(token)
    return user is not None and secrets.compare_digest(user.name, name or "")


def user_by_name(name: str | None) -> User | None:
    """按用户名取用户（登录态里只有用户名时用）。"""
    if auth_disabled():
        return DEV_USER
    return next((u for u in get_store().users() if u.name == name), None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m core.auth", description="管理用户")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="新增用户，或给已有用户换发 token")
    p_add.add_argument("name")
    p_add.add_argument("--admin", action="store_true", help="管理员（可安装和卸载 skill）")
    p_add.add_argument("--token", help="自己指定 token（默认随机生成）。短 token 容易被猜中，只适合本机开发")
    sub.add_parser("list", help="列出用户")
    p_rm = sub.add_parser("remove", help="删除用户")
    p_rm.add_argument("name")
    args = parser.parse_args(argv)

    store = get_store()
    if args.cmd == "add":
        try:
            token = store.add(args.name, "admin" if args.admin else "user", token=args.token)
        except ValueError as e:
            parser.error(str(e))
        print(f"用户 {args.name}（{'admin' if args.admin else 'user'}）已写入 {store.path}")
        if args.token:
            if len(args.token) < 16:
                print("警告：这个 token 太短，容易被猜中；服务没有登录失败次数限制，不要用于能被别人访问的部署", file=sys.stderr)
        else:
            print(f"token（只显示这一次）: {token}")
    elif args.cmd == "list":
        for u in store.users():
            print(f"{u.name}\t{u.role}")
    elif args.cmd == "remove":
        if not store.remove(args.name):
            parser.error(f"用户 {args.name} 不存在")
        print(f"已删除 {args.name}")


if __name__ == "__main__":
    main()
