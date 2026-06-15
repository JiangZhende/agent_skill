# Agent Skills POC

验证 skills 机制的轻量级 Agent 原型，基于 smolagents + OpenAI 兼容接口。

## 架构

```
agent_skills_poc/
├── core/
│   ├── agent.py            # ToolCallingAgent 封装，注入 skill 机制
│   ├── skill_loader.py     # SKILL.md 解析与 SkillRegistry
│   ├── skill_installer.py  # 从 ZIP / SKILL.md 安装 / 卸载 skill
│   ├── trajectory.py       # 运行轨迹序列化为 JSONL
│   └── workspace.py        # 运行时文件隔离（inputs / workspace / outputs）
├── tools/
│   ├── run_script.py       # 执行 skill 内脚本（支持 subprocess / Docker 两种模式）
│   ├── load_skill.py       # 加载 skill 详情
│   ├── read_resource.py    # 读取 skill 目录内资源文件（JSON / CSV / YAML 等）
│   └── workspace_tool.py   # list_inputs / list_outputs 工具
├── skills/                 # 生产 skill 目录
│   ├── csv_to_report/      # CSV 转 Excel 报表
│   ├── log_analysis/       # 服务器日志分析
│   └── web_research/       # 深度网络调研（搜索 + 抓取正文）
├── workspaces/             # 每次运行的临时工作区（自动创建，gitignore）
├── trajectories/           # 每次运行的 JSONL 轨迹（自动生成）
├── Dockerfile              # Docker 沙箱镜像
├── app.py                  # Gradio Web UI
└── test.py                 # CLI 入口
```

## 范式选择：为什么是 ToolCallingAgent

选择 `ToolCallingAgent`（JSON tool call 范式），而不是 `CodeAgent`（代码生成范式）。

理由：
1. **Skill 已经把能力封装成强 schema 的 script**，模型只需"判断 → 选 script → 传参 → 解读"，不需要写 Python 代码
2. **JSON tool call 比 Python 代码生成更可靠**，主流大模型都做过 function calling 专项训练
3. **Trajectory 更干净**，天然就是 SFT/DPO 训练数据的格式
4. **私有化部署更简单**，脚本由 subprocess / Docker 隔离，主进程不需要 Python 沙箱

## 核心机制

- **Tool**：原子能力，稳定接口（`execute_skill_script`, `load_skill`, `read_resource`, `list_inputs`, `list_outputs`）
- **Skill**：业务方法论 + 脚本资源，通过 SKILL.md 描述
- **渐进式披露**：系统提示只列 skill briefs，模型按需 `load_skill` 加载详情，再按需 `read_resource` 读取资源文件
- **文件隔离**：每次 run 在 `workspaces/agent_<uuid>/` 下创建独立工作区，脚本通过 `@input:` / `@output:` / `@workspace:` 引用文件，不接触真实路径
- **Docker 沙箱**：`USE_DOCKER=1` 时脚本在容器内执行，资源隔离，镜像不存在时自动构建
- **多轮对话**：同一 session 内 `agent.memory` 保持上下文；有新文件上传时注入当前轮文件名提示；点"新对话"开启新 session
- **Trajectory**：每次 run 自动写 `trajectories/<timestamp>.jsonl`，含 system_prompt，可直接用于 SFT/DPO 训练
- **多用户并发**：对话、workspace、Docker 容器各自完全隔离；skill 库全局共享，安装/卸载操作串行化保证线程安全；Docker 镜像构建有全局锁，多用户同时触发只 build 一次

## 快速开始

```bash
pip install smolagents pyyaml gradio openpyxl requests beautifulsoup4 trafilatura ddgs python-dotenv

# 配置 API Key
cp .env.example .env
# 编辑 .env，填入真实 API Key

# 启动 Web UI
python app.py

# 可选参数
python app.py --port 7861 --share
python app.py --skills-dir test_skills   # 指定其他 skill 目录（测试用）
```

**配置文件 `.env`**（复制 `.env.example` 修改，不提交到 git）：

```bash
MODEL_ID=Pro/zai-org/GLM-5.1
API_BASE=https://api.siliconflow.cn/v1/
API_KEY=your-api-key-here

# 启用 Docker 沙箱（需要先安装 Docker）
USE_DOCKER=0
DOCKER_IMAGE=agent-sandbox:latest
```

