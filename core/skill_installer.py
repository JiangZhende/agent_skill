"""
Skill 的安装、审核发布、版本与卸载。

流程：上传（ZIP 或单个 SKILL.md）只会生成一个"待审核"版本，不影响正在使用的 skill；
管理员检查后发布，该版本才会成为 skills/<name>/ 下生效的内容。再次上传同名 skill
是新增一个版本，不会覆盖；发布旧版本即回滚。

目录布局：
    skills/<name>/                     当前生效的版本（SkillRegistry 只看这里）
    skills/.versions/<name>/<n>/       各版本的完整内容
    skills/.versions/state.json        每个 skill 的版本、状态、操作人

安全约束：
- 压缩包大小、解压后总大小、单文件大小、文件数都有上限；按实际读到的字节数校验，
  不相信压缩包里自报的大小
- 拒绝路径穿越、符号链接和白名单之外的文件类型
- skill 名取自 SKILL.md 的 frontmatter，必须通过名称校验
"""
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path

import yaml

from core.skill_loader import _VALID_NAME_RE

VERSIONS_DIRNAME = ".versions"

MAX_ZIP_BYTES = 10 * 1024 * 1024       # 压缩包本身
MAX_TOTAL_BYTES = 50 * 1024 * 1024     # 解压后总大小
MAX_FILE_BYTES = 10 * 1024 * 1024      # 解压后单个文件
MAX_FILES = 200

_ALLOWED_SUFFIXES = {".py", ".md", ".sh", ".txt", ".yaml", ".yml", ".json", ".csv", ".toml"}
# 打包工具顺手带进来的垃圾文件：直接跳过，不算错误
_IGNORED_NAMES = {".DS_Store", "Thumbs.db"}

PENDING, PUBLISHED, RETIRED = "pending", "published", "retired"
STATUS_LABELS = {PENDING: "待审核", PUBLISHED: "生效中", RETIRED: "已停用"}

_lock = threading.RLock()


class SkillInstallError(ValueError):
    """上传内容不合规，或操作的版本不存在。消息可以直接展示给管理员。"""


# ── 状态文件 ─────────────────────────────────────────────────────────────────

def _versions_root(skills_dir) -> Path:
    return Path(skills_dir) / VERSIONS_DIRNAME


def _load_state(skills_dir) -> dict:
    path = _versions_root(skills_dir) / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_state(skills_dir, state: dict):
    root = _versions_root(skills_dir)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / "state.json")


def _entry(state: dict, name: str) -> dict:
    return state.setdefault(name, {"active": None, "next": 1, "versions": {}})


def _new_version(entry: dict, status: str, user: str | None, **extra) -> int:
    version = entry["next"]
    entry["next"] = version + 1
    entry["versions"][str(version)] = {
        "status": status,
        "created_by": user,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **extra,
    }
    return version


# ── 校验 ─────────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        raise SkillInstallError("SKILL.md 缺少 frontmatter（--- ... ---）")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillInstallError(f"SKILL.md 的 frontmatter 不是合法的 YAML: {e}")
    if not isinstance(fm, dict):
        raise SkillInstallError("SKILL.md 的 frontmatter 必须是键值对")
    return fm


def _validate_skill_md(text: str) -> str:
    """校验 SKILL.md，返回 skill 名。"""
    fm = _parse_frontmatter(text)
    name = str(fm.get("name") or "").strip()
    if not name:
        raise SkillInstallError("SKILL.md frontmatter 缺少 'name' 字段")
    if not _VALID_NAME_RE.match(name) or len(name) > 64:
        raise SkillInstallError(f"skill name '{name}' 不合法（只允许字母、数字、下划线、连字符，最长 64 位）")
    if not str(fm.get("description") or "").strip():
        raise SkillInstallError("SKILL.md frontmatter 缺少 'description' 字段")
    return name


