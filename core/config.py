"""
服务端配置：统一从环境变量（及项目根目录的 .env）读取。

模型的地址和密钥只在这里出现，任何入口都不接受调用方传入。
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes")


_DEFAULT_MODEL = "Pro/zai-org/GLM-5.1"


def _models() -> list[str]:
    """MODEL_ID 和 ALLOWED_MODELS 里的全部模型名（都可以用逗号分隔多个），去重并保持顺序。"""
    raw = (os.getenv("MODEL_ID") or _DEFAULT_MODEL) + "," + os.getenv("ALLOWED_MODELS", "")
    return list(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip())) or [_DEFAULT_MODEL]


# 默认模型：MODEL_ID 里的第一个
MODEL_ID = _models()[0]
API_BASE = os.getenv("API_BASE", "https://api.siliconflow.cn/v1/").strip()
API_KEY = os.getenv("API_KEY") or ""

# 默认启用沙箱；本机直接执行脚本要显式设 USE_DOCKER=0（仅开发）
USE_DOCKER = os.getenv("USE_DOCKER", "1").strip().lower() in ("1", "true", "yes")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "agent-sandbox:latest")
IS_PRODUCTION = os.getenv("AGENT_ENV", "development").strip().lower() == "production"

# 会话与限流
SESSION_IDLE_TTL = _int("SESSION_IDLE_TTL", 1800)            # 会话空闲多少秒后回收它的沙箱和工作副本
# 每用户同时保留沙箱和工作副本的会话数；超出时回收最久未用的（内容已持久化，随时可恢复）
MAX_SESSIONS_PER_USER = _int("MAX_SESSIONS_PER_USER", 5)
SESSION_RETENTION_DAYS = _int("SESSION_RETENTION_DAYS", 30)  # 会话多久不用后连同文件一起删除，0 为永久保留
TRAJECTORY_JSONL = _flag("TRAJECTORY_JSONL") if os.getenv("TRAJECTORY_JSONL") else True  # 是否同时写 trajectories/*.jsonl
MAX_CONCURRENT_RUNS_PER_USER = _int("MAX_CONCURRENT_RUNS_PER_USER", 2)
RATE_LIMIT_PER_MINUTE = _int("RATE_LIMIT_PER_MINUTE", 30)    # 每用户每分钟的请求数
MAX_TASK_CHARS = _int("MAX_TASK_CHARS", 20000)

# 单次运行的预算：任一用完就停止，并让模型交代做完了什么、还剩什么
MAX_STEPS = _int("MAX_STEPS", 15)
RUN_TOKEN_BUDGET = _int("RUN_TOKEN_BUDGET", 300000)   # 一次运行累计消耗的 token（输入 + 输出），0 为不限
RUN_TIME_BUDGET = _int("RUN_TIME_BUDGET", 300)        # 一次运行的总时长（秒），0 为不限
# 每次调用模型时放进去的历史上限（字符数）；超出时压缩较早的内容，完整记录仍保存在数据库里
CONTEXT_BUDGET_CHARS = _int("CONTEXT_BUDGET_CHARS", 40000)


def allowed_models() -> list[str]:
    """调用方可选的模型：MODEL_ID 加上 ALLOWED_MODELS。它们共用 API_BASE 和 API_KEY。"""
    return _models()


def resolve_model(requested: str | None) -> str:
    """把调用方给的模型名换成白名单里的模型；不在白名单里则抛 ValueError。"""
    if not requested or not requested.strip():
        return MODEL_ID
    requested = requested.strip()
    if requested not in allowed_models():
        raise ValueError(f"模型 '{requested}' 不可用。可选: {', '.join(allowed_models())}")
    return requested
