"""Web search tool via DuckDuckGo（无需 API Key）。"""
import json
from smolagents import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """在互联网上搜索信息，返回标题、链接和摘要。
    当任务需要查询实时信息、新闻、公开资料时使用。

    Args:
        query: 搜索关键词或问题
        max_results: 返回结果数量，默认 5
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return "[error] 请先安装: pip install ddgs"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"[error] 搜索失败: {e}"

    if not results:
        return f"未找到与 '{query}' 相关的结果"

    output = []
    for i, r in enumerate(results, 1):
        output.append({
            "index": i,
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        })

    return json.dumps(output, ensure_ascii=False, indent=2)
