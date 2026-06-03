#!/usr/bin/env python
"""统计日志文件中的 ERROR / WARN / INFO 分布及高频错误模式。"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,\d]*")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log_path", required=True, help="日志文件路径，可用 @input:<filename>")
    p.add_argument("--top_n", type=int, default=5, help="返回前 N 个高频 ERROR 模式")
    args = p.parse_args()

    path = Path(args.log_path)
    if not path.exists():
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    counts = Counter({"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0})
    error_patterns: Counter = Counter()
    total = 0

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            total += 1
            if "ERROR" in line:
                counts["ERROR"] += 1
                normalized = _UUID_RE.sub("", _TS_RE.sub("", line)).strip()
                error_patterns[normalized] += 1
            elif "WARN" in line:
                counts["WARN"] += 1
            elif "INFO" in line:
                counts["INFO"] += 1
            else:
                counts["OTHER"] += 1

    top_errors = [
        {"pattern": pat[:200], "count": cnt}
        for pat, cnt in error_patterns.most_common(args.top_n)
    ]

    result = {
        "file": str(path),
        "total_lines": total,
        "level_counts": dict(counts),
        "top_error_patterns": top_errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
