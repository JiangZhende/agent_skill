#!/usr/bin/env python
"""
对网页正文做提取式精炼：按与查询的相关性对段落打分，
保留最相关的段落直到达到 max_chars，保持原文顺序。
不盲目截断，确保返回内容在段落边界处结束。
"""
import argparse
import json
import re
import sys
from pathlib import Path


def _score(paragraph: str, query_words: set[str]) -> float:
    """简单词频打分：段落中出现的查询词数 / 段落总词数（归一化）。"""
    if not paragraph.strip():
        return 0.0
    words = set(re.findall(r"\w+", paragraph.lower()))
    if not words:
        return 0.0
    hit = len(query_words & words)
    # 额外加权：段落越短越可能是噪声，超过 20 词的段落才有意义
    length_bonus = min(len(words) / 50, 1.0)
    return (hit / len(query_words)) * length_bonus if query_words else length_bonus


def extract_relevant(text: str, query: str, max_chars: int) -> tuple[str, dict]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    query_words = set(re.findall(r"\w+", query.lower())) if query else set()

    # 每个段落打分
    scored = [(i, p, _score(p, query_words)) for i, p in enumerate(paragraphs)]

    # 按分数降序，贪心选段落
    selected_indices: set[int] = set()
    total = 0
    for i, p, _ in sorted(scored, key=lambda x: -x[2]):
        if total + len(p) > max_chars:
            # 还有空间但当前段落放不下时，尝试更短的段落
            continue
        selected_indices.add(i)
        total += len(p)
        if total >= max_chars * 0.9:
            break

    # 如果一个都没选上（所有段落都比 max_chars 长），取分数最高的前几句
    if not selected_indices and paragraphs:
        best_para = max(scored, key=lambda x: x[2])[1]
        excerpt = best_para[:max_chars].rsplit("。", 1)[0] + "。"
        return excerpt, {"note": "段落过长，取最相关段落前半部分"}

    # 按原文顺序还原
    result = "\n\n".join(paragraphs[i] for i in sorted(selected_indices))
    return result, {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--content", help="直接传入文本内容")
    p.add_argument("--content_file", help="从文件读取内容（支持 @workspace: 引用解析后的路径）")
    p.add_argument("--query", required=True, help="原始搜索查询，用于相关性评分")
    p.add_argument("--max_chars", type=int, default=3000, help="精炼后最大字符数")
    args = p.parse_args()

    if args.content:
        text = args.content
    elif args.content_file:
        path = Path(args.content_file)
        if not path.exists():
            print(json.dumps({
                "error": f"文件不存在: {path}",
                "hint": "请先成功执行 fetch_page.py --output_file 写入该文件，再调用 summarize.py",
            }))
            sys.exit(1)
        # 如果是 JSON 文件（fetch_page 输出），提取 content 字段
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            text = data.get("content", raw)
        except json.JSONDecodeError:
            text = raw
    else:
        print(json.dumps({"error": "必须提供 --content 或 --content_file"}))
        sys.exit(1)

    original_chars = len(text)
    if original_chars <= args.max_chars:
        print(json.dumps({
            "query": args.query,
            "original_chars": original_chars,
            "summary_chars": original_chars,
            "compressed": False,
            "summary": text,
        }, ensure_ascii=False, indent=2))
        return

    summary, meta = extract_relevant(text, args.query, args.max_chars)
    result = {
        "query": args.query,
        "original_chars": original_chars,
        "summary_chars": len(summary),
        "compressed": True,
        "summary": summary,
    }
    if meta:
        result["note"] = meta.get("note")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
