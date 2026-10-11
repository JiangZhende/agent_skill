"""
框架内建：execute_skill_script —— 唯一的脚本执行引擎。

与上游 agent_skills 库的 ExecuteSkillScriptTool 接口对齐：
  execute_skill_script(
    skill_name="xxx",
    script_path="scripts/yyy.py",
    args=["--region", "华南", "--quarter", "2025Q3"],
    input="..."   # stdin 数据，适合传上一步的 JSON 输出
  )

框架在内部:
1. 安全校验：skill 存在、script 路径在 skill 目录内、参数不带宿主机路径
2. 解析 args 列表中的 @input:/@output:/@workspace:/@result_id 引用
3. 交给 SandboxExecutor 执行（见 core/executor.py），stdout/stderr/exit_code 返回给模型
4. 缓存 stdout 为 result_id，供后续步骤用 @<result_id> 引用
"""
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import List

from smolagents import tool

from core.executor import LocalExecutor, MAX_OUTPUT_BYTES

# 超时由服务端封顶，模型传入的值只能在此范围内
MAX_TIMEOUT = int(os.getenv("SANDBOX_MAX_TIMEOUT", "300"))


def _unsafe_path_arg(value: str) -> bool:
    """参数是否像一个指向沙箱外的路径（绝对路径、~ 或含 ..）。文件只能通过 @ 引用传入。"""
    candidates = [value]
    if value.startswith("-") and "=" in value:  # --flag=value 形式
        candidates.append(value.split("=", 1)[1])
    for v in candidates:
        if v.startswith(("http://", "https://")):
            continue
        if v.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", v):
            return True
        if ".." in PurePosixPath(v.replace("\\", "/")).parts:
            return True
    return False


