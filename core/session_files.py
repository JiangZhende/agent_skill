"""
会话文件在"工作副本（workspaces/ 下的目录）"和"文件存储"之间的同步。

- sync_up：一次运行结束后，把工作副本里新增、修改、删除的文件同步到文件存储
- sync_down：运行开始前，把文件存储里有、而本地没有或不一致的文件取回
- put_input：不经过工作副本，直接把用户上传的文件存进会话的 inputs（API 上传、排队运行时用）
- fetch：不经过工作副本，直接取一个文件到本地（下载产物用）
"""
import hashlib
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, select

from core import db, storage

# 区域名 → Workspace 上对应的目录
AREAS = ("inputs", "workspace", "outputs", "results")
# 用户可以直接下载的区域
DOWNLOADABLE = ("inputs", "outputs")


def _area_dir(workspace, area: str) -> Path:
    return {
        "inputs": workspace.inputs_dir,
        "workspace": workspace.workspace_dir,
        "outputs": workspace.outputs_dir,
        "results": Path(workspace.root) / "results",
    }[area]


def _key(session_id: str, area: str, name: str) -> str:
    # 文件名可能含任意字符，存储里用它的哈希，避免和对象存储的命名规则冲突
    return f"sessions/{session_id}/{area}/{hashlib.sha256(name.encode('utf-8')).hexdigest()}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan(workspace) -> dict[tuple[str, str], Path]:
    found = {}
    for area in AREAS:
        base = _area_dir(workspace, area)
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink():
                found[(area, p.relative_to(base).as_posix())] = p
    return found


def sync_up(session_id: str, workspace) -> dict:
    """把工作副本的变化同步到文件存储。返回 {"uploaded": n, "deleted": n}。"""
    store = storage.get_store()
    local = _scan(workspace)
    uploaded = deleted = 0
    with db.session_scope() as s:
        rows = {(r.area, r.name): r for r in
                s.scalars(select(db.FileEntry).where(db.FileEntry.session_id == session_id))}
        for (area, name), path in local.items():
            st = path.stat()
            row = rows.get((area, name))
            if row is not None and row.size == st.st_size and row.mtime_ns == st.st_mtime_ns:
                continue
            key = _key(session_id, area, name)
            digest = _sha256(path)
            if row is not None and row.sha256 == digest:
                row.mtime_ns = st.st_mtime_ns   # 只是时间变了，内容没变：不用重传
                continue
            store.put(key, path)
            if row is None:
                s.add(db.FileEntry(session_id=session_id, area=area, name=name, size=st.st_size,
                                   mtime_ns=st.st_mtime_ns, storage_key=key, sha256=digest))
            else:
                row.size, row.mtime_ns, row.sha256, row.updated_at = st.st_size, st.st_mtime_ns, digest, time.time()
            uploaded += 1
        for key_, row in rows.items():
            if key_ not in local:
                store.delete(row.storage_key)
                s.delete(row)
                deleted += 1
    return {"uploaded": uploaded, "deleted": deleted}


def sync_down(session_id: str, workspace) -> int:
    """把文件存储里有、而工作副本里没有或不一致的文件取回，返回取回的个数。

    工作副本可能是之前留下的缓存（同一个 worker 上次跑这个会话时的），期间用户可能通过 API
    或另一个进程上传了新版本的文件，所以不能只补缺失的，大小或修改时间对不上的也要更新。
    工作副本里自己的改动在每次运行结束时都已同步出去，这里覆盖它不会丢东西。
    """
    import os
    store = storage.get_store()
    restored = 0
    with db.session_scope() as s:
        rows = list(s.scalars(select(db.FileEntry).where(db.FileEntry.session_id == session_id)))
        for row in rows:
            if row.area not in AREAS:
                continue
            base = _area_dir(workspace, row.area).resolve()
            dest = (base / row.name).resolve()
            if base not in dest.parents:
                continue  # 记录里的名字不应该能指到区域目录之外
            if dest.exists():
                st = dest.stat()
                if st.st_size == row.size and st.st_mtime_ns == row.mtime_ns:
                    continue
            try:
                store.get(row.storage_key, dest)
            except FileNotFoundError:
                continue
            # 恢复原来的修改时间，下次 sync_up 不会把它当成改动过的文件重新上传
            os.utime(dest, ns=(row.mtime_ns, row.mtime_ns))
            restored += 1
    return restored


def put_input(session_id: str, src: Path, name: str = None) -> tuple[str, bool]:
    """把一个上传的文件存进会话的 inputs。返回 (文件名, 是否新增或内容有变化)。"""
    src = Path(src)
    name = Path(name or src.name).name
    if not name or name in (".", ".."):
        raise ValueError("文件名不合法")
    digest = _sha256(src)
    st = src.stat()
    key = _key(session_id, "inputs", name)
    with db.session_scope() as s:
        row = s.scalar(select(db.FileEntry).where(
            db.FileEntry.session_id == session_id, db.FileEntry.area == "inputs", db.FileEntry.name == name))
        if row is not None and row.sha256 == digest:
            return name, False
        storage.get_store().put(key, src)
        now_ns = time.time_ns()
        if row is None:
            s.add(db.FileEntry(session_id=session_id, area="inputs", name=name, size=st.st_size,
                               mtime_ns=now_ns, storage_key=key, sha256=digest))
        else:
            row.size, row.mtime_ns, row.sha256, row.updated_at = st.st_size, now_ns, digest, time.time()
    return name, True


def inputs_changed_since(session_id: str, since: float) -> list[str]:
    """某个时间点之后新增或更新过的上传文件名（用来告诉模型"本轮新上传了哪些文件"）。"""
    with db.session_scope() as s:
        return list(s.scalars(select(db.FileEntry.name).where(
            db.FileEntry.session_id == session_id, db.FileEntry.area == "inputs",
            db.FileEntry.updated_at > since).order_by(db.FileEntry.name)))


def list_files(session_id: str, areas=DOWNLOADABLE) -> list[dict]:
    with db.session_scope() as s:
        rows = s.scalars(
            select(db.FileEntry)
            .where(db.FileEntry.session_id == session_id, db.FileEntry.area.in_(areas))
            .order_by(db.FileEntry.area, db.FileEntry.name)
        )
        return [{"area": r.area, "name": r.name, "size": r.size, "updated_at": int(r.updated_at)} for r in rows]


def fetch(session_id: str, area: str, name: str, dest_dir: Path = None) -> Path:
    """把一个文件取到本地临时目录并返回路径；不存在时抛 FileNotFoundError。"""
    with db.session_scope() as s:
        row = s.scalar(select(db.FileEntry).where(
            db.FileEntry.session_id == session_id, db.FileEntry.area == area, db.FileEntry.name == name))
        if row is None:
            raise FileNotFoundError(name)
        key = row.storage_key
    dest_dir = Path(dest_dir or tempfile.mkdtemp(prefix="agent_dl_"))
    dest = dest_dir / Path(name).name
    storage.get_store().get(key, dest)
    return dest


def delete_all(session_id: str) -> None:
    """删除会话的全部文件（存储里的对象和清单）。"""
    storage.get_store().delete_prefix(f"sessions/{session_id}/")
    with db.session_scope() as s:
        s.execute(delete(db.FileEntry).where(db.FileEntry.session_id == session_id))