## Docker 沙箱

脚本默认以 `subprocess` 在本机执行。设置 `USE_DOCKER=1` 后改为在 Docker 容器内执行，提供资源和权限隔离。

**构建镜像（首次使用或依赖变更后）：**

```bash
docker build -t agent-sandbox:latest .
```

镜像不存在时也会在首次执行脚本时**自动构建**。

**资源限制（默认值）：**

| 限制 | 值 |
|---|---|
| 内存 | 512MB |
| CPU | 0.5 核 |
| PID | 100 |
| 权限 | no-new-privileges，cap-drop ALL |

**Skill 私有依赖：** 如果某个 skill 的脚本需要额外的 Python 包，在 skill 目录下放 `requirements.txt`，执行时会自动在容器内安装。

## Web UI

`python app.py` 启动后提供两个 Tab：

- **💬 对话**：多轮对话，支持文件上传、步骤级流式输出、产物文件下载、模型配置
- **🧩 Skills 管理**：查看已安装 skill，上传 ZIP 或 SKILL.md 安装新 skill，卸载已有 skill

### 对话 Tab 功能

| 功能 | 说明 |
|------|------|
| 多轮对话 | 同一 session 保持上下文，"新对话"按钮重置 |
| 文件上传 | 上传后通过 `@input:<filename>` 供 skill 脚本引用；同一 session 内新上传文件会提示模型当前轮新文件名 |
| 流式输出 | 每个 step 完成后即时推送到界面 |
| 产物下载 | skill 生成的文件（Excel、报告等）可直接点击下载 |
| 模型配置 | 展开"⚙️ 模型配置"可修改 Model ID / API Base URL / API Key（点"新对话"生效） |

### 安装 / 卸载 Skill

支持两种格式安装：

**ZIP**（含脚本）：

```
my_skill.zip
└── my_skill/
    ├── SKILL.md
    └── scripts/
        └── run.py
```

**SKILL.md 单文件**（无脚本，适合纯提示类 skill）

安装/卸载后无需重启，下次发送任务即生效。

> **注意**：skill 库全局共享，任何用户的操作对所有用户可见。已在运行中的 session 不受影响，变更在下一次对话时生效。

## 测试

测试文件使用 `test_skills/` 目录（已 gitignore）：

```bash
# 全量测试（需要 smolagents 在 path 中）
PYTHONPATH="/Users/likun/code/deepresearch:$PYTHONPATH" python -m pytest test_sandbox.py -v

# 只跑 read_resource 相关
PYTHONPATH="/Users/likun/code/deepresearch:$PYTHONPATH" python -m pytest test_sandbox.py::TestReadResource test_sandbox.py::TestRegionLookupScript -v

# 只跑 Docker 相关（需要 Docker 运行中）
PYTHONPATH="/Users/likun/code/deepresearch:$PYTHONPATH" python -m pytest test_sandbox.py::TestDockerReal -v
```

## Trajectory 格式

每次运行在 `trajectories/` 下生成一个 JSONL 文件：

```jsonl
{"type": "system_prompt", "run_id": "...", "content": "..."}
{"type": "task",          "run_id": "...", "timestamp": "...", "task": "..."}
{"type": "tool_call",     "run_id": "...", "step": 1, "tool": "load_skill",           "args": {...}, "duration": 1.2}
{"type": "observation",   "run_id": "...", "step": 1, "content": "..."}
{"type": "tool_call",     "run_id": "...", "step": 2, "tool": "execute_skill_script", "args": {...}, "duration": 3.4}
{"type": "observation",   "run_id": "...", "step": 2, "content": "..."}
{"type": "final_answer",  "run_id": "...", "step": 3, "content": "..."}
```

## SKILL.md 写法

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

## 资源文件（可选）

如果 skill 依赖查找表、schema 等，放在 `resources/` 下，并在执行流程中引导模型调用：

```
read_resource(skill_name="my_skill", path="resources/config.json")
```

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
- 通过 `@input:<name>` / `@workspace:<name>` / `@output:<name>` 引用文件（框架自动解析成正确的绝对路径，Docker 模式下自动转换为容器内路径）
- 如需额外 Python 依赖，在 skill 目录下放 `requirements.txt`
