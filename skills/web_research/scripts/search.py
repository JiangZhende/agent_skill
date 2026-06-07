#!/usr/bin/env python
"""DuckDuckGo 搜索，返回 title / url / abstract 供模型判断是否值得抓取。"""
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--max_results", type=int, default=8)
    args = p.parse_args()

    try:
        from ddgs import DDGS
    except ImportError:
        print(json.dumps({"error": "请安装: pip install ddgs"}))
        sys.exit(1)

    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(args.query, max_results=args.max_results))
    except Exception as e:
        print(json.dumps({"error": f"搜索失败: {e}"}))
        sys.exit(1)

    results = [
        {
            "index": i + 1,
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "abstract": r.get("body", ""),
        }
        for i, r in enumerate(raw)
    ]
    print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
