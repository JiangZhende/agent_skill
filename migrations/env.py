"""Alembic 环境：连接和模型都取自 core.db，不使用 alembic.ini。"""
from alembic import context

from core import db

target_metadata = db.Base.metadata


def run_migrations_online():
    with db.get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # SQLite 改表要走"建新表再拷贝"
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
