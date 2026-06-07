"""
Agent 主封装。

设计理念：
- 唯一的脚本执行入口是 run_script（不为每个 script 注册独立 tool）
- System prompt 完全自定义，不依赖 smolagents 默认模板
- load_skill 返回值中包含该 skill 所有脚本的 run_script 调用样例
- 业务方通过 extra_tools 注入自己的工具
"""
from pathlib import Path
from smolagents import ToolCallingAgent, OpenAIServerModel

from core.skill_loader import SkillRegistry
from core.workspace import Workspace
from tools.run_script import make_run_script_tool
from tools.load_skill import make_load_skill_tool
from tools.workspace_tool import make_workspace_tools


# 完全自定义的 Jinja2 系统提示模板，对齐上游 agent_skills 风格：
# base prompt 极简，不在 system prompt 中解释工具用法，只保留 skills 协议。
_SYSTEM_PROMPT_TEMPLATE = """\
你是一个能够使用工具完成用户任务的助手。

## 输出格式（必须严格遵守）

你每次必须输出且只能输出一个 JSON 格式的工具调用，格式如下：
```json
{
  "name": "工具名称",
  "arguments": {"参数名": "参数值"}
}
```

**禁止**直接输出自然语言作为回答。所有回答都必须通过调用 `final_answer` 工具来返回：
```json
{
  "name": "final_answer",
  "arguments": {"answer": "你的最终回答内容"}
}
```
`final_answer` 是结束任务的唯一方式，不调用它任务将无法结束。

## 可用工具

{%- for tool in tools.values() %}
- **{{ tool.name }}**: {{ tool.description.split('\n')[0] }}
{%- endfor %}

SKILL_SECTION_PLACEHOLDER
"""

# 对齐上游 agent_skills/skills/prompt.py 的 format_skills_metadata() 输出
_SKILL_SECTION_TEMPLATE = """\
### Available Skills (IMPORTANT - READ CAREFULLY)

**You MUST actively consider using these skills for EVERY user request.**

#### Skill Matching Protocol (MANDATORY)

Before responding to ANY user query, follow this checklist:

1. **SCAN**: Read each skill's description and trigger conditions below
2. **MATCH**: Check if the user's intent matches ANY skill's triggers (keywords, scenarios, or task types)
3. **LOAD**: If a match is found, call `load_skill(skill_name="...")` BEFORE generating your response
4. **APPLY**: Follow the skill's instructions step by step, calling `execute_skill_script` with real values

**⚠️ CRITICAL**: Skill usage is MANDATORY when applicable. Do NOT skip skills to save time or tokens.

#### Available Skills

{skill_briefs}

#### Tool Reference

- `load_skill(skill_name)`: Load full skill instructions (MUST call before using a skill)
- `execute_skill_script(skill_name, script_path, args, input)`: Run utility scripts bundled with a skill
  - `args`: CLI argument **array**, e.g. `["--region", "华南", "--quarter", "2025Q3"]`
  - `input`: Pass data directly via stdin (use this when you have data in memory, e.g. JSON string from a previous step)
  - **Note**: `args` MUST be a JSON array `[...]`, never an object `{{...}}`
"""


def _build_skill_section(registry: SkillRegistry) -> str:
    skill_briefs = registry.list_briefs()
    return _SKILL_SECTION_TEMPLATE.format(skill_briefs=skill_briefs)


