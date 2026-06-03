"""
Agent 运行时工作区管理。

每次 agent.run() 创建一个独立工作区：
  /tmp/agent_run_<uuid>/
    inputs/     用户上传的输入（只读视角）
    workspace/  script 间中间文件
    outputs/    最终给用户的产物

设计要点：
- 路径语义化：模型只看到 @input:xxx / @output:xxx / @workspace:xxx，
  不接触实际文件系统路径，降低出错率，避免路径穿越
- 大小可控：单文件、单目录、总配额可设置
- 生命周期清晰：run 结束统一清理（outputs 可选保留或上传）
"""
import shutil
import tempfile
import uuid
from pathlib import Path


# 默认配额
DEFAULT_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB 单文件
DEFAULT_MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024  # 2GB 总量


class Workspace:
    """单次 agent.run() 的工作区"""

    def __init__(
        self,
        run_id: str = None,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    ):
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        self.root = Path(tempfile.gettempdir()) / f"agent_{self.run_id}"
        self.inputs_dir = self.root / "inputs"
        self.workspace_dir = self.root / "workspace"
        self.outputs_dir = self.root / "outputs"

        for d in (self.inputs_dir, self.workspace_dir, self.outputs_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.max_file_size = max_file_size
        self.max_total_size = max_total_size

    def add_input(self, src_path: str | Path, dest_name: str = None) -> str:
        """把用户上传的文件拷贝到 inputs/，返回文件名（不含目录）"""
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"输入文件不存在: {src}")
        if src.stat().st_size > self.max_file_size:
            raise ValueError(f"文件超过单文件大小限制: {src.name}")

        dest_name = dest_name or src.name
        dest = self.inputs_dir / dest_name
        shutil.copy2(src, dest)
        return dest_name

    def list_inputs(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "size_human": _format_size(p.stat().st_size),
            }
            for p in sorted(self.inputs_dir.iterdir())
            if p.is_file()
        ]

    def list_outputs(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "size_human": _format_size(p.stat().st_size),
            }
            for p in sorted(self.outputs_dir.iterdir())
            if p.is_file()
        ]

    def resolve_ref(self, value: str) -> tuple[str | None, str | None]:
        """
        解析路径引用：
        - @input:name    -> inputs/name 的绝对路径
        - @workspace:name -> workspace/name 的绝对路径
        - @output:name   -> outputs/name 的绝对路径（用于声明输出位置）
        返回: (resolved_path, error_message)
        """
        if not isinstance(value, str) or not value.startswith("@"):
            return value, None

        # @input:xxx / @workspace:xxx / @output:xxx
        if ":" not in value:
            return value, None  # 不是带空间的引用，可能是 @result_id

        kind, name = value[1:].split(":", 1)
        kind = kind.strip()
        name = name.strip()

        # 防止路径穿越
        if "/" in name or "\\" in name or ".." in name:
            return None, f"引用名包含非法字符: {value}"

        if kind == "input":
            path = self.inputs_dir / name
            if not path.exists():
                available = [p.name for p in self.inputs_dir.iterdir()]
                return None, f"输入文件不存在: {name}。可用: {available}"
            return str(path), None

        if kind == "workspace":
            # workspace 文件可能还没创建（是即将写入的目标），不要求存在
            path = self.workspace_dir / name
            return str(path), None

        if kind == "output":
            path = self.outputs_dir / name
            return str(path), None

        return value, None  # 未知 kind，保持原样

    def total_size(self) -> int:
        total = 0
        for d in (self.inputs_dir, self.workspace_dir, self.outputs_dir):
            for p in d.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        return total

    def check_quota(self) -> str | None:
        """检查总用量是否超限，返回错误信息或 None"""
        size = self.total_size()
        if size > self.max_total_size:
            return f"工作区总用量超限: {_format_size(size)} > {_format_size(self.max_total_size)}"
        return None

    def cleanup(self, keep_outputs: bool = False, outputs_persist_to: Path = None):
        """清理工作区。

        Args:
            keep_outputs: True 则保留 outputs 目录（移动到 outputs_persist_to）
            outputs_persist_to: 保留 outputs 时的目标目录
        """
        if keep_outputs and outputs_persist_to:
            outputs_persist_to = Path(outputs_persist_to)
            outputs_persist_to.mkdir(parents=True, exist_ok=True)
            for p in self.outputs_dir.iterdir():
                if p.is_file():
                    shutil.copy2(p, outputs_persist_to / p.name)

        try:
            shutil.rmtree(self.root)
        except Exception:
            pass


def _format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"