def _safe_relative(member: str) -> Path:
    path = Path(member)
    if path.is_absolute() or ".." in path.parts or "\\" in member or member.startswith("/"):
        raise SkillInstallError(f"ZIP 含非法路径: {member}")
    return path


def _read_zip(zip_path: Path) -> tuple[str, dict[Path, bytes]]:
    """校验并读出 ZIP 里的 skill 文件，返回 (skill 名, {相对路径: 内容})。不落盘。"""
    if zip_path.stat().st_size > MAX_ZIP_BYTES:
        raise SkillInstallError(f"压缩包超过 {MAX_ZIP_BYTES // (1024 * 1024)}MB 上限")
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise SkillInstallError("不是合法的 ZIP 文件")

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES * 2:  # 连垃圾文件一起算也多得离谱时，直接拒绝
            raise SkillInstallError(f"压缩包内文件过多（上限 {MAX_FILES}）")

        skill_mds = [i.filename for i in infos if Path(i.filename).name == "SKILL.md"]
        if not skill_mds:
            raise SkillInstallError("ZIP 中未找到 SKILL.md")
        skill_md = min(skill_mds, key=lambda n: len(Path(n).parts))
        parts = Path(skill_md).parts
        if len(parts) > 2:
            raise SkillInstallError("SKILL.md 必须在压缩包根目录，或唯一的顶层目录下")
        prefix = "" if len(parts) == 1 else parts[0] + "/"

        files: dict[Path, bytes] = {}
        total = 0
        for info in infos:
            member = info.filename
            if member.startswith("__MACOSX/") or Path(member).name in _IGNORED_NAMES \
                    or Path(member).name.startswith("._"):
                continue
            if prefix and not member.startswith(prefix):
                raise SkillInstallError(f"压缩包里有顶层目录之外的文件: {member}")
            rel = _safe_relative(member[len(prefix):])
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SkillInstallError(f"不允许符号链接: {member}")
            if info.flag_bits & 0x1:
                raise SkillInstallError("不支持加密的压缩包")
            if rel.suffix.lower() not in _ALLOWED_SUFFIXES:
                raise SkillInstallError(
                    f"不允许的文件类型: {member}（仅支持 {', '.join(sorted(_ALLOWED_SUFFIXES))}）")
            if len(files) >= MAX_FILES:
                raise SkillInstallError(f"压缩包内文件过多（上限 {MAX_FILES}）")
            # 自报的大小不可信：最多读到上限 +1 字节，按实际读到的算
            with zf.open(info) as f:
                data = f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise SkillInstallError(f"文件过大: {member}（单文件上限 {MAX_FILE_BYTES // (1024 * 1024)}MB）")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise SkillInstallError(f"解压后总大小超过 {MAX_TOTAL_BYTES // (1024 * 1024)}MB 上限")
            files[rel] = data

    try:
        name = _validate_skill_md(files[Path("SKILL.md")].decode("utf-8"))
    except UnicodeDecodeError:
        raise SkillInstallError("SKILL.md 必须是 UTF-8 编码")
    return name, files


# ── 安装（生成待审核版本）────────────────────────────────────────────────────

def _store_pending(name: str, files: dict[Path, bytes], skills_dir, user: str | None) -> int:
    with _lock:
        state = _load_state(skills_dir)
        entry = _entry(state, name)
        root = _versions_root(skills_dir) / name
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f"_incoming_{uuid.uuid4().hex[:8]}"
        try:
            for rel, data in files.items():
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
            version = _new_version(entry, PENDING, user,
                                   files=len(files), bytes=sum(len(d) for d in files.values()))
            os.rename(staging, root / str(version))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        _save_state(skills_dir, state)
        return version


