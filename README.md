# Agent Skills POC

验证 skills 机制的轻量级 Agent 原型，基于 smolagents + OpenAI 兼容接口。

## 架构

```
agent_skills_poc/
├── core/
│   ├── agent.py            # ToolCallingAgent 封装，注入 skill 机制
│   ├── skill_loader.py     # SKILL.md 解析与 SkillRegistry
│   ├── skill_installer.py  # 从 ZIP / SKILL.md 安装 skill
│   ├── trajectory.py       # 运行轨迹序列化为 JSONL
│   ├── workspace.py        # 运行时文件隔离（inputs / workspace / outputs）
│   └── sandbox.py          # Docker 沙箱配置说明
├── tools/
│   ├── run_script.py       # 按 SKILL.md 契约执行 skill 内脚本
│   ├── load_skill.py       # 加载 skill 详情
│   ├── workspace_tool.py   # list_inputs / list_outputs 工具
│   └── web_search.py       # DuckDuckGo 网络搜索工具
├── skills/
│   ├── csv_to_report/        # CSV 转 Excel 报表
│   │   ├── SKILL.md
│   │   └── scripts/
│   ├── log_analysis/         # 服务器日志分析
│   │   ├── SKILL.md
│   │   └── scripts/
│   └── sales_report/         # 销售报告（Mock 数据，仅用于测试）
│       ├── SKILL.md
│       └── scripts/
├── trajectories/           # 每次运行的 JSONL 轨迹（自动生成）
├── app.py                  # Gradio Web UI
└── test.py                 # CLI 入口
```

## 范式选择：为什么是 ToolCallingAgent

我们选择 `ToolCallingAgent`（JSON tool call 范式），而不是 `CodeAgent`（代码生成范式）。

理由：
1. **Skill 已经把能力封装成强 schema 的 script**，模型只需"判断 → 选 script → 传参 → 解读"，不需要写 Python 代码
2. **JSON tool call 比 Python 代码生成更可靠**，主流大模型都做过 function calling 专项训练
3. **Trajectory 更干净**，天然就是 SFT/DPO 训练数据的格式
4. **私有化部署更简单**，主进程不需要 Python 沙箱，脚本由 subprocess 隔离

未来如果需要灵活的代码组合能力（如即兴探索任务），再考虑混合 CodeAgent。

## 核心机制

- **Tool**：原子能力，稳定接口（`execute_skill_script`, `load_skill`, `list_inputs`, `list_outputs`, `web_search`）
- **Skill**：业务方法论 + 脚本资源，通过 SKILL.md 描述
- **渐进式披露**：系统提示只列 skill briefs，模型按需 `load_skill` 加载详情
- **文件隔离**：每次 run 独立 workspace，脚本通过 `@input:` / `@output:` / `@workspace:` 引用文件，不接触真实路径
- **多轮对话**：同一 session 内 agent.memory 保持上下文，点"新对话"开启新 session
- **Trajectory**：每次 run 自动写 `trajectories/<timestamp>.jsonl`，含 system_prompt，可直接用于 SFT/DPO 训练

## 快速开始

```bash
pip install smolagents pyyaml gradio openpyxl ddgs

# 列出所有 skill
python test.py --list-skills

# 执行任务（CLI）
python test.py --task "华南区 2025Q3 销售报告"

# 带文件上传
python test.py --task "把这个 CSV 转成 Excel" --upload data.csv --outputs-dir ./out

# 启动 Web UI
python app.py
python app.py --port 7861 --share
```

环境变量配置（对接内网推理服务）：

```bash
export MODEL_ID="openai/qwen3-32b"
export API_BASE="http://internal-vllm:8000/v1"
export API_KEY="EMPTY"
```

## Web UI

`python app.py` 启动后提供两个 Tab：

- **💬 对话**：多轮对话，支持文件上传、步骤级流式输出、产物文件下载、模型配置
- **🧩 Skills 管理**：查看已安装 skill，上传 ZIP 或 SKILL.md 安装新 skill

### 对话 Tab 功能

| 功能 | 说明 |
|------|------|
| 多轮对话 | 同一 session 保持上下文，"新对话"按钮重置 |
| 文件上传 | 上传后通过 `@input:<filename>` 供 skill 脚本引用 |
| 流式输出 | 每个 step 完成后即时推送到界面 |
| 产物下载 | skill 生成的文件（Excel、报告等）可直接点击下载 |
| 模型配置 | 展开"⚙️ 模型配置"可修改 Model ID / API Base URL / API Key |

模型配置修改后，点"新对话"才生效（当前 session 继续使用原有模型）。

### 安装新 Skill

支持两种格式：

**ZIP**（含脚本）：

```
my_skill.zip
└── my_skill/
    ├── SKILL.md
    └── scripts/
        └── run.py
```

**SKILL.md 单文件**（无脚本，适合纯提示类 skill）

安装后无需重启，下次发送任务即生效。

## Trajectory 格式

每次运行在 `trajectories/` 下生成一个 JSONL 文件，首行为 system_prompt，后续每行一条 step 记录：

```jsonl
{"type": "system_prompt","run_id": "...", "content": "You are a helpful assistant..."}
{"type": "task",         "run_id": "...", "timestamp": "...", "task": "..."}
{"type": "tool_call",    "run_id": "...", "step": 1, "tool": "load_skill",           "args": {...}, "duration": 1.2}
{"type": "observation",  "run_id": "...", "step": 1, "content": "..."}
{"type": "tool_call",    "run_id": "...", "step": 2, "tool": "execute_skill_script", "args": {...}, "duration": 3.4}
{"type": "observation",  "run_id": "...", "step": 2, "content": "..."}
{"type": "final_answer", "run_id": "...", "step": 3, "content": "..."}
```

## SKILL.md 写法

frontmatter 声明元信息，正文写执行流程供模型参考：

```markdown
---
name: my_skill
description: 何时使用这个 skill（写"什么时候用"，不是"是什么"）
triggers:
  - 关键词1
  - 关键词2
---

## 适用场景
...

## Scripts

### script_name

参数说明...

## 执行流程

### Step 1: ...
调用 execute_skill_script，传入真实参数值。
```

## Script 编写约定

每个 script 是独立的 Python CLI 程序：

- 用 `argparse` 接受 `--<name> <value>` 参数
- 成功时输出 JSON 到 stdout，退出码 0
- 失败时非零退出码 + stderr 报错
- 读写文件通过环境变量 `AGENT_INPUTS_DIR` / `AGENT_WORKSPACE_DIR` / `AGENT_OUTPUTS_DIR`，或通过 `@input:` / `@output:` 引用（框架自动解析成绝对路径）
