#!/usr/bin/env python
"""
对网页正文做提取式精炼：把正文切成小块，按与查询的相关性打分，
只保留相关的块（保持原文顺序），并丢掉导航、链接列表这类噪声。
"""
import argparse
import json
import re
import sys
from pathlib import Path

_CJK = r"\u3400-\u9fff"
_BLOCK_CHARS = 200      # 短行合并成块的目标大小
_PROSE_LINE = 40        # 超过这个长度的行视为一段正文，单独成块
_KEEP_RATIO = 0.35      # 分数低于最高分这个比例的块不要
_LIST_RUN = 6           # 连续这么多行"很短且不含数字"的行，视为链接列表


def _tokens(s: str) -> set[str]:
    """英文和数字按词切，中日韩文字按相邻两字切（没有分词器时最稳妥的做法）。"""
    s = s.lower()
    toks = set(re.findall(r"[a-z0-9]+", s))
    for run in re.findall(f"[{_CJK}]+", s):
        if len(run) == 1:
            toks.add(run)
        toks.update(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def _is_plain_short(line: str) -> bool:
    """很短、不含数字和句读的行：单看可能是标题、标签，也可能是一个导航链接。"""
    return len(line) <= 10 and not re.search(r"[\d，。、,.:：;；!?！？]", line)


def _blocks(text: str) -> list[tuple[list[str], bool]]:
    """把正文切成 (行列表, 是否链接列表) 的块。

    - 连续 _LIST_RUN 行以上的"很短且不含数字"的行，视为导航或链接列表（如"北京天气 / 上海天气 / …"）
    - 其余内容里，长行单独成块，连续的短行合并成约 _BLOCK_CHARS 的块

    只对长行去重（页面里重复出现的整段文字）。短行不能去重：
    表格类数据里"多云""12~21°"这样的值本来就会重复，去掉就把数据弄错了。
    """
    seen, lines = set(), []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) >= _PROSE_LINE:
            if line in seen:
                continue
            seen.add(line)
        lines.append(line)

    # 先标出链接列表所在的行
    in_list = [False] * len(lines)
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _is_plain_short(lines[j]):
            j += 1
        if j - i >= _LIST_RUN:
            in_list[i:j] = [True] * (j - i)
        i = max(j, i + 1)

    blocks, cur, size, cur_list = [], [], 0, False

    def flush():
        nonlocal cur, size
        if cur:
            blocks.append((cur, cur_list))
        cur, size = [], 0

    for line, is_list in zip(lines, in_list):
        if is_list != cur_list:
            flush()
            cur_list = is_list
        if not is_list and len(line) >= _PROSE_LINE:
            flush()
            blocks.append(([line], False))
            continue
        cur.append(line)
        size += len(line)
        if size >= _BLOCK_CHARS:
            flush()
    flush()
    return blocks


def _score(lines: list[str], is_list: bool, query_tokens: set[str], position: float) -> float:
    text = " ".join(lines)
    hit = len(query_tokens & _tokens(text)) / len(query_tokens) if query_tokens else 0.0
    # 含数字的行多，说明是数据而不是导航；靠前的内容略微优先
    digit_ratio = sum(1 for l in lines if re.search(r"\d", l)) / len(lines)
    score = hit + 0.3 * min(digit_ratio, 0.5) * 2 + 0.2 * (1 - position)
    return score * (0.2 if is_list else 1.0)


def extract_relevant(text: str, query: str, max_chars: int) -> tuple[str, dict]:
    blocks = _blocks(text)
    if not blocks:
        return "", {}
    query_tokens = _tokens(query or "")
    n = len(blocks)
    scored = [(i, lines, _score(lines, is_list, query_tokens, i / n)) for i, (lines, is_list) in enumerate(blocks)]
    best = max(s for _, _, s in scored)

    selected, total = set(), 0
    for i, lines, s in sorted(scored, key=lambda x: -x[2]):
        if s < best * _KEEP_RATIO:
            break
        size = sum(len(l) for l in lines) + len(lines)
        if total + size > max_chars:
            continue
        selected.add(i)
        total += size

    # 最相关的那一块本身就超长：截取它的开头
    if not selected:
        top = "\n".join(max(scored, key=lambda x: x[2])[1])[:max_chars]
        cut = max(top.rfind(c) for c in "。！？.!?\n")
        return (top[:cut + 1] if cut > max_chars // 2 else top), {"note": "最相关的段落过长，只取了开头"}

    return "\n\n".join("\n".join(blocks[i][0]) for i in sorted(selected)), {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--content", help="直接传入文本内容")
    p.add_argument("--content_file", help="从文件读取内容（支持 @workspace: 引用解析后的路径）")
    p.add_argument("--query", required=True, help="原始搜索查询，用于相关性评分")
    p.add_argument("--max_chars", type=int, default=1500, help="精炼后最大字符数")
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
    summary, meta = extract_relevant(text, args.query, args.max_chars)
    result = {
        "query": args.query,
        "original_chars": original_chars,
        "summary_chars": len(summary),
        "compressed": len(summary) < original_chars,
        "summary": summary,
    }
    if meta:
        result["note"] = meta.get("note")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
