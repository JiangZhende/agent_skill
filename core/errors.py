"""
把运行时异常换成可以给用户看的说明。

异常原文可能带模型服务地址等内部信息，只记日志；用户看到的是原因类别和一个错误编号。
"""
import logging
import uuid

logger = logging.getLogger("agent.run")

# 模型服务返回的 HTTP 状态码 → 给用户的说明
_MODEL_STATUS_MESSAGES = {
    401: "模型服务拒绝了服务端的密钥，请联系管理员检查 API_KEY",
    402: "模型服务账户余额不足，请联系管理员充值或更换模型服务",
    403: "模型服务拒绝访问（密钥无权使用该模型），请联系管理员",
    404: "模型服务找不到所选的模型，请联系管理员检查模型名",
    429: "模型服务限流，请稍后再试",
}


def _model_status(exc: BaseException) -> int | None:
    """沿异常链找模型服务返回的 HTTP 状态码。"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status
        exc = exc.__cause__ or exc.__context__
    return None


def describe(exc: BaseException, session_id: str = None) -> str:
    """记录异常详情，返回给用户看的一句话（带错误编号，便于在日志里对上）。"""
    error_id = uuid.uuid4().hex[:8]
    logger.error("run failed [%s] session=%s", error_id, session_id, exc_info=exc)
    status = _model_status(exc)
    if status in _MODEL_STATUS_MESSAGES:
        reason = _MODEL_STATUS_MESSAGES[status]
    elif status is not None and status >= 500:
        reason = "模型服务暂时不可用，请稍后再试"
    else:
        reason = "任务执行出错"
    ref = f"错误编号 {error_id}" + (f"，会话 {session_id}" if session_id else "")
    return f"{reason}（{ref}）"
