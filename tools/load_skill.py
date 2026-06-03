from smolagents import tool


def make_load_skill_tool(registry):

    @tool
    def load_skill(skill_name: str) -> str:
        """加载指定 skill 的完整说明。

        返回内容包含：适用场景、脚本参数、执行流程（含具体调用示例）。
        阅读其中的 "执行流程" 部分，按步骤调用 execute_skill_script，
        将示例中的占位值替换为用户请求中的实际值。

        Args:
            skill_name: skill 名称，从系统提示的 skills 列表中选择
        """
        if skill_name not in registry.skills:
            available = ", ".join(registry.skills.keys())
            return f"Skill '{skill_name}' 不存在。可用 skills: {available}"

        return registry.load(skill_name)

    return load_skill
