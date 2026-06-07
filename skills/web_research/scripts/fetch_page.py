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
        soup = BeautifulSoup(resp.text, "html.parser")

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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--output_file", default=None,
                   help="把完整正文写入此文件路径（用于后续 summarize.py 处理），"
                        "不截断。不指定则直接输出到 stdout（限 4000 字符）。")
    args = p.parse_args()

    # bs4 优先（更稳定），trafilatura 作为补充
    text = _extract_with_bs4(args.url) or _extract_with_trafilatura(args.url)

    if not text or not text.strip():
        print(json.dumps({"url": args.url, "error": "无法提取正文", "content": ""}))
        sys.exit(1)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
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
