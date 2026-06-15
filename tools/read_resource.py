"""
框架内建：read_resource —— 读取 skill 目录下的资源文件。

与 load_skill 不同，load_skill 读 SKILL.md 获取 skill 概述；
read_resource 读取 skill 内任意被明确引用的资源文件（schema、模板、字典等）。
"""
from pathlib import Path

from smolagents import tool

_ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml"}
_MAX_BYTES = 100 * 1024  # 100KB


def make_read_resource_tool(registry, skills_dir):
    skills_root = Path(skills_dir).resolve()

    @tool
    def read_resource(skill_name: str, path: str) -> str:
        """读取 skill 目录下的指定资源文件（schema、模板、字典表等）。

        当 SKILL.md 的执行流程中提到需要参考某个资源文件时调用此工具。
        只能读取文本类资源文件，不能读取脚本（.py）。

        Args:
            skill_name: skill 名称，如 "csv_to_report"
            path: 相对于 skill 目录的文件路径，如 "resources/schema.json"
        """
        # 1. skill 必须存在
        if skill_name not in registry.skills:
            available = ", ".join(registry.skills.keys())
            return f"[错误] skill '{skill_name}' 不存在。可用: {available}"

        # 2. 路径安全校验
        skill_root = skills_root / skill_name
        candidate = skill_root / path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(skill_root)
        except (ValueError, OSError):
            return f"[错误] 路径越界，只能读取 skill '{skill_name}' 目录内的文件"

        # 3. 文件必须存在
        if not resolved.exists() or not resolved.is_file():
            return f"[错误] 文件不存在: {path}"

        # 4. 只允许文本类文件
        if resolved.suffix.lower() not in _ALLOWED_SUFFIXES:
            return (
                f"[错误] 不允许读取 '{resolved.suffix}' 类型的文件。"
                f"允许的类型: {', '.join(sorted(_ALLOWED_SUFFIXES))}"
            )

        # 5. 读取内容，超出 100KB 截断
        raw = resolved.read_bytes()
        truncated = len(raw) > _MAX_BYTES
        content = raw[:_MAX_BYTES].decode("utf-8", errors="replace")

        result = f"=== {skill_name}/{path} ===\n\n{content}"
        if truncated:
            result += f"\n\n...（文件过大，已截断，完整大小 {len(raw)} 字节）"
        return result

    return read_resource
