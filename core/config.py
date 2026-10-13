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

USE_DOCKER = _flag("USE_DOCKER")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "agent-sandbox:latest")
IS_PRODUCTION = os.getenv("AGENT_ENV", "development").strip().lower() == "production"

# 会话与限流
SESSION_IDLE_TTL = _int("SESSION_IDLE_TTL", 1800)            # 会话空闲多少秒后回收
MAX_SESSIONS_PER_USER = _int("MAX_SESSIONS_PER_USER", 5)
MAX_CONCURRENT_RUNS_PER_USER = _int("MAX_CONCURRENT_RUNS_PER_USER", 2)
RATE_LIMIT_PER_MINUTE = _int("RATE_LIMIT_PER_MINUTE", 30)    # 每用户每分钟的请求数
MAX_TASK_CHARS = _int("MAX_TASK_CHARS", 20000)


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
