#!/usr/bin/env python
"""提取日志最近 N 行中的关键事件（ERROR / WARN / Exception / Failed / Timeout）。"""
import argparse
import json
import re
import sys
from pathlib import Path

_KEYWORDS = re.compile(r"ERROR|WARN|Exception|Failed|Timeout", re.I)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log_path", required=True, help="日志文件路径，可用 @input:<filename>")
    p.add_argument("--tail_lines", type=int, default=100, help="从末尾读取的行数")
    args = p.parse_args()

    path = Path(args.log_path)
    if not path.exists():
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    with path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    tail = lines[-args.tail_lines:]
    events = [
        {"line_no": len(lines) - args.tail_lines + i + 1, "content": line.rstrip()}
        for i, line in enumerate(tail)
        if _KEYWORDS.search(line)
    ]

    result = {
        "file": str(path),
        "tail_lines": args.tail_lines,
        "matched_events": len(events),
        "events": events[:50],  # 最多返回 50 条，避免超 context
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
