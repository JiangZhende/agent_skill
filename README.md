# Agent Skills POC

验证 skills 机制的轻量级 Agent 原型，基于 smolagents + OpenAI 兼容接口。

## 架构

### 运行时组件

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ 浏览器        │   │ HTTP 客户端   │   │ 命令行        │
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
        │                  │ POST /run (SSE)  │
 ┌──────▼───────┐   ┌──────▼───────┐   ┌──────▼───────┐
 │ app.py       │   │ api.py       │   │ main.py      │   入口层（app/api 需登录）
 │ Gradio UI    │   │ FastAPI      │   │ 示例脚本      │
 │ 会话:gr.State │   │ 会话:进程字典 │   │ 无会话        │
 │ 对话+Skill管理│   │ 每请求一线程  │   │              │
 └──┬───────┬───┘   └──────┬───────┘   └──────┬───────┘
    │       │              │                  │
    │       └──────────────┼──────────────────┤
    │                      ▼
    │   ┌──────────────────────────────────────────────────┐
    │   │ core/agent.py  build_agent()                     │  Agent 层
    │   │  smolagents ToolCallingAgent + 自定义系统提示       │  （每个会话一个实例，
    │   │  系统提示里只放各 skill 的 name + description       │    对话记忆在 agent.memory）
    │   └───┬─────────────────────────────────┬────────────┘
    │       │ 工具调用                         │ OpenAI 兼容协议
    │       ▼                                 ▼
    │   ┌─────────────────────────────┐   ┌────────────────────┐
    │   │ tools/                      │   │ 模型服务（外部）     │
    │   │  load_skill                 │   │ MODEL_ID/API_BASE  │
    │   │  read_resource              │   └────────────────────┘
    │   │  execute_skill_script       │
    │   │  list_inputs / list_outputs │
    │   │  ALL_TOOLS（业务工具，现为空）│
    │   └───┬───────────┬─────────┬───┘
    │       │           │         │
    │       ▼           ▼         ▼
    │  ┌──────────┐ ┌──────────┐ ┌─────────────────────────────┐
    │  │SkillRegi-│ │Workspace │ │ 脚本执行 core/executor.py    │  执行层
    │  │stry      │ │解析 @引用 │ │  USE_DOCKER=0: 宿主机子进程   │
    │  │扫描并解析 │ │统计配额   │ │  USE_DOCKER=1: 每会话每 skill │
    │  │SKILL.md  │ │          │ │   一个常驻容器，docker exec   │
    │  └────┬─────┘ └────┬─────┘ └──────┬──────────────────────┘
    │       │            │              │
    ▼       ▼            ▼              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 本地磁盘（项目根目录下）                                        │  存储层
 │  skills/<name>/SKILL.md + scripts/ + 资源文件   ← 全局共享      │
 │  workspaces/agent_<run_id>/inputs|workspace|outputs ← 每会话   │
 │  trajectories/*.jsonl                           ← 仅 app.py 写 │
 │  临时目录: 脚本 stdout 缓存 (@r_xxx)、导出的产物                 │
 └─────────────────────────────────────────────────────────────┘
    ▲
    └─ core/skill_installer.py：app.py 的"Skills 管理"页直接写入/删除 skills/
```

三个入口各自调用 `build_agent()`。上传文件、Skill 管理和 Trajectory 保存目前只有 `app.py` 提供；`api.py` 只有对话接口。所有状态都在单进程内存和本地磁盘，没有外部数据库或队列。

### 一次请求的流程

1. 入口收到任务，按会话取出或新建 agent；新建时扫描 `skills/` 并创建一个 workspace（因此新装的 skill 只对之后新建的会话生效）
2. agent 把系统提示（含全部 skill 的简介）和任务发给模型
3. 模型选中某个 skill，调用 `load_skill` 拿到完整的 SKILL.md 正文
4. 模型按正文里的步骤调用 `execute_skill_script`；参数里的 `@input:` / `@workspace:` / `@output:` / `@r_xxx` 被解析成真实路径后交给脚本
5. 脚本的 stdout 回给模型，同时缓存成一个 `result_id` 供下一步引用
6. 重复 4、5，直到模型调用 `final_answer`；入口把过程和答案流式推给客户端

### 目录结构

```
agent_skills_poc/
├── core/
│   ├── agent.py            # ToolCallingAgent 封装，注入 skill 机制
│   ├── auth.py             # 用户表与 token 鉴权（python -m core.auth 管理用户）
│   ├── config.py           # 服务端配置（模型地址和密钥、各项上限）
│   ├── sessions.py         # 会话归属、空闲回收、每用户上限
│   ├── executor.py         # 脚本执行器：DockerExecutor（沙箱）/ LocalExecutor（开发）
│   ├── skill_loader.py     # SKILL.md 解析与 SkillRegistry
│   ├── skill_installer.py  # 从 ZIP / SKILL.md 安装 / 卸载 skill
│   ├── trajectory.py       # 运行轨迹序列化为 JSONL
│   └── workspace.py        # 运行时文件隔离（inputs / workspace / outputs）
├── tools/
│   ├── run_script.py       # 执行 skill 内脚本（校验、@引用解析，执行交给 executor）
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
├── api.py                  # FastAPI 入口（POST /run，SSE 流式返回）
└── main.py                 # CLI 入口（单次任务，无 UI）
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
- **Docker 沙箱**：`USE_DOCKER=1` 时脚本在容器内执行，只能看到自己的 skill 目录和本会话 workspace，默认断网，镜像不存在时自动构建
- **多轮对话**：同一 session 内 `agent.memory` 保持上下文；有新文件上传时注入当前轮文件名提示；点"新对话"开启新 session
- **Trajectory**：每次 run 自动写 `trajectories/<timestamp>.jsonl`，含 system_prompt，可直接用于 SFT/DPO 训练
- **多用户并发**：对话、workspace、Docker 容器各自完全隔离；skill 库全局共享，安装/卸载操作串行化保证线程安全；Docker 镜像构建有全局锁，多用户同时触发只 build 一次

## 启动

### 首次准备

```bash
# 1. 安装依赖（建议用独立环境，如 conda activate agent）
pip install -r requirements.txt

# 2. 配置模型
cp .env.example .env
# 编辑 .env，填入 MODEL_ID、API_BASE、API_KEY

# 3. 创建第一个用户（管理员）。输出的 token 只显示一次，自己保存好
python -m core.auth add <用户名> --admin
```

没有创建任何用户时，两个入口都会拒绝启动并提示上面这条命令。

### 启动 Web UI

```bash
python app.py                 # 默认端口 7860
python app.py --port 7861     # 换端口
python app.py --skills-dir test_skills   # 指定其他 skill 目录（测试用）
```

打开 <http://localhost:7860>，登录时**用户名填账号名，密码填 token**。

`--share` 会生成公网链接，登录页会暴露在公网上，只在确实需要时使用。

### 启动 API

```bash
uvicorn api:app --port 8000
```

请求头带 `Authorization: Bearer <token>`，用法见下方 [API](#api) 一节。Web UI 和 API 是两个独立的进程，可以同时运行，共用同一份用户表。

### 启用 Docker 沙箱

默认（`USE_DOCKER=0`）skill 脚本在本机以子进程执行，只适合开发。要在容器里执行：

```bash
# 1. 确认 Docker 在运行（macOS 上用 colima 时先 colima start）
docker info

# 2. 在 .env 里设置
USE_DOCKER=1

# 3. 照常启动。沙箱镜像不存在时会在首次执行脚本时自动构建
python app.py
```

详见 [Docker 沙箱](#docker-沙箱) 一节。

### 停止

前台运行时按 `Ctrl+C`。服务退出时会回收所有会话的 workspace 和沙箱容器；如果进程被强杀，遗留的容器会在下次启动时清理。

### 启动失败时

| 现象 | 原因与处理 |
|---|---|
| `还没有任何用户` | 先执行 `python -m core.auth add <用户名> --admin` |
| `No module named 'smolagents'` | 当前 Python 环境没装依赖，切到装了依赖的环境 |
| 端口被占用 | 换 `--port`，或先停掉占用该端口的进程 |
| 登录失败 | 密码要填 token 而不是自己设的密码；token 丢了就重新执行 `add` 换发 |
| `生产环境必须启用沙箱` | 生产配置下不允许本机执行，启用沙箱或改回 `development` |
| `failed to connect to the docker API` | Docker 没在运行，先启动它 |

**配置文件 `.env`**（复制 `.env.example` 修改，不提交到 git）：

```bash
MODEL_ID=Pro/zai-org/GLM-5.1
API_BASE=https://api.siliconflow.cn/v1/
API_KEY=your-api-key-here

# 启用 Docker 沙箱（需要先安装 Docker）
USE_DOCKER=0
DOCKER_IMAGE=agent-sandbox:latest
SANDBOX_RUNTIME=runc
AGENT_ENV=development
```

## 鉴权与用户

两个入口都需要登录，没有配置任何用户时拒绝启动。

```bash
python -m core.auth add alice --admin   # 管理员：可以安装和卸载 skill
python -m core.auth add bob             # 普通用户
python -m core.auth add bob             # 再执行一次是换发 token，旧 token 立即失效
python -m core.auth list
python -m core.auth remove bob
```

用户表存在 `users.json`（已 gitignore，权限 600），只保存 token 的 SHA-256，明文 token 只在创建时显示一次。增删用户不需要重启服务。

- **API**：请求头带 `Authorization: Bearer <token>`
- **Web UI**：登录页用户名填账号名，密码填 token
- **本机开发**：设 `AUTH_DISABLED=1` 可跳过鉴权，所有请求算作管理员；`AGENT_ENV=production` 时不允许

**模型配置只在服务端**：`API_BASE` 和 `API_KEY` 只从 `.env` 读取，任何入口都不接受调用方传入。调用方只能在 `MODEL_ID` 和 `ALLOWED_MODELS` 列出的模型名里选。

**每用户的上限**（`.env` 可调）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SESSION_IDLE_TTL` | 1800 | 会话空闲多少秒后回收（连同 workspace 和沙箱容器） |
| `MAX_SESSIONS_PER_USER` | 5 | 同时存在的会话数 |
| `MAX_CONCURRENT_RUNS_PER_USER` | 2 | 同时运行的任务数 |
| `RATE_LIMIT_PER_MINUTE` | 30 | 每分钟请求数 |

两个入口各自计数。

## API

```bash
TOKEN=<创建用户时得到的 token>

# 新建会话并执行；响应头 X-Session-Id 是会话 id
curl -N -D - -X POST http://localhost:8000/run \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"task": "用一句话介绍你自己"}'

# 在同一会话里继续
curl -N -X POST http://localhost:8000/run \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"task": "再详细一点", "session_id": "<X-Session-Id>"}'
```

| 接口 | 说明 |
|---|---|
| `POST /run` | 执行任务，返回 SSE 流。请求体只有 `task`、`session_id`（可选）、`model_id`（可选，仅新建会话时生效），其他字段会被拒绝 |
| `GET /sessions` | 列出自己的会话 |
| `DELETE /sessions/{id}` | 删除自己的会话 |
| `GET /models` | 可选的模型 |
| `GET /health` | 健康检查，不需要 token |

会话 id 由服务端生成并绑定用户；别人的会话和不存在的会话都返回 404。同一会话同时只能有一个任务在运行（409）。超过上限返回 429。

## Docker 沙箱

脚本默认以子进程在本机执行（仅供开发）。设置 `USE_DOCKER=1` 后改为在 Docker 容器内执行。执行方式由 `core/executor.py` 的 `SandboxExecutor` 接口抽象，`execute_skill_script` 不关心脚本跑在哪里。

**构建镜像（首次使用或依赖变更后）：**

```bash
docker build -t agent-sandbox:latest .
```

镜像不存在时也会在首次执行脚本时**自动构建**。

**容器规格：**

| 项 | 值 |
|---|---|
| 粒度 | 每个会话、每个 skill 一个常驻容器，首次用到时启动，之后 `docker exec` 复用 |
| 挂载 | 当前 skill 目录 → `/skill`（只读）；本会话 workspace → `/agent_workspace`（读写）；其余不挂载 |
| 文件系统 | 根文件系统只读，`/tmp` 为 64MB tmpfs |
| 身份 | 服务进程的 uid:gid（非 root），no-new-privileges，cap-drop ALL |
| 网络 | 默认断网；SKILL.md frontmatter 声明 `network: true` 的 skill 才能联网 |
| 资源 | 内存 512MB、CPU 0.5 核、PID 100、单文件 500MB |
| 环境变量 | 只有 `AGENT_INPUTS_DIR` / `AGENT_WORKSPACE_DIR` / `AGENT_OUTPUTS_DIR`，不含服务端任何配置 |

**执行限制（两种执行方式都生效）：**

- 超时：默认 60 秒，上限 `SANDBOX_MAX_TIMEOUT`（默认 300 秒）。容器内用 `timeout -s KILL` 强制执行，超时后整个容器销毁，不留残留进程
- 输出：stdout / stderr 各自最多读 10MB，超过即终止脚本
- 参数：文件只能通过 `@input:` / `@workspace:` / `@output:` / `@r_xxx` 传入，参数里出现绝对路径或 `..` 会被拒绝
- 配额：workspace 总量超限后，停掉该会话的容器并拒绝后续执行

**容器回收：**

- 空闲 15 分钟自动销毁（`SANDBOX_IDLE_TTL`），下次用到再启动
- 每会话最多 4 个容器（`SANDBOX_MAX_PER_SESSION`），全局最多 32 个（`SANDBOX_MAX_CONTAINERS`），达到上限时回收最久未用的空闲容器
- 会话结束、进程正常退出时停掉全部容器；服务启动时清理上次崩溃遗留的容器

**相关配置：**

| 变量 | 说明 |
|---|---|
| `USE_DOCKER` | `1` 启用 Docker 沙箱 |
| `DOCKER_IMAGE` | 沙箱镜像名 |
| `SANDBOX_RUNTIME` | 容器运行时，默认 `runc`；Linux 生产环境建议装 gVisor 后设为 `runsc` |
| `AGENT_ENV` | 设为 `production` 时必须 `USE_DOCKER=1`，否则拒绝启动本机执行 |

**Skill 依赖：** 沙箱内不会在运行时安装任何包（断网且文件系统只读）。脚本需要的 Python 包要预先装进镜像（改 `Dockerfile` 后重新构建）。

**尚未实现：** 声明 `network: true` 的 skill 目前走 Docker 默认网桥，仍能访问宿主机所在的内网；出口白名单代理和按 skill 构建镜像在后续步骤。

## Web UI

`python app.py` 启动后提供两个 Tab：

- **💬 对话**：多轮对话，支持文件上传、步骤级流式输出、产物文件下载
- **🧩 Skills 管理**：查看已安装 skill；管理员可以上传 ZIP 或 SKILL.md 安装新 skill、卸载已有 skill

### 对话 Tab 功能

| 功能 | 说明 |
|------|------|
| 多轮对话 | 同一 session 保持上下文，"新对话"按钮重置 |
| 文件上传 | 上传后通过 `@input:<filename>` 供 skill 脚本引用；同一 session 内新上传文件会提示模型当前轮新文件名 |
| 流式输出 | 工具调用步骤完成后即时推送；`final_answer` 回答内容 token 级逐字流式显示 |
| 产物下载 | skill 生成的文件（Excel、报告等）可直接点击下载 |
| 模型选择 | 配置了 `ALLOWED_MODELS` 时出现模型下拉框（点"新对话"生效）；模型地址和密钥只在服务端配置 |

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

> **注意**：安装和卸载只有管理员能操作。skill 库全局共享，管理员的操作对所有用户可见。已在运行中的 session 不受影响，变更在下一次对话时生效。

## 测试

测试文件使用 `test_skills/` 目录（已 gitignore）：

```bash
# 全量测试
python -m pytest test_sandbox.py test_auth.py -v

# 鉴权、会话归属、限流（不需要网络和 Docker）
python -m pytest test_auth.py -v

# 只跑 read_resource 相关
python -m pytest test_sandbox.py::TestReadResource test_sandbox.py::TestRegionLookupScript -v

# 只跑真实容器的隔离测试（需要 Docker 运行中，否则自动跳过）
python -m pytest test_sandbox.py::TestDockerReal -v
```

## Trajectory 格式

Web UI 和 API 的每次运行都会在 `trajectories/` 下生成一个 JSONL 文件，文件名是 `<日期>_<时间>_<任务前 30 字>_<run_id 前 6 位>.jsonl`：

```jsonl
{"type": "system_prompt", "run_id": "...", "content": "..."}
{"type": "task",          "run_id": "...", "timestamp": "...", "task": "...", "user": "alice", "session_id": "...", "source": "web"}
{"type": "tool_call",     "run_id": "...", "step": 1, "tool": "load_skill",           "args": {...}, "duration": 1.2}
{"type": "observation",   "run_id": "...", "step": 1, "content": "..."}
{"type": "tool_call",     "run_id": "...", "step": 2, "tool": "execute_skill_script", "args": {...}, "duration": 3.4}  // duration 可为 null（步骤异常退出时）
{"type": "observation",   "run_id": "...", "step": 2, "content": "..."}
{"type": "final_answer",  "run_id": "...", "step": 3, "content": "..."}
```

`task` 记录里的 `user` 是发起对话的用户名，`session_id` 标识会话（同一会话的多轮对话相同），`source` 是入口（`web` 或 `api`）。

```bash
ls -t trajectories | head                           # 最近的几条
grep -l '"user": "alice"' trajectories/*.jsonl      # 某个用户的全部对话
grep -l '"session_id": "<id>"' trajectories/*.jsonl # 某个会话的各轮
grep -l "关键词" trajectories/*.jsonl                # 按内容找
```

注意：多轮对话时每轮存一个文件，内容是该会话到那一刻为止的全部步骤，所以同一会话里后一个文件包含前面几轮。文件是明文，含用户上传内容的脚本输出，目录权限要自行控制。

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
- 脚本的工作目录是 skill 目录，skill 自带的资源文件用相对路径引用（如 `resources/regions.json`）；参数里不能传绝对路径或 `..`
- 需要联网时在 SKILL.md frontmatter 里声明 `network: true`，否则沙箱内没有网络
- 额外的 Python 依赖要预先装进沙箱镜像，运行时不会安装

## 路线图

从原型到多用户平台的后续工作、已知安全问题及其修复状态，见 [docs/ROADMAP.md](docs/ROADMAP.md)。
