"""
用户与鉴权。

用户存在数据库的 users 表里（见 core/db.py），只保存 token 的 SHA-256。

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
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from core import config, db

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
    """数据库里的用户表。增删用户立即对所有服务进程生效。"""

    def verify(self, token: str | None) -> User | None:
        if not token:
            return None
        with db.session_scope() as s:
            row = s.scalar(select(db.User).where(db.User.token_sha256 == _hash(token)))
            return User(name=row.name, role=row.role) if row else None

    def users(self) -> list[User]:
        with db.session_scope() as s:
            return [User(name=r.name, role=r.role) for r in s.scalars(select(db.User).order_by(db.User.name))]

    def get(self, name: str) -> User | None:
        with db.session_scope() as s:
            row = s.get(db.User, name) if name else None
            return User(name=row.name, role=row.role) if row else None

    def add(self, name: str, role: str = "user", token: str = None) -> str:
        """新增用户（已存在则换发 token），返回明文 token。明文不落库。

        token 不传则随机生成；自己指定的 token 强度由调用方负责。
        """
        if not _NAME_RE.match(name):
            raise ValueError("用户名只能包含字母、数字、下划线、点和连字符，最长 64 位")
        if role not in ROLES:
            raise ValueError(f"role 必须是 {ROLES} 之一")
        token = token or secrets.token_urlsafe(32)
        with db.session_scope() as s:
            clash = s.scalar(select(db.User).where(db.User.token_sha256 == _hash(token), db.User.name != name))
            if clash is not None:
                raise ValueError("这个 token 已被其他用户使用")
            row = s.get(db.User, name)
            if row is None:
                s.add(db.User(name=name, role=role, token_sha256=_hash(token)))
            else:
                row.role, row.token_sha256 = role, _hash(token)
        return token

    def remove(self, name: str) -> bool:
        with db.session_scope() as s:
            row = s.get(db.User, name)
            if row is None:
                return False
            s.delete(row)
            return True

    def import_json(self, path: Path) -> int:
        """把旧版 users.json 里的用户导入数据库（已存在的用户名跳过），返回导入个数。"""
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8")).get("users", [])
        except (OSError, ValueError):
            return 0
        imported = 0
        with db.session_scope() as s:
            for u in entries:
                name, role, digest = u.get("name", ""), u.get("role", "user"), u.get("token_sha256", "")
                if not _NAME_RE.match(name) or role not in ROLES or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    continue
                if s.get(db.User, name) is None:
                    s.add(db.User(name=name, role=role, token_sha256=digest))
                    imported += 1
        return imported


_store = UserStore()


def get_store() -> UserStore:
    return _store


def legacy_users_file() -> Path:
    return Path(os.getenv("AUTH_USERS_FILE", str(config.PROJECT_ROOT / "users.json")))


def migrate_legacy_users() -> int:
    """数据库里还没有用户、而旧版 users.json 存在时，把它导入。服务启动时调用。"""
    path = legacy_users_file()
    if not path.exists() or get_store().users():
        return 0
    imported = get_store().import_json(path)
    if imported:
        print(f"[auth] 已从 {path} 导入 {imported} 个用户到数据库；该文件不再使用，可以删除", file=sys.stderr)
    return imported


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
    migrate_legacy_users()
    if not get_store().users():
        raise RuntimeError(
            f"还没有任何用户。先创建一个管理员:\n"
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
    return get_store().get(name)


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

    migrate_legacy_users()
    store = get_store()
    where = db.get_engine().url.render_as_string(hide_password=True)
    if args.cmd == "add":
        try:
            token = store.add(args.name, "admin" if args.admin else "user", token=args.token)
        except ValueError as e:
            parser.error(str(e))
        print(f"用户 {args.name}（{'admin' if args.admin else 'user'}）已写入 {where}")
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
