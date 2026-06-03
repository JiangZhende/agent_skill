"""框架内建：让 agent 能查询 workspace 中的文件。"""
from smolagents import tool


def make_workspace_tools(workspace):
    """工厂：生成 list_inputs / list_outputs 工具"""

    @tool
    def list_inputs() -> str:
        """列出用户上传的所有输入文件，包含文件名和大小。
        当任务涉及用户提供的文件，先用此工具确认有哪些可用输入。
        """
        files = workspace.list_inputs()
        if not files:
            return "（用户未上传任何输入文件）"
        lines = ["可用输入文件（使用 @input:<filename> 引用）："]
        for f in files:
            lines.append(f"  - {f['name']} ({f['size_human']})")
        return "\n".join(lines)

    @tool
    def list_outputs() -> str:
        """列出 outputs 目录中已生成的文件。
        在最终回答用户前，可以用此工具确认生成的产物文件。
        """
        files = workspace.list_outputs()
        if not files:
            return "（还没有任何输出文件）"
        lines = ["已生成的输出文件："]
        for f in files:
            lines.append(f"  - {f['name']} ({f['size_human']}) at {f['path']}")
        return "\n".join(lines)

    return [list_inputs, list_outputs]
