---
name: web_research
description: 深度网络调研：搜索 → 判断 URL 价值 → 抓取正文 → 提炼相关内容。当用户需要查询实时信息、深入了解某个话题、需要网页正文内容时使用。
triggers:
  - 网络搜索
  - 查一下
  - 搜索资料
  - 调研
  - 最新信息
  - 网页内容
---

# Web 深度调研

## 适用场景

需要从互联网获取信息，且摘要不够用、需要网页正文内容时使用。

## Scripts

### search

DuckDuckGo 搜索，返回每条结果的 title、url、abstract。

**参数:**
- `query` (string, required): 搜索关键词
- `max_results` (int, optional, default=8): 返回条数

**输出:** JSON，包含 results 列表，每项有 index / title / url / abstract

### fetch_page

抓取指定 URL 的网页正文，过滤导航栏、广告等噪声。

**参数:**
- `url` (string, required): 要抓取的网页地址
- `output_file` (string, optional): 写入完整正文的文件路径，用 `@workspace:<name>.json` 引用。指定此参数时不截断，供后续 summarize.py 处理。不指定时直接输出（限 4000 字符）。

**输出:** JSON，包含 url / char_count / output_file（或 content）

### summarize

对网页正文做提取式精炼：按与查询的相关性对段落打分，保留最相关段落，在段落边界处截止，不盲目截断。

**参数:**
- `content_file` (string, required): fetch_page 写入的文件路径，用 `@workspace:<name>.json` 引用
- `query` (string, required): 原始搜索查询，用于相关性评分
- `max_chars` (int, optional, default=3000): 精炼后最大字符数

**输出:** JSON，包含 summary / original_chars / summary_chars / compressed

## 执行流程

### Step 1: 搜索

```
execute_skill_script(
    skill_name="web_research",
    script_path="scripts/search.py",
    args=["--query", "<搜索词>", "--max_results", "8"]
)
```

### Step 2: 判断哪些 URL 值得抓取

根据 abstract 判断相关性，选出 **1-3 个**最值得深入阅读的 URL。判断标准：
- abstract 与用户问题直接相关
- 来源可信（官网、权威媒体、学术）
- 不是论坛、问答、广告页面

若 abstract 已足够回答用户问题，可直接跳到 Step 5。

### Step 3: 抓取正文（写入 workspace 文件）

对每个选定的 URL，把完整正文写入 workspace 文件：

```
execute_skill_script(
    skill_name="web_research",
    script_path="scripts/fetch_page.py",
    args=["--url", "<url>", "--output_file", "@workspace:page1.json"]
)
```

### Step 4: 精炼内容

**前提：Step 3 必须成功**（返回 `output_file` 字段，exit code 0）。若 Step 3 失败或返回 error，跳过该 URL，不要调用 summarize.py。

对每个抓取的文件，用 summarize.py 提取相关段落：

```
execute_skill_script(
    skill_name="web_research",
    script_path="scripts/summarize.py",
    args=["--content_file", "@workspace:page1.json",
          "--query", "<原始搜索词>",
          "--max_chars", "3000"]
)
```

读取返回的 `summary` 字段作为该页面的有效内容。

### Step 5: 综合回答

基于精炼后的内容，通过 `final_answer` 给出综合回答，注明信息来源 URL。

## 注意事项

- fetch_page 如果返回 error，跳过该 URL 换下一个
- summarize 的 `compressed: false` 表示原文本身不长，直接用 `summary` 即可
- workspace 文件名每个 URL 用不同名称，如 page1.json、page2.json
