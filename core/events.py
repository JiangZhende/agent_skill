"""
运行事件：worker 在运行过程中写入，客户端（SSE、Web UI）按序号读取。

事件存在数据库里，所以读事件的进程和跑任务的进程可以不是同一个；客户端断线重连时，
带上收到的最后一个序号，从下一条接着读。

事件类型：
    token  最终回答的流式文本（content 是到目前为止的全文，不是增量）
    step   一个步骤完成（role / content / metadata，与 smolagents 的展示消息一致）
    error  运行出错（content 是给用户看的说明）
    done   运行结束（answer 是最终回答，status 是运行的最终状态）
"""
import json
import re
import time

from sqlalchemy import delete, select

from core import db

TOKEN_INTERVAL = 0.3   # token 事件最多每隔这么久写一次（只保留最新全文），避免每个字都写一次库


class EventWriter:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.seq = 0
        self._pending_token = None
        self._last_token_at = 0.0

    def emit(self, type_: str, payload: dict):
        if type_ == "token":
            self._pending_token = payload
            if time.monotonic() - self._last_token_at < TOKEN_INTERVAL:
                return
        else:
            self.flush_token()
        self._write(type_, payload)

    def flush_token(self):
        if self._pending_token is not None:
            payload, self._pending_token = self._pending_token, None
            self._write("token", payload)

    def _write(self, type_: str, payload: dict):
        if type_ == "token":
            self._last_token_at = time.monotonic()
            self._pending_token = None
        self.seq += 1
        with db.session_scope() as s:
            s.add(db.RunEvent(run_id=self.run_id, seq=self.seq, type=type_, payload=payload))


def read_events(run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
    with db.session_scope() as s:
        rows = s.scalars(select(db.RunEvent).where(db.RunEvent.run_id == run_id, db.RunEvent.seq > after_seq)
                         .order_by(db.RunEvent.seq).limit(limit))
        return [{"seq": r.seq, "type": r.type, **r.payload} for r in rows]


def drop_tokens(run_id: str):
    """运行结束后删掉中间的 token 事件：最终回答已在 done 事件和运行记录里。"""
    with db.session_scope() as s:
        s.execute(delete(db.RunEvent).where(db.RunEvent.run_id == run_id, db.RunEvent.type == "token"))


def extract_answer_from_deltas(accumulated) -> str:
    """从累积的流式分片中提取 final_answer 的 answer 文本（兼容原生 tool call 和写在 content 里的 JSON）。"""
    from smolagents.models import agglomerate_stream_deltas

    msg = agglomerate_stream_deltas(accumulated)
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        if tc.function.name != "final_answer":
            return ""
        raw = tc.function.arguments or ""
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
            return args.get("answer", "") if isinstance(args, dict) else ""
        except json.JSONDecodeError:
            return _partial_answer(raw)

    if msg.content:
        content = msg.content.strip()
        if not content.startswith("{"):
            return content
        tool_m = re.search(r'"(?:name|tool)"\s*:\s*"([^"]+)"', content)
        if not tool_m or tool_m.group(1) != "final_answer":
            return ""
        return _partial_answer(content)
    return ""


def _partial_answer(raw: str) -> str:
    """JSON 还没生成完整时，尽量取出 "answer" 已经生成的部分。"""
    m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
    if not m:
        return ""
    partial = m.group(1)
    try:
        return json.loads(f'"{partial}"')
    except json.JSONDecodeError:
        partial = re.sub(r'\\u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), partial)
        return partial.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