def install_from_zip(zip_path, skills_dir, user: str = None) -> tuple[str, int, str]:
    """
    从 ZIP 上传 skill，生成一个待审核版本。

    ZIP 结构支持两种：skill_name/SKILL.md（带一个顶层目录），或 SKILL.md 直接在根目录。
    skill 名以 SKILL.md frontmatter 里的 name 为准。

    Returns: (skill_name, version, message)
    Raises: SkillInstallError
    """
    name, files = _read_zip(Path(zip_path))
    version = _store_pending(name, files, skills_dir, user)
    return name, version, f"skill '{name}' 已上传为 v{version}，待审核"


def install_from_skill_md(md_path, skills_dir, user: str = None) -> tuple[str, int, str]:
    """从单个 SKILL.md 上传无脚本的 skill，生成一个待审核版本。"""
    md_path = Path(md_path)
    if md_path.stat().st_size > MAX_FILE_BYTES:
        raise SkillInstallError("SKILL.md 过大")
    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SkillInstallError("SKILL.md 必须是 UTF-8 编码")
    name = _validate_skill_md(text)
    version = _store_pending(name, {Path("SKILL.md"): text.encode("utf-8")}, skills_dir, user)
    return name, version, f"skill '{name}' 已上传为 v{version}（无脚本），待审核"


# ── 发布 / 回滚 ──────────────────────────────────────────────────────────────

def _version_dir(skills_dir, name: str, version) -> Path:
    if not _VALID_NAME_RE.match(name or "") or not str(version).isdigit():
        raise SkillInstallError("skill 名或版本号不合法")
    return _versions_root(skills_dir) / name / str(int(version))


