#!/usr/bin/env python
"""抓取网页正文，优先使用 trafilatura，回退到 requests + 简单 HTML 清洗。"""
import argparse
import json
import re
import sys
from pathlib import Path


def _extract_with_bs4(url: str) -> str | None:
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        # 响应头没声明 charset 时，requests 会按 ISO-8859-1 解码，中文页面变成乱码。
        # 这时把原始字节交给 BeautifulSoup，由它按 <meta charset> 和内容探测编码。
        declared = "charset" in resp.headers.get("content-type", "").lower()
        soup = BeautifulSoup(resp.text if declared else resp.content, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # 优先取主内容区域
        main = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id="mw-content-text")   # Wikipedia
            or soup.find(class_=re.compile(r"(article|content|post|entry)-?(body|text|main)?", re.I))
            or soup.find("body")
        )
        text = (main or soup).get_text(separator="\n", strip=True)
        return text
    except Exception:
        pass
    return None


def _extract_with_trafilatura(url: str) -> str | None:
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
            return text or None
    except Exception:
        pass
    return None


def _fetch(url: str) -> str | None:
    # bs4 优先（更稳定），trafilatura 作为补充
    text = _extract_with_bs4(url) or _extract_with_trafilatura(url)
    if not text or not text.strip():
        return None
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _refined(urls: list[str], query: str, max_chars: int) -> int:
    """抓取一个或多个页面并直接返回与 query 相关的内容，省掉单独的精炼步骤。"""
    from concurrent.futures import ThreadPoolExecutor

    from summarize import extract_relevant  # 同目录的 summarize.py

    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        texts = list(pool.map(_fetch, urls))

    pages = []
    for url, text in zip(urls, texts):
        if text is None:
            pages.append({"url": url, "error": "无法提取正文"})
            continue
        summary, _ = extract_relevant(text, query, max_chars)
        pages.append({"url": url, "original_chars": len(text), "content": summary})

    print(json.dumps({"query": query, "pages": pages}, ensure_ascii=False, indent=2))
    return 0 if any("content" in p for p in pages) else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, action="append",
                   help="要抓取的网页地址；配合 --query 时可以重复传入多个，并行抓取")
    p.add_argument("--query", default=None,
                   help="用户的问题或搜索词。传入后直接返回各页面中与之相关的内容（推荐）")
    p.add_argument("--max_chars", type=int, default=1500, help="配合 --query：每个页面最多返回的字符数")
    p.add_argument("--output_file", default=None,
                   help="把完整正文写入此文件路径（用于后续 summarize.py 处理），"
                        "不截断。不指定则直接输出到 stdout（限 4000 字符）。")
    args = p.parse_args()

    if args.query:
        sys.exit(_refined(args.url, args.query, args.max_chars))
    if len(args.url) > 1:
        print(json.dumps({"error": "一次抓取多个 URL 时必须同时传 --query"}, ensure_ascii=False))
        sys.exit(1)
    args.url = args.url[0]

    text = _fetch(args.url)

    if not text:
        print(json.dumps({"url": args.url, "error": "无法提取正文", "content": ""}))
        sys.exit(1)

    char_count = len(text)
    if args.output_file:
        # 写完整内容到文件，供 summarize.py 读取，不截断
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "url": args.url,
            "char_count": char_count,
            "content": text,
        }, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({
            "url": args.url,
            "char_count": char_count,
            "output_file": str(out),
            "note": "完整正文已写入 output_file，请用 summarize.py 精炼后再使用",
        }, ensure_ascii=False, indent=2))
    else:
        # 直接输出时限制 4000 字符（stdout 传给模型）
        limit = 4000
        truncated = char_count > limit
        print(json.dumps({
            "url": args.url,
            "char_count": char_count,
            "truncated": truncated,
            "content": text[:limit] + ("…（内容过长，建议用 --output_file + summarize.py 精炼）" if truncated else ""),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
