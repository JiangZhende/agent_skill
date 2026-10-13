#!/usr/bin/env python
"""DuckDuckGo 搜索，返回 title / url / abstract 供模型判断是否值得抓取。"""
import argparse
import json
import re
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

    # 中文查询用中文区域，结果更相关
    region = "cn-zh" if re.search(r"[\u3400-\u9fff]", args.query) else "us-en"
    raw, last_error = [], None
    # 默认的 auto 会依次询问多个搜索引擎，一次要十秒左右；先只问一个，没有结果再退回 auto
    for backend in ("duckduckgo", "auto"):
        try:
            raw = DDGS().text(args.query, max_results=args.max_results, region=region, backend=backend)
        except Exception as e:
            last_error = e
            continue
        if raw:
            break
    if not raw:
        print(json.dumps({"error": f"搜索失败: {last_error}" if last_error else "没有搜到结果，换个搜索词试试"},
                         ensure_ascii=False))
        sys.exit(1)

    results, seen = [], set()
    for r in raw:
        url = r.get("href", "")
        if not url or url in seen:  # 同一个网址只留一条
            continue
        seen.add(url)
        results.append({
            "index": len(results) + 1,
            "title": r.get("title", ""),
            "url": url,
            "abstract": r.get("body", ""),
        })
    print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