def build_agent(
    model_id: str = "gpt-4o-mini",
    api_base: str | None = None,
    api_key: str | None = None,
    skills_dir: Path = Path("skills"),
    extra_tools: list = None,
    max_steps: int = 10,
    upload_files: list = None,
    outputs_persist_to: Path = None,
    use_docker: bool = False,
    docker_image: str = "agent-sandbox:latest",
):
    """构建一个 skills-based ToolCallingAgent。

    Args:
        upload_files: 用户上传的文件列表
        outputs_persist_to: 持久化 outputs 的目标目录
    """
    registry = SkillRegistry(skills_dir)
    workspace = Workspace()

    if upload_files:
        for f in upload_files:
            try:
                name = workspace.add_input(f)
                print(f"[upload] {f} -> @input:{name}")
            except Exception as e:
                print(f"[upload error] {f}: {e}")

    load_skill_tool = make_load_skill_tool(registry)
    run_script_tool, run_script_cleanup = make_run_script_tool(
        registry, skills_dir, workspace=workspace,
        use_docker=use_docker, docker_image=docker_image,
    )
    workspace_tools = make_workspace_tools(workspace)

    builtin_tools = [load_skill_tool, run_script_tool] + workspace_tools

    model = OpenAIServerModel(model_id=model_id, api_base=api_base, api_key=api_key)
    agent = ToolCallingAgent(
        tools=builtin_tools + (extra_tools or []),
        model=model,
        max_steps=max_steps,
    )

    # 完全替换 smolagents 默认模板，使用纯中文自定义模板
    skill_section = _build_skill_section(registry)
    custom_prompt = _SYSTEM_PROMPT_TEMPLATE.replace("SKILL_SECTION_PLACEHOLDER", skill_section)
    agent.prompt_templates["system_prompt"] = custom_prompt

    def cleanup_all():
        run_script_cleanup()
        workspace.cleanup(
            keep_outputs=bool(outputs_persist_to),
            outputs_persist_to=outputs_persist_to,
        )

    agent.cleanup = cleanup_all
    agent.workspace = workspace

    # get_facts 是此 fork 特有功能：每次 tool 返回后额外调用模型提炼 observation。
    # 问题：(1) 每步增加一次模型调用，延迟高；(2) 在强制 JSON tool call 的 system prompt 下
    # 模型返回 content=None，导致 observations 显示为空。
    # 直接 bypass：原样返回 observation 内容。
    if hasattr(agent, "get_facts"):
        import types
        # 不调用原始 get_facts（它把 content 提前提取成字符串，丢失了 tool_calls）
        # 直接调模型拿完整 ChatMessage，兼容 content=None 而答案在 tool_calls 里的情况
        def _get_facts_fixed(*args):
            return args[-1]
        agent.get_facts = types.MethodType(_get_facts_fixed, agent)

    # smolagents 本地 fork 的 get_facts 把 input_messages 构建成 dict 而非 list，
    # 导致 get_clean_message_list 迭代时报 TypeError: string indices must be integers。
    # 用 proxy 包装 agent.model，把 dict 自动升级为 list，不修改依赖源码。
    if hasattr(agent, "get_facts"):
        class _ModelProxy:
            def __init__(self, m):
                self._m = m
            def __call__(self, messages, **kw):
                if isinstance(messages, dict):
                    messages = [messages]
                return self._m(messages, **kw)
            def __getattr__(self, name):
                return getattr(self._m, name)
        agent.model = _ModelProxy(agent.model)

    # 当 postprocess_message 解析 JSON tool call 失败时：
    # 1. 打印模型原始输出方便排查
    # 2. 若模型直接返回自然语言（未调用任何 tool），降级为 final_answer 而非崩溃
    _inner_model = agent.model._m if hasattr(agent.model, "_m") else agent.model
    _orig_postprocess = _inner_model.postprocess_message
    def _postprocess_with_log(message, tools_to_call_from=None):
        try:
            return _orig_postprocess(message, tools_to_call_from)
        except Exception as e:
            import sys, uuid
            print(
                f"\n[model parse error] {type(e).__name__}: {e}\n"
                f"[model raw output]:\n{message.content}\n",
                file=sys.stderr,
            )
            # 模型返回纯文本而非 JSON tool call，降级为 final_answer
            if message.content and message.tool_calls is None:
                # 从已加载的模块里取类型，不写 import 语句（smolagents 是本地路径，IDE 无法静态解析）
                _models = sys.modules[type(message).__module__]
                _CM  = type(message)
                _TC  = _models.ChatMessageToolCall
                _TCD = _models.ChatMessageToolCallDefinition
                return _CM(
                    role=message.role,
                    content=None,
                    tool_calls=[_TC(
                        id=f"fallback_{uuid.uuid4().hex[:8]}",
                        type="function",
                        function=_TCD(name="final_answer", arguments={"answer": message.content}),
                    )],
                    raw=message.raw,
                )
            raise
    _inner_model.postprocess_message = _postprocess_with_log

    return agent
