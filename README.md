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
    │  │stry      │ │解析 @引用 │ │  默认: 每会话每 skill 一个    │
    │  │扫描并解析 │ │统计配额   │ │   常驻容器，docker exec 执行  │
    │  │SKILL.md  │ │          │ │  USE_DOCKER=0: 宿主机子进程   │
    │  └────┬─────┘ └────┬─────┘ └──────┬──────────────────────┘
    │       │            │              │
    ▼       ▼            ▼              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 数据库（默认 SQLite：data/agent.db，可换 Postgres）            │  存储层
 │  用户、会话、每次运行、对话记忆、文件清单、轨迹                  │
 │ 文件存储（默认本地：data/files，可换 S3 / MinIO）               │
 │  会话的上传文件、中间文件（含 @r_xxx）、产物                     │
 │ 本地磁盘                                                      │
 │  skills/<name>/ + skills/.versions/        ← 全局共享          │
 │  workspaces/agent_s_<会话id>/              ← 运行时的工作副本   │
 │  trajectories/*.jsonl                      ← 轨迹的导出（可关） │
 └─────────────────────────────────────────────────────────────┘
```

`app.py` 和 `api.py` 共用同一个会话服务（`core/sessions.py`）和同一个数据库，所以在网页上开始的对话可以用 API 接着做，反过来也行。agent 不常驻内存：每条消息到来时现建一个，从数据库恢复对话记忆，跑完把新增的记忆、过程和文件存回去。服务重启（包括被强杀）之后，会话可以原样继续。

上传文件和 Skill 管理目前只有 `app.py` 提供；`main.py` 是不经过会话服务的独立示例。

### 一次请求的流程

1. 入口收到任务，校验会话归属；取出（或重建）该会话的 workspace 和沙箱，新建一个 agent 并从数据库恢复对话记忆。每次都会重新扫描 `skills/`，所以新发布的 skill 下一条消息就生效
2. agent 把系统提示（含全部 skill 的简介）和任务发给模型
3. 模型选中某个 skill，调用 `load_skill` 拿到完整的 SKILL.md 正文
4. 模型按正文里的步骤调用 `execute_skill_script`；参数里的 `@input:` / `@workspace:` / `@output:` / `@r_xxx` 被解析成真实路径后交给脚本
5. 脚本的 stdout 回给模型，同时缓存成一个 `result_id` 供下一步引用
6. 重复 4、5，直到模型调用 `final_answer`；入口把过程和答案流式推给客户端
7. 运行结束：新增的对话记忆、展示过程、轨迹写入数据库，workspace 里变化的文件同步到文件存储

### 目录结构

```
agent_skills_poc/
├── core/
│   ├── agent.py            # ToolCallingAgent 封装，注入 skill 机制
│   ├── db.py               # 数据库模型与迁移入口（python -m core.db）
│   ├── history.py          # 对话记忆的保存与恢复
│   ├── runtime.py          # 会话的运行时资源（workspace 工作副本 + 沙箱），可随时回收重建
│   ├── session_files.py    # workspace 与文件存储之间的同步
│   ├── storage.py          # 文件存储（本地目录 / S3）
│   ├── auth.py             # 用户表与 token 鉴权（python -m core.auth 管理用户）
│   ├── config.py           # 服务端配置（模型地址和密钥、各项上限）
│   ├── sessions.py         # 会话服务：归属校验、运行的开始与落库、回收（两个入口共用）
│   ├── executor.py         # 脚本执行器：DockerExecutor（沙箱）/ LocalExecutor（开发）
│   ├── skill_loader.py     # SKILL.md 解析与 SkillRegistry
│   ├── skill_installer.py  # skill 的上传校验、审核发布、版本与卸载
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
├── workspaces/             # 脚本运行时的工作副本（自动创建和回收，gitignore）
├── trajectories/           # 每次运行的 JSONL 轨迹导出（自动生成，数据库里另有一份）
├── deploy/egress-proxy/    # 沙箱出口代理（联网 skill 只能经它访问公网）
├── migrations/             # 数据库迁移脚本（Alembic）
├── data/                   # SQLite 数据库和本地文件存储（自动创建，gitignore）
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

- **Tool**：原子能力，稳定接口（`execute_skill_script`, `load_skill`, `read_resource`, `update_plan`, `list_inputs`, `list_outputs`）
- **Skill**：业务方法论 + 脚本资源，通过 SKILL.md 描述
- **渐进式披露**：系统提示只列 skill briefs，模型按需 `load_skill` 加载详情，再按需 `read_resource` 读取资源文件
- **上下文管理**：完整记忆照样保存，但发给模型时只放压缩后的历史：当前轮原样、最近两轮截短工具输出、更早的轮次只留问答，仍超出 `CONTEXT_BUDGET_CHARS` 时整轮省略
- **单次运行的预算**：步数、token、时长任一用完，或同一操作原地打转，就停下来让模型交代"已完成 / 未完成"，而不是硬给答案；skill 可声明各脚本每次任务最多调用几次
- **文件隔离**：每次 run 在 `workspaces/agent_<uuid>/` 下创建独立工作区，脚本通过 `@input:` / `@output:` / `@workspace:` 引用文件，不接触真实路径
- **Docker 沙箱**：脚本默认在容器内执行，只能看到自己的 skill 目录和本会话 workspace；默认断网，声明联网的 skill 只能经出口代理访问公网、访问不到内网；镜像不存在时自动构建
- **多轮对话与持久化**：对话记忆、过程和文件都存在数据库和文件存储里，agent 每条消息现建并恢复记忆；服务重启后会话可继续，历史会话可重新打开
- **Trajectory**：每次运行的轨迹存入数据库（按用户、会话可查），并默认导出为 `trajectories/*.jsonl`，可直接用于 SFT/DPO 训练
- **多用户并发**：对话、workspace、Docker 容器各自完全隔离；skill 库全局共享，上传的 skill 要经管理员审核发布才生效，带版本可回滚；Docker 镜像构建有全局锁，多用户同时触发只 build 一次

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

数据默认存在项目下的 `data/` 目录（SQLite 数据库和文件），首次启动时自动建表，不需要另外安装数据库。要换成 Postgres 或对象存储见 [数据与持久化](#数据与持久化)。

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

### Docker 沙箱

skill 脚本默认在 Docker 容器里执行，所以启动前要确认 Docker 在运行：

```bash
docker info        # macOS 上用 colima 时先 colima start
```

沙箱镜像不存在时会在首次执行脚本时自动构建。

- **所在网络要经代理才能访问外网时**：在 `.env` 里设 `EGRESS_UPSTREAM_PROXY`，否则联网的 skill（如搜索）在沙箱里会超时。宿主机上的代理要写成 `http://host.docker.internal:<端口>`，不能写 `127.0.0.1`。
- **本机开发、不想用 Docker**：在 `.env` 里设 `USE_DOCKER=0`，脚本直接在本机执行，没有任何隔离。`AGENT_ENV=production` 时不允许这样做。

详见 [Docker 沙箱](#docker-沙箱) 一节。

### 停止

前台运行时按 `Ctrl+C`。服务退出时会回收所有会话的 workspace 和沙箱容器；如果进程被强杀，遗留的容器会在下次启动时清理。

### 启动失败时

| 现象 | 原因与处理 |
|---|---|
| `还没有任何用户` | 先执行 `python -m core.auth add <用户名> --admin` |
| `No module named 'smolagents'` | 当前 Python 环境没装依赖，切到装了依赖的环境 |
| `No module named 'alembic'` | 依赖有更新，重新执行 `pip install -r requirements.txt` |
| 端口被占用 | 换 `--port`，或先停掉占用该端口的进程 |
| 登录失败 | 密码要填 token 而不是自己设的密码；token 丢了就重新执行 `add` 换发 |
| `生产环境必须启用沙箱` | 生产配置下不允许本机执行，启用沙箱或改回 `development` |
| `failed to connect to the docker API` | Docker 没在运行，先启动它 |

**配置文件 `.env`**（复制 `.env.example` 修改，不提交到 git）：

```bash
MODEL_ID=Pro/zai-org/GLM-5.1
API_BASE=https://api.siliconflow.cn/v1/
API_KEY=your-api-key-here

# Docker 沙箱，默认启用；本机开发可设为 0
USE_DOCKER=1
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

用户存在数据库的 `users` 表里，只保存 token 的 SHA-256，明文 token 只在创建时显示一次。旧版本的 `users.json` 会在数据库里还没有用户时自动导入一次，之后不再使用。增删用户不需要重启服务。

- **API**：请求头带 `Authorization: Bearer <token>`
- **Web UI**：登录页用户名填账号名，密码填 token
- **本机开发**：设 `AUTH_DISABLED=1` 可跳过鉴权，所有请求算作管理员；`AGENT_ENV=production` 时不允许

**模型配置只在服务端**：`API_BASE` 和 `API_KEY` 只从 `.env` 读取，任何入口都不接受调用方传入。调用方只能在 `MODEL_ID` 和 `ALLOWED_MODELS` 列出的模型名里选。

**每用户的上限**（`.env` 可调）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SESSION_IDLE_TTL` | 1800 | 会话空闲多少秒后回收它的沙箱和工作副本。内容都已持久化，再用到时自动恢复 |
| `MAX_SESSIONS_PER_USER` | 5 | 同时保留沙箱和工作副本的会话数。超出时回收最久未用的，不会拒绝请求 |
| `SESSION_RETENTION_DAYS` | 30 | 会话多少天不用后连同文件一起删除，0 为永久保留 |
| `MAX_CONCURRENT_RUNS_PER_USER` | 2 | 同时运行的任务数 |
| `RATE_LIMIT_PER_MINUTE` | 30 | 每分钟请求数 |

**单次运行的预算**（任一用完即停止，并让模型交代已完成和未完成的部分）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_STEPS` | 15 | 一次运行最多几步 |
| `RUN_TOKEN_BUDGET` | 300000 | 一次运行累计消耗的 token（输入 + 输出），0 为不限 |
| `RUN_TIME_BUDGET` | 300 | 一次运行的总时长（秒），0 为不限 |
| `CONTEXT_BUDGET_CHARS` | 40000 | 每次调用模型时放进去的历史上限（字符数），超出时压缩较早的内容 |

同一工具、同一参数连续失败 2 次，或连续调用 3 次，也会停止。每次运行的步数、token 用量、上下文峰值和停止原因记在 `runs` 表里，`GET /sessions/{id}` 可以看到。

频率和并发由两个入口各自计数。

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
| `GET /sessions` | 列出自己的会话（最近使用的在前，服务重启后仍在） |
| `GET /sessions/{id}` | 会话详情和历次运行（任务、回答、状态）；加 `?events=1` 带上每次运行的过程 |
| `GET /sessions/{id}/files` | 会话的上传文件和产物 |
| `GET /sessions/{id}/files/{area}/{name}` | 下载一个文件，`area` 为 `inputs` 或 `outputs` |
| `DELETE /sessions/{id}` | 删除自己的会话，连同文件、对话记忆和运行记录 |
| `GET /models` | 可选的模型 |
| `GET /health` | 健康检查，不需要 token |

会话 id 由服务端生成并绑定用户；别人的会话和不存在的会话都返回 404。同一会话同时只能有一个任务在运行（409）。超过频率或并发上限返回 429。`/run` 的响应头里还有 `X-Run-Id`，是这次运行的编号。

## Docker 沙箱

脚本默认在 Docker 容器内执行；`USE_DOCKER=0` 时改为本机子进程（仅供开发，没有隔离）。执行方式由 `core/executor.py` 的 `SandboxExecutor` 接口抽象，`execute_skill_script` 不关心脚本跑在哪里。

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
| 网络 | 默认断网；SKILL.md frontmatter 声明 `network: true` 的 skill 只能经出口代理访问公网（见下） |
| 资源 | 内存 512MB、CPU 0.5 核、PID 100、单文件 500MB |
| 环境变量 | 只有 `AGENT_INPUTS_DIR` / `AGENT_WORKSPACE_DIR` / `AGENT_OUTPUTS_DIR`，不含服务端任何配置 |

**执行限制（两种执行方式都生效）：**

- 超时：默认 60 秒，上限 `SANDBOX_MAX_TIMEOUT`（默认 300 秒）。容器内用 `timeout -s KILL` 强制执行，超时后整个容器销毁，不留残留进程
- 输出：stdout / stderr 各自最多读 10MB，超过即终止脚本
- 参数：文件只能通过 `@input:` / `@workspace:` / `@output:` / `@r_xxx` 传入，参数里出现绝对路径或 `..` 会被拒绝
- 配额：脚本运行期间每秒检查一次 workspace 总量，超限立即终止脚本、销毁容器，并拒绝该会话后续的执行

**容器回收：**

- 空闲 15 分钟自动销毁（`SANDBOX_IDLE_TTL`），下次用到再启动
- 每会话最多 4 个容器（`SANDBOX_MAX_PER_SESSION`），全局最多 32 个（`SANDBOX_MAX_CONTAINERS`），达到上限时回收最久未用的空闲容器
- 会话结束、进程正常退出时停掉全部容器；服务启动时清理上次崩溃遗留的容器

**相关配置：**

| 变量 | 说明 |
|---|---|
| `USE_DOCKER` | 默认 `1`；设为 `0` 时在本机直接执行脚本（仅开发） |
| `SANDBOX_EGRESS` | 联网 skill 的出口方式，默认 `proxy`；`direct` 为直接接默认网桥（能访问内网，仅调试） |
| `EGRESS_UPSTREAM_PROXY` | 上游 HTTP 代理，所在网络要经代理才能访问外网时设置 |
| `EGRESS_ALLOW_DOMAINS` | 可选的域名白名单（含子域），不设则放行所有公网域名 |
| `EGRESS_ALLOWED_PORTS` | 允许访问的目标端口，默认 `80,443` |
| `SANDBOX_PIP_INDEX` | 构建 skill 依赖镜像时用的 pip 源 |
| `DOCKER_IMAGE` | 沙箱镜像名 |
| `SANDBOX_RUNTIME` | 容器运行时，默认 `runc`；Linux 生产环境建议装 gVisor 后设为 `runsc` |
| `AGENT_ENV` | 设为 `production` 时必须 `USE_DOCKER=1`，否则拒绝启动本机执行 |

**联网与出口代理：**

声明了 `network: true` 的 skill 接入一个没有外部路由的内部网络（`agent-sandbox-egress`），唯一能连到的是出口代理容器（`agent-egress-proxy`，代码在 `deploy/egress-proxy/`）。代理在转发前检查目标：

- 拒绝一切非公网地址：内网、回环、链路本地（含云元数据地址 `169.254.169.254`）、保留地址
- 只允许 80 和 443 端口
- 只连接检查过的那个 IP，域名在检查之后再解析成别的地址也没用
- 每个请求记一行日志（`docker logs agent-egress-proxy`），可以看到哪个容器访问了什么、是放行还是拒绝

脚本里用 `requests`、`urllib` 等会读取 `HTTP_PROXY` / `HTTPS_PROXY` 的库不需要改动；自己开 socket 直连的代码在沙箱里连不出去。代理容器由所有会话共用，首次用到时自动启动，服务退出后不会自动删除（`docker rm -f agent-egress-proxy` 可手动清掉）。

配置了 `EGRESS_UPSTREAM_PROXY` 时，通过检查的请求会交给上游代理去连接，域名也由上游解析，所以上游代理自己不应该能访问到需要保护的内网。

**Skill 依赖：**

沙箱内不会在运行时安装任何包。skill 需要额外的 Python 包时，在 skill 目录下放 `requirements.txt`：发布该 skill 时会基于基础镜像构建一个它专用的镜像（`agent-skill-<name>:<依赖内容的哈希>`），依赖在这一步装好；构建失败则发布失败。所有 skill 都用得到的包直接加进 `Dockerfile`。

**尚未实现 / 已知局限：**

- 磁盘配额靠运行期间的轮询来执行，不是内核级的硬限制：超限后最多多写约一秒的量。要严格上限，需把 `workspaces/` 放在带配额的文件系统或独立的卷上
- gVisor（`SANDBOX_RUNTIME=runsc`）没有在 Linux 上实测
- 卸载 skill 不会删除它的依赖镜像

## Web UI

`python app.py` 启动后提供两个 Tab：

- **💬 对话**：多轮对话，支持文件上传、步骤级流式输出、产物文件下载
- **🧩 Skills 管理**：查看已生效的 skill；管理员可以上传、审核发布、回滚和卸载 skill

### 对话 Tab 功能

| 功能 | 说明 |
|------|------|
| 多轮对话 | 同一会话保持上下文；"新对话"回到空白页，原来的会话仍在历史里 |
| 历史会话 | 左侧下拉框列出自己的会话，选中即恢复聊天记录和产物，之后的消息接着它继续；可删除当前会话 |
| 文件上传 | 上传后通过 `@input:<filename>` 供 skill 脚本引用；本轮新增或内容有变化的文件会提示给模型 |
| 流式输出 | 工具调用步骤完成后即时推送；`final_answer` 回答内容 token 级逐字流式显示 |
| 产物下载 | skill 生成的文件（Excel、报告等）可直接点击下载；重新打开历史会话时同样可以下载 |
| 模型选择 | 配置了 `ALLOWED_MODELS` 时出现模型下拉框（点"新对话"生效）；模型地址和密钥只在服务端配置 |

### Skill 的上传、审核与版本

只有管理员能操作。上传不会直接生效，要经过发布：

1. **上传**：ZIP 或单个 SKILL.md。生成一个**待审核**版本，存放在 `skills/.versions/<name>/<版本号>/`，不影响正在使用的 skill
2. **审核**：页面右侧显示该版本的概要（是否联网、依赖、文件清单）；脚本内容请到上面的目录里查看
3. **发布**：该版本成为 `skills/<name>/` 下生效的内容。带 `requirements.txt` 的会先构建镜像，可能要等一两分钟

再次上传同名 skill 是新增一个版本，不会覆盖。在"审核与版本"里选一个旧版本点发布，就是回滚。原本就在 `skills/` 下的内容（如随仓库带的）在第一次被替换时会自动存成一个版本，同样可以回滚。

**ZIP 结构**（可以带一个顶层目录，也可以直接把 SKILL.md 放在根目录）：

```
my_skill.zip
└── my_skill/
    ├── SKILL.md
    ├── requirements.txt      # 可选
    ├── scripts/
    │   └── run.py
    └── resources/            # 可选
        └── table.json
```

**限制：**

- skill 名以 SKILL.md 里的 `name` 为准，只允许字母、数字、下划线、连字符
- 压缩包不超过 10MB，解压后总共不超过 50MB、单文件不超过 10MB、不超过 200 个文件
- 只允许 `.py .md .sh .txt .yaml .yml .json .csv .toml`；不允许符号链接和带 `..` 的路径

发布、回滚和卸载都不需要重启，对之后新建的对话生效；已经在进行中的对话继续用它开始时的内容。

## 测试

测试文件使用 `test_skills/` 目录（已 gitignore）：

```bash
# 全量测试
python -m pytest test_sandbox.py test_auth.py test_skill_store.py test_skill_scripts.py test_context.py -v

# 鉴权、会话归属、持久化与恢复、限流（不需要网络和 Docker）
python -m pytest test_auth.py -v

# 只跑 read_resource 相关
python -m pytest test_sandbox.py::TestReadResource test_sandbox.py::TestRegionLookupScript -v

# 只跑真实容器的隔离测试（需要 Docker 运行中，否则自动跳过）
python -m pytest test_sandbox.py::TestDockerReal -v
```

## 数据与持久化

| 内容 | 存在哪里 | 默认 | 可换成 |
|---|---|---|---|
| 用户、会话、每次运行、对话记忆、文件清单、轨迹 | 数据库 | SQLite 文件 `data/agent.db` | Postgres：`DATABASE_URL=postgresql+psycopg://...` |
| 会话的上传文件、中间文件、产物 | 文件存储 | 本地目录 `data/files` | S3 兼容的对象存储（如 MinIO）：`FILE_STORE=s3` 加 `S3_*` |
| 脚本运行时读写的目录 | 工作副本 | `workspaces/` | `WORKSPACES_DIR` |

工作副本只是缓存：每次运行结束后，变化的文件会同步到文件存储；会话空闲、超出上限或服务重启后工作副本被删除，下次用到时再取回。进程被强杀时来不及同步的文件，会在下次启动时补同步。

**备份**只需要备份数据库和文件存储（默认即整个 `data/` 目录）。

**换成 Postgres / MinIO**：在 `.env` 里设置对应变量（见 [.env.example](.env.example)），并安装驱动：

```bash
pip install "psycopg[binary]"   # Postgres
pip install boto3               # S3 / MinIO
```

单机部署用默认的 SQLite 和本地目录就够了；要跑多个服务副本时才需要换，并且所有副本要指向同一个数据库和同一个文件存储。

**表结构迁移**（Alembic，脚本在 `migrations/`）：

```bash
python -m core.db upgrade              # 升级到最新（服务启动时也会自动执行）
python -m core.db current              # 查看当前版本
python -m core.db revision -m "说明"    # 改了 core/db.py 里的模型后，生成新的迁移脚本
```

**保留期**：会话超过 `SESSION_RETENTION_DAYS`（默认 30 天）没有使用，会连同文件、对话记忆和运行记录一起删除；设为 0 则永久保留。

## Trajectory 格式

每次运行的轨迹存在数据库 `runs` 表的 `trajectory` 字段里，同时默认导出一个 JSONL 文件到 `trajectories/`（`TRAJECTORY_JSONL=0` 可关闭），文件名是 `<日期>_<时间>_<任务前 30 字>_<run_id 前 6 位>.jsonl`：

```jsonl
{"type": "system_prompt", "run_id": "...", "content": "..."}
{"type": "task",          "run_id": "...", "timestamp": "...", "task": "...", "user": "alice", "session_id": "...", "source": "web"}
{"type": "tool_call",     "run_id": "...", "step": 1, "tool": "load_skill",           "args": {...}, "duration": 1.2}
{"type": "observation",   "run_id": "...", "step": 1, "content": "..."}
{"type": "tool_call",     "run_id": "...", "step": 2, "tool": "execute_skill_script", "args": {...}, "duration": 3.4}  // duration 可为 null（步骤异常退出时）
{"type": "observation",   "run_id": "...", "step": 2, "content": "..."}
{"type": "final_answer",  "run_id": "...", "step": 3, "content": "..."}
```

`task` 记录里的 `user` 是发起对话的用户名，`session_id` 标识会话（同一会话的多轮对话相同），`source` 是入口（`web` 或 `api`）。每个文件只包含**这一次运行**的步骤；要看整个会话，按 `session_id` 把各个文件连起来，或者直接查数据库。

```bash
ls -t trajectories | head                           # 最近的几条
grep -l '"user": "alice"' trajectories/*.jsonl      # 某个用户的全部对话
grep -l '"session_id": "<id>"' trajectories/*.jsonl # 某个会话的各轮
grep -l "关键词" trajectories/*.jsonl                # 按内容找
```

文件是明文，含用户上传内容的脚本输出，目录权限要自行控制。

## SKILL.md 写法

```markdown
---
name: my_skill
description: 何时使用这个 skill（写"什么时候用"，不是"是什么"）
triggers:
  - 关键词1
  - 关键词2
network: true             # 可选：脚本需要联网时声明，否则沙箱里没有网络
call_limits:              # 可选：每次任务里各脚本最多调用几次，到上限后系统直接拒绝
  scripts/search.py: 2
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
- 容易被模型反复调用的脚本（如搜索），在 frontmatter 的 `call_limits` 里限制每次任务的调用次数
- 需要额外的 Python 包时在 skill 目录下放 `requirements.txt`，发布时会为它构建镜像；运行时不会安装任何东西
- 联网请用会读取 `HTTP_PROXY` / `HTTPS_PROXY` 的库（`requests`、`urllib` 等）；沙箱里只能经出口代理访问公网的 80 / 443 端口

## 路线图

从原型到多用户平台的后续工作、已知安全问题及其修复状态，见 [docs/ROADMAP.md](docs/ROADMAP.md)。