def publish(name: str, version: int, skills_dir, user: str = None, build_image=None) -> str:
    """
    把某个版本发布为生效版本（发布旧版本即回滚）。

    Args:
        build_image: 可选的 (skill_dir, skill_name) -> None。带 requirements.txt 的 skill 在这里
                     构建镜像；构建失败则不发布，版本保持原状态。
    """
    skills_dir = Path(skills_dir)
    with _lock:
        state = _load_state(skills_dir)
        entry = state.get(name)
        src = _version_dir(skills_dir, name, version)
        if not entry or str(int(version)) not in entry["versions"] or not src.is_dir():
            raise SkillInstallError(f"skill '{name}' 没有 v{version}")
        if entry["active"] == int(version):
            return f"skill '{name}' v{version} 已经是生效版本"

        if build_image is not None and (src / "requirements.txt").exists():
            build_image(src, name)

        dest = skills_dir / name
        root = _versions_root(skills_dir) / name
        outgoing = root / f"_outgoing_{uuid.uuid4().hex[:8]}"
        incoming = root / f"_incoming_{uuid.uuid4().hex[:8]}"
        shutil.copytree(src, incoming)
        try:
            if dest.exists():
                if entry["active"] is None:
                    # 发布前就在 skills/ 下的内容（如随代码仓库带的）：先存成一个版本，之后可以回滚到它
                    kept = _new_version(entry, RETIRED, None, note="发布前已存在的内容")
                    os.rename(dest, root / str(kept))
                else:
                    os.rename(dest, outgoing)
            os.rename(incoming, dest)
        except BaseException:
            shutil.rmtree(incoming, ignore_errors=True)
            raise
        shutil.rmtree(outgoing, ignore_errors=True)

        if entry["active"] is not None:
            entry["versions"][str(entry["active"])]["status"] = RETIRED
        meta = entry["versions"][str(int(version))]
        meta.update(status=PUBLISHED, published_by=user, published_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        entry["active"] = int(version)
        _save_state(skills_dir, state)
    return f"skill '{name}' v{version} 已发布，新建的对话开始生效"


def discard_version(name: str, version: int, skills_dir) -> str:
    """删除一个未生效的版本（驳回待审核的上传，或清理旧版本）。"""
    with _lock:
        state = _load_state(skills_dir)
        entry = state.get(name)
        vdir = _version_dir(skills_dir, name, version)
        if not entry or str(int(version)) not in entry["versions"]:
            raise SkillInstallError(f"skill '{name}' 没有 v{version}")
        if entry["active"] == int(version):
            raise SkillInstallError("不能删除生效中的版本；先发布其他版本，或卸载整个 skill")
        shutil.rmtree(vdir, ignore_errors=True)
        del entry["versions"][str(int(version))]
        if not entry["versions"] and not (Path(skills_dir) / name).exists():
            del state[name]
            shutil.rmtree(vdir.parent, ignore_errors=True)
        _save_state(skills_dir, state)
    return f"skill '{name}' v{version} 已删除"


def uninstall_skill(skill_name: str, skills_dir) -> tuple[str, str]:
    """卸载 skill：删除生效内容和它的所有版本。

    Returns: (skill_name, message)
    Raises: SkillInstallError if skill not found or name invalid
    """
    if not _VALID_NAME_RE.match(skill_name or ""):
        raise SkillInstallError(f"skill name '{skill_name}' 不合法")
    skills_dir = Path(skills_dir)
    with _lock:
        state = _load_state(skills_dir)
        dest = skills_dir / skill_name
        if not dest.is_dir() and skill_name not in state:
            raise SkillInstallError(f"skill '{skill_name}' 不存在")
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(_versions_root(skills_dir) / skill_name, ignore_errors=True)
        if state.pop(skill_name, None) is not None:
            _save_state(skills_dir, state)
    return skill_name, f"skill '{skill_name}' 已卸载"


# ── 查询 ─────────────────────────────────────────────────────────────────────

def list_versions(skills_dir) -> list[dict]:
    """所有 skill 的所有版本，待审核的排在最前。"""
    rows = []
    for name, entry in _load_state(skills_dir).items():
        for v, meta in entry["versions"].items():
            rows.append({"name": name, "version": int(v), "active": entry["active"] == int(v), **meta})
    order = {PENDING: 0, PUBLISHED: 1, RETIRED: 2}
    return sorted(rows, key=lambda r: (order.get(r["status"], 9), r["name"], -r["version"]))


def active_version(name: str, skills_dir) -> int | None:
    return _load_state(skills_dir).get(name, {}).get("active")


def review_summary(name: str, version: int, skills_dir) -> str:
    """给审核人看的版本概要（Markdown）：状态、联网声明、依赖、文件清单。"""
    state = _load_state(skills_dir)
    meta = state.get(name, {}).get("versions", {}).get(str(int(version)))
    vdir = _version_dir(skills_dir, name, version)
    if meta is None or not vdir.is_dir():
        raise SkillInstallError(f"skill '{name}' 没有 v{version}")

    fm = _parse_frontmatter((vdir / "SKILL.md").read_text(encoding="utf-8"))
    active = state[name]["active"]
    lines = [
        f"### {name} v{version}（{STATUS_LABELS.get(meta['status'], meta['status'])}）",
        "",
        f"- 上传：{meta.get('created_by') or '—'}，{meta.get('created_at', '—')}",
        f"- 当前生效版本：{'v%d' % active if active else '无（或为发布前已存在的内容）'}",
        f"- 描述：{str(fm.get('description', '')).strip()}",
        f"- **联网**：{'是（经出口代理访问公网）' if fm.get('network') is True else '否'}",
    ]
    req = vdir / "requirements.txt"
    if req.exists():
        deps = [l.strip() for l in req.read_text(encoding="utf-8", errors="replace").splitlines()
                if l.strip() and not l.strip().startswith("#")]
        lines.append(f"- **依赖**（发布时构建镜像安装）：{', '.join(f'`{d}`' for d in deps[:30]) or '空'}")
    else:
        lines.append("- 依赖：无")
    lines += ["", "| 文件 | 大小 |", "|---|---|"]
    for p in sorted(vdir.rglob("*")):
        if p.is_file():
            lines.append(f"| `{p.relative_to(vdir).as_posix()}` | {p.stat().st_size} B |")
    lines += ["", "发布前请打开 `skills/.versions/%s/%d/` 检查脚本内容。" % (name, int(version))]
    return "\n".join(lines)
