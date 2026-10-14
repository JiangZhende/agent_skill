"""
数据库：用户、会话、运行记录、对话步骤、文件清单。

默认是项目目录下的 SQLite 文件（data/agent.db），单机直接可用；
多副本部署时把 DATABASE_URL 指向 Postgres，例如：

    DATABASE_URL=postgresql+psycopg://user:pass@host:5432/agent

表结构由 migrations/ 下的迁移脚本管理：

    python -m core.db upgrade            # 升级到最新（服务启动时也会自动执行）
    python -m core.db current            # 查看当前版本
    python -m core.db revision -m "说明"  # 改了下面的模型后，生成一个新的迁移脚本
"""
import argparse
import os
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    BigInteger, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from core import config

MIGRATIONS_DIR = config.PROJECT_ROOT / "migrations"


def default_url() -> str:
    return os.getenv("DATABASE_URL") or f"sqlite:///{config.PROJECT_ROOT / 'data' / 'agent.db'}"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ChatSession(Base):
    """一个会话：同一个用户的多轮对话，共用一个 workspace。"""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16), default="api")      # web / api
    title: Mapped[str] = mapped_column(String(200), default="")         # 第一条消息的开头
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_used_at: Mapped[float] = mapped_column(Float, default=time.time)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_sessions_owner_last_used", "owner", "last_used_at"),)


class Run(Base):
    """一次运行：用户发一条消息到 agent 给出回答。"""
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    owner: Mapped[str] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / done / error / interrupted
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    events: Mapped[list | None] = mapped_column(JSON, nullable=True)      # 展示给用户的过程（与 SSE 的 step 事件一致）
    trajectory: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 本次运行的轨迹记录
    started_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    worker: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 执行它的进程："主机名:pid"
    steps: Mapped[int | None] = mapped_column(Integer, nullable=True)            # 模型调用了几步
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 各步输入 token 之和
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 单次请求最大的输入，即上下文长度的峰值
    stop_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 因预算或打转被提前停止时的原因

    __table_args__ = (
        Index("ix_runs_session_started", "session_id", "started_at"),
        Index("ix_runs_owner_started", "owner", "started_at"),
    )


class Step(Base):
    """agent 记忆里的一步。恢复会话时按 seq 顺序重建 agent.memory。"""
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))   # task / action
    payload: Mapped[dict] = mapped_column(JSON)

    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_steps_session_seq"),)


class FileEntry(Base):
    """会话 workspace 里的一个文件在文件存储中的位置。"""
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    area: Mapped[str] = mapped_column(String(16))    # inputs / workspace / outputs / results
    name: Mapped[str] = mapped_column(String(512))   # 相对于该区域目录的路径
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(700))
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)

    __table_args__ = (UniqueConstraint("session_id", "area", "name", name="uq_files_session_area_name"),)


# ── 连接 ─────────────────────────────────────────────────────────────────────

_engine = None
_Session = None
_url = None


def _make_engine(url: str):
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")   # 读写互不阻塞
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def configure(url: str = None, migrate: bool = True):
    """（重新）连接数据库并把表结构升级到最新。测试里用它指向临时库。"""
    global _engine, _Session, _url
    if _engine is not None:
        _engine.dispose()
    _url = url or default_url()
    _engine = _make_engine(_url)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    if migrate:
        upgrade()
    return _engine


def get_engine():
    if _engine is None:
        configure()
    return _engine


def current_url() -> str:
    get_engine()
    return _url


@contextmanager
def session_scope():
    """一个事务：正常结束提交，抛异常回滚。"""
    get_engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


# ── 迁移 ─────────────────────────────────────────────────────────────────────

def _alembic_config():
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def upgrade():
    from alembic import command
    command.upgrade(_alembic_config(), "head")


def main(argv=None):
    from alembic import command
    parser = argparse.ArgumentParser(prog="python -m core.db", description="数据库迁移")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upgrade", help="升级到最新版本")
    sub.add_parser("current", help="显示当前版本")
    p_rev = sub.add_parser("revision", help="对比模型与数据库，生成新的迁移脚本")
    p_rev.add_argument("-m", "--message", required=True)
    args = parser.parse_args(argv)

    configure(migrate=False)
    cfg = _alembic_config()
    if args.cmd == "upgrade":
        command.upgrade(cfg, "head")
        print(f"已升级到最新: {_safe_url()}")
    elif args.cmd == "current":
        print(_safe_url())
        command.current(cfg, verbose=True)
    elif args.cmd == "revision":
        command.upgrade(cfg, "head")
        command.revision(cfg, message=args.message, autogenerate=True)


def _safe_url() -> str:
    """打印用：去掉连接串里的密码。"""
    return get_engine().url.render_as_string(hide_password=True)


if __name__ == "__main__":
    # 以 `python -m core.db` 运行时本文件是 __main__，而迁移脚本导入的是 core.db；
    # 交给后者执行，保证两边用的是同一个数据库连接
    from core import db as _db
    _db.main()
