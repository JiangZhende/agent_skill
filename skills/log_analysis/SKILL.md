---
name: log_analysis
description: 分析服务器日志文件，统计错误、提取关键事件。当用户要求"分析日志"、"查 error"、"统计访问量"时使用。
triggers:
  - 日志分析
  - 查日志
  - error 统计
---

# 日志分析

## 适用场景

用户上传日志文件，需要做错误统计、关键事件提取等分析。

## Scripts

### error_stats

统计日志文件中 ERROR / WARN / INFO 的分布，并返回高频错误模式。

**参数:**
- `log_path` (string, required): 日志文件路径，用 `@input:<filename>` 引用用户上传的文件
- `top_n` (int, optional, default=5): 返回前 N 个高频 ERROR 模式

**输出:** JSON，包含 total_lines、level_counts、top_error_patterns

### recent_events

提取日志末尾 N 行中的关键事件（ERROR / WARN / Exception / Failed / Timeout）。

**参数:**
- `log_path` (string, required): 日志文件路径，用 `@input:<filename>` 引用
- `tail_lines` (int, optional, default=100): 从末尾读取的行数

**输出:** JSON，包含 matched_events 数量和 events 列表

## 执行流程

### Step 1: 确认上传的日志文件

调用 `list_inputs` 查看用户上传了哪个日志文件。

### Step 2: 错误统计

```
execute_skill_script(
    skill_name="log_analysis",
    script_path="scripts/error_stats.py",
    args=["--log_path", "@input:<filename>", "--top_n", "10"]
)
```

### Step 3（可选）: 最近关键事件

如需了解最近发生了什么：

```
execute_skill_script(
    skill_name="log_analysis",
    script_path="scripts/recent_events.py",
    args=["--log_path", "@input:<filename>", "--tail_lines", "200"]
)
```

### Step 4: 汇总结论

通过 `final_answer` 返回：
- 错误总数和分级（ERROR / WARN）
- Top N 高频错误模式
- 给运维同学的处置建议

## 注意事项

- 不要把日志文件内容直接输出到 context，用脚本采样后返回结构化数据
- 大文件分析以 error_stats 为主，recent_events 只看末尾
