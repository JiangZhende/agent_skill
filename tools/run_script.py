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
1. 安全校验：skill 存在、script 路径在 skill 目录内
2. 解析 args 列表中的 @input:/@output:/@workspace:/@result_id 引用
3. subprocess 执行，stdout/stderr/exit_code 返回给模型
4. 缓存 stdout 为 result_id，供后续步骤用 @<result_id> 引用
"""
import re
import subprocess
import sys
import threading
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from smolagents import tool

# 模块级：跨所有 agent 实例共享，防止并发重复构建同一镜像
_built_images: set = set()
_build_lock = threading.Lock()

def make_run_script_tool(registry, skills_dir, workspace=None, use_docker=False, docker_image="agent-sandbox:latest"):
    """
    工厂：返回单个 execute_skill_script tool + cleanup 函数。

    Args:
        registry: SkillRegistry
        skills_dir: skills 根目录
        workspace: 可选 Workspace 实例（用于 @ 引用解析）
        use_docker: True 则在 Docker 容器内执行脚本，False 用本地 subprocess（默认）
        docker_image: Docker 镜像名，use_docker=True 时生效

    Returns: (execute_skill_script_tool, cleanup_fn)
    """
    skills_root = Path(skills_dir).resolve()
    shared_cache = {}
    shared_tempdir = Path(tempfile.mkdtemp(prefix="agent_run_"))
    def _ensure_image():
        """镜像不存在时自动 build，模块级锁防止多用户并发重复构建。"""
        if docker_image in _built_images:
            return
        with _build_lock:
            if docker_image in _built_images:  # 双重检查
                return
            check = subprocess.run(
                ["docker", "image", "inspect", docker_image],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if check.returncode != 0:
                dockerfile_dir = skills_root.parent
                print(f"[sandbox] 镜像 {docker_image} 不存在，正在自动构建...", flush=True)
                build = subprocess.run(
                    ["docker", "build", "-t", docker_image, str(dockerfile_dir)],
                    check=False,
                )
                if build.returncode != 0:
                    raise RuntimeError(
                        f"镜像构建失败（exit {build.returncode}）。"
                        f"请手动运行: docker build -t {docker_image} {dockerfile_dir}"
                    )
                print(f"[sandbox] 镜像构建完成：{docker_image}", flush=True)
            _built_images.add(docker_image)

    def _docker_run(resolved_path, argv, stdin_bytes, timeout):
        """在 Docker 容器内执行脚本，挂载 skills 目录和 workspace。"""
        _ensure_image()
        project_root = skills_root.parent
        script_in_container = f"/app/{resolved_path.relative_to(project_root)}"

        cmd = [
            "docker", "run", "--rm",
            "--memory=512m",
            "--cpus=0.5",
            "--pids-limit=100",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--workdir=/app",
            "-v", f"{project_root}:/app:ro",
        ]

        if stdin_bytes:
            cmd.append("-i")

        if workspace is not None:
            cmd += [
                "-v", f"{workspace.root}:/agent_workspace:rw",
                "-e", "AGENT_INPUTS_DIR=/agent_workspace/inputs",
                "-e", "AGENT_WORKSPACE_DIR=/agent_workspace/workspace",
                "-e", "AGENT_OUTPUTS_DIR=/agent_workspace/outputs",
            ]

        # 如果 skill 有 requirements.txt，先 pip install 再执行脚本
        skill_name = resolved_path.relative_to(skills_root).parts[0]
        req_file = skills_root / skill_name / "requirements.txt"
        if req_file.exists():
            req_in_container = f"/app/{req_file.relative_to(project_root)}"
            entrypoint = (
                f"pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple "
                f"-r {req_in_container} && python {script_in_container} "
                + " ".join(f'"{a}"' for a in argv)
            )
            cmd += [docker_image, "sh", "-c", entrypoint]
            argv = []  # 参数已拼入 entrypoint
        else:
            cmd += [docker_image, "python", script_in_container]

        cmd += argv

        return subprocess.run(
            cmd,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

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
            return str(shared_cache[rid]), None

        if value.startswith("@") and workspace is not None:
            host_path, err = workspace.resolve_ref(value)
            if err or not use_docker:
                return host_path, err
            # Docker 模式：把宿主机路径转换为容器内挂载路径
            host_path = Path(host_path)
            for host_dir, container_dir in (
                (workspace.inputs_dir,   "/agent_workspace/inputs"),
                (workspace.workspace_dir, "/agent_workspace/workspace"),
                (workspace.outputs_dir,  "/agent_workspace/outputs"),
            ):
                try:
                    rel = host_path.relative_to(host_dir)
                    return f"{container_dir}/{rel}", None
                except ValueError:
                    continue
            return str(host_path), None  # 无法映射，原样返回

        return value, None

    def _cache_stdout(stdout):
        rid = f"r_{uuid.uuid4().hex[:8]}"
        p = shared_tempdir / f"{rid}.json"
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
            timeout: 超时秒数，默认 60
        """
        # 1. skill 必须存在
        if skill_name not in registry.skills:
            available = ", ".join(registry.skills.keys())
            return f"[错误] skill '{skill_name}' 不存在。可用: {available}"

        # 2. 脚本路径安全校验
        resolved_path, err = _resolve_script(skill_name, script_path)
        if err:
            return f"[错误] {err}"

        # 3. 解析 args 中的 @ 引用
        argv = []
        ref_errors = []
        for item in (args or []):
            resolved, ref_err = _resolve_ref(str(item))
            if ref_err:
                ref_errors.append(f"{item!r} → {ref_err}")
                continue
            argv.append(resolved if resolved is not None else str(item))

        if ref_errors:
            return "[参数引用失败]\n" + "\n".join(ref_errors)

        # 4. 注入环境变量（仅 subprocess 路径使用）
        env = None
        if not use_docker and workspace is not None:
            import os
            env = os.environ.copy()
            env["AGENT_INPUTS_DIR"] = str(workspace.inputs_dir)
            env["AGENT_WORKSPACE_DIR"] = str(workspace.workspace_dir)
            env["AGENT_OUTPUTS_DIR"] = str(workspace.outputs_dir)

        # 5. 执行
        stdin_bytes = input.encode("utf-8") if input else None
        try:
            if use_docker:
                result = _docker_run(resolved_path, argv, stdin_bytes, timeout)
            else:
                cmd = [sys.executable, str(resolved_path)] + argv
                result = subprocess.run(
                    cmd,
                    input=stdin_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    cwd=str(skills_root.parent),
                    env=env,
                )
        except subprocess.TimeoutExpired:
            return f"[超时] 脚本执行超过 {timeout} 秒"
        except Exception as e:
            return f"[执行错误] {type(e).__name__}: {e}"

        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        # 6. 配额检查
        if workspace is not None:
            q_err = workspace.check_quota()
            if q_err:
                return f"[配额超限] {q_err}\n**Exit Code**: {result.returncode}"

        # 7. 缓存 stdout
        rid = None
        if result.returncode == 0 and stdout_text.strip():
            if len(stdout_text) <= 5 * 1024 * 1024:
                rid = _cache_stdout(stdout_text)

        # 8. 组装返回（对齐 agent_skills ToolResult 格式）
        parts = [f"=== Script Execution: {skill_name}/{script_path} ===\n"]
        if argv:
            parts.append(f"**Arguments**: {argv}")
        parts.append(f"**Exit Code**: {result.returncode}")
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
        for p in shared_cache.values():
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        shared_cache.clear()
        try:
            import shutil
            shutil.rmtree(shared_tempdir, ignore_errors=True)
        except Exception:
            pass

    return execute_skill_script, cleanup
