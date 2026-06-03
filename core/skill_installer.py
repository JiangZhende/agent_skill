"""
Skill 安装器：支持从 ZIP 或单个 SKILL.md 安装 skill。

安全约束：
- ZIP 解压时拒绝路径穿越（..）
- 只允许 .py / .md / .sh / .txt / .yaml / .yml 文件
- skill name 必须通过 SkillRegistry 的校验（含 _VALID_NAME_RE）
"""
import re
import shutil
import zipfile
from pathlib import Path

from core.skill_loader import Skill, _VALID_NAME_RE

_ALLOWED_SUFFIXES = {".py", ".md", ".sh", ".txt", ".yaml", ".yml"}


def _safe_extract(zf: zipfile.ZipFile, dest: Path):
    """解压 ZIP，拒绝路径穿越和不允许的文件类型。"""
    for member in zf.namelist():
        member_path = Path(member)
        # 拒绝绝对路径和 ..
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"ZIP 包含非法路径: {member}")
        if member.endswith("/"):
            continue
        suffix = Path(member).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError(f"不允许的文件类型: {member}（仅支持 {_ALLOWED_SUFFIXES}）")
        out = dest / member_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(member))


def install_from_zip(zip_path: str | Path, skills_dir: Path) -> tuple[str, str]:
    """
    从 ZIP 安装 skill。

    ZIP 结构支持两种格式：
      1. skill_name/SKILL.md  （带顶层目录）
      2. SKILL.md             （不带顶层目录，用 ZIP 文件名作为 skill 目录名）

    Returns: (skill_name, message)
    Raises: ValueError on validation failure
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

        # 判断是否有顶层目录
        skill_md_entries = [n for n in names if Path(n).name == "SKILL.md"]
        if not skill_md_entries:
            raise ValueError("ZIP 中未找到 SKILL.md")

        skill_md_entry = skill_md_entries[0]
        parts = Path(skill_md_entry).parts

        if len(parts) == 1:
            # 无顶层目录，用 ZIP 文件名（去掉 .zip）
            skill_dir_name = zip_path.stem
            prefix = ""
        else:
            # 有顶层目录
            skill_dir_name = parts[0]
            prefix = skill_dir_name + "/"

        if not _VALID_NAME_RE.match(skill_dir_name):
            raise ValueError(f"skill 目录名 '{skill_dir_name}' 不合法（只允许字母、数字、连字符）")

        # 读取并校验 SKILL.md
        skill_md_text = zf.read(skill_md_entry).decode("utf-8")
        _validate_skill_md(skill_md_text, skill_dir_name)

        dest = skills_dir / skill_dir_name
        if dest.exists():
            shutil.rmtree(dest)

        with zipfile.ZipFile(zip_path, "r") as zf2:
            _safe_extract_filtered(zf2, prefix, dest)

    return skill_dir_name, f"skill '{skill_dir_name}' 安装成功"


def install_from_skill_md(md_path: str | Path, skills_dir: Path) -> tuple[str, str]:
    """
    从单个 SKILL.md 安装无脚本的 skill。

    Returns: (skill_name, message)
    """
    md_path = Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    skill_name = _validate_skill_md(text, hint=md_path.stem)

    dest = skills_dir / skill_name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")

    return skill_name, f"skill '{skill_name}' 安装成功（无脚本）"


def _validate_skill_md(text: str, hint: str = "") -> str:
    """校验 SKILL.md 格式，返回 skill name。"""
    import yaml
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        raise ValueError("SKILL.md 缺少 frontmatter（--- ... ---）")
    fm = yaml.safe_load(m.group(1)) or {}
    name = fm.get("name", "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter 缺少 'name' 字段")
    if not _VALID_NAME_RE.match(name):
        raise ValueError(f"skill name '{name}' 不合法（只允许字母、数字、连字符）")
    if not fm.get("description", "").strip():
        raise ValueError("SKILL.md frontmatter 缺少 'description' 字段")
    return name


def _safe_extract_filtered(zf: zipfile.ZipFile, prefix: str, dest: Path):
    """解压 ZIP 中属于 prefix 的文件到 dest，去掉 prefix 前缀。"""
    for member in zf.namelist():
        if member.endswith("/"):
            continue
        if prefix and not member.startswith(prefix):
            continue

        rel = member[len(prefix):]  # 去掉顶层目录前缀
        if not rel:
            continue

        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError(f"ZIP 含非法路径: {member}")
        if rel_path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError(f"不允许的文件类型: {member}")

        out = dest / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(member))