def make_run_script_tool(registry, skills_dir, workspace=None, executor=None):
    """
    工厂：返回单个 execute_skill_script tool + cleanup 函数。

    Args:
        registry: SkillRegistry
        skills_dir: skills 根目录
        workspace: 可选 Workspace 实例（用于 @ 引用解析）
        executor: SandboxExecutor；不传则用 LocalExecutor（本机子进程，仅开发用）

    Returns: (execute_skill_script_tool, cleanup_fn)
    """
    skills_root = Path(skills_dir).resolve()
    if executor is None:
        executor = LocalExecutor(workspace=workspace)
    shared_cache = {}

    def _resolve_script(skill_name, script_path):
        """安全解析脚本路径，必须在 skills/<skill_name>/ 子目录下"""
        skill_root = skills_root / skill_name
        candidate = skill_root / script_path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(skill_root)
        except (ValueError, OSError):
            return None, "脚本路径越界"
        if not resolved.exists():
            return None, f"脚本文件不存在: {script_path}"
        if resolved.suffix != ".py":
            return None, f"仅支持 .py 脚本: {script_path}"
        return resolved, None

    def _resolve_ref(value):
        """解析 @result_id / @input:xxx / @workspace:xxx / @output:xxx"""
        if not isinstance(value, str):
            return str(value), None

        m = re.match(r"^@(r_[a-z0-9]+)$", value)
        if m:
            rid = m.group(1)
            if rid not in shared_cache:
                return None, f"result_id 不存在: {rid}"
            return executor.path_for(shared_cache[rid]), None

        if value.startswith("@") and workspace is not None:
            host_path, err = workspace.resolve_ref(value)
            if err or host_path == value:  # 出错，或不是已知引用（原样返回）
                return host_path, err
            return executor.path_for(host_path), None

        return value, None

    def _cache_stdout(stdout):
        rid = f"r_{uuid.uuid4().hex[:8]}"
        p = executor.results_dir / f"{rid}.json"
        p.write_text(stdout, encoding="utf-8")
        shared_cache[rid] = p
        return rid

    @tool
    def execute_skill_script(
        skill_name: str,
        script_path: str,
        args: List[str] = None,
        input: str = None,
        timeout: int = 60,
    ) -> str:
        """Execute a script bundled with a skill in a sandboxed environment.

        ## When to Use
        After load_skill returns and shows the skill has scripts to run.
        Only scripts from loaded skills can be executed.

        ## Parameters
        - args: CLI argument array, e.g. ["--region", "华南", "--quarter", "2025Q3"]
                Supports @ references in values:
                  @<result_id>       — stdout of a previous execute_skill_script call
                  @input:<filename>  — user-uploaded file
                  @workspace:<name>  — intermediate workspace file
                  @output:<name>     — declare an output file to persist
        - input: Data to pass via stdin (use for in-memory data, e.g. JSON from a previous step).
                 Equivalent to: echo 'data' | python script.py

        Args:
            skill_name: skill 名称，如 "sales_report"
            script_path: 脚本相对路径，如 "scripts/fetch_sales.py"
            args: CLI 参数数组，如 ["--region", "华南", "--quarter", "2025Q3"]
            input: 通过 stdin 管道传给脚本的原始字符串
            timeout: 超时秒数，默认 60，上限由服务端决定
        """
        # 1. skill 必须存在
        if skill_name not in registry.skills:
            available = ", ".join(registry.skills.keys())
            return f"[错误] skill '{skill_name}' 不存在。可用: {available}"

        # 2. 脚本路径安全校验
        resolved_path, err = _resolve_script(skill_name, script_path)
        if err:
            return f"[错误] {err}"

        # 3. 解析 args 中的 @ 引用；其余参数不允许带沙箱外的路径
        argv = []
        ref_errors = []
        for item in (args or []):
            item = str(item)
            if not item.startswith("@") and _unsafe_path_arg(item):
                ref_errors.append(
                    f"{item!r} → 不允许传入绝对路径或含 '..' 的路径，"
                    f"文件请用 @input:/@workspace:/@output: 引用，skill 内资源用相对路径"
                )
                continue
            resolved, ref_err = _resolve_ref(item)
            if ref_err:
                ref_errors.append(f"{item!r} → {ref_err}")
                continue
            argv.append(resolved if resolved is not None else item)

        if ref_errors:
            return "[参数引用失败]\n" + "\n".join(ref_errors)

        # 4. 配额已超限的会话不再执行
        if workspace is not None:
            q_err = workspace.check_quota()
            if q_err:
                return f"[配额超限] {q_err}"

        # 5. 执行
        try:
            timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout = 60
        stdin_bytes = input.encode("utf-8") if input else None
        script_rel = resolved_path.relative_to((skills_root / skill_name).resolve()).as_posix()
        try:
            result = executor.run(registry.skills[skill_name], script_rel, argv, stdin_bytes, timeout)
        except Exception as e:
            return f"[执行错误] {type(e).__name__}: {e}"

        if result.timed_out:
            return f"[超时] 脚本执行超过 {timeout} 秒，已终止"
        if result.output_truncated:
            return (
                f"[输出超限] 脚本输出超过 {MAX_OUTPUT_BYTES // (1024 * 1024)}MB，已终止。"
                f"大量数据请写入 @workspace: 文件而不是打印到 stdout"
            )

        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        # 6. 配额检查
        if workspace is not None:
            q_err = workspace.check_quota()
            if q_err:
                executor.close()  # 停掉本会话的沙箱，后续执行会在第 4 步被拒绝
                return f"[配额超限] {q_err}\n**Exit Code**: {result.exit_code}"

        # 7. 缓存 stdout
        rid = None
        if result.exit_code == 0 and stdout_text.strip():
            if len(stdout_text) <= 5 * 1024 * 1024:
                rid = _cache_stdout(stdout_text)

        # 8. 组装返回（对齐 agent_skills ToolResult 格式）
        parts = [f"=== Script Execution: {skill_name}/{script_path} ===\n"]
        if args:
            parts.append(f"**Arguments**: {[str(a) for a in args]}")
        parts.append(f"**Exit Code**: {result.exit_code}")
        if rid:
            parts.append(
                f"**Result ID**: {rid}"
                f"（可用 \"@{rid}\" 在下一步 args 中引用，"
                f"或直接把 stdout 内容传给下一步的 input）"
            )

        if stdout_text:
            display = stdout_text.rstrip()
            if len(display) > 10000:
                display = display[:10000] + f"\n...（已截断，完整 {len(stdout_text)} 字符）"
            parts.append("\n## Standard Output\n```")
            parts.append(display)
            parts.append("```")

        if stderr_text:
            display = stderr_text.rstrip()
            if len(display) > 2000:
                display = display[:2000] + "...（已截断）"
            parts.append("\n## Standard Error\n```")
            parts.append(display)
            parts.append("```")

        return "\n".join(parts)

    def cleanup():
        shared_cache.clear()
        executor.close()

    return execute_skill_script, cleanup
