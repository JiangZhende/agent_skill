---
name: sales_report
description: 生成业务区域的销售分析报告。当用户要求"季度销售报告"、"区域销售分析"、"销售排名"时使用。
triggers:
  - 销售报告
  - 季度销售
  - 区域销售分析
test_only: true  # 数据为 Mock，仅用于测试和演示
---

# 销售报告生成

## 适用场景

用户要求生成某个区域、某个季度的销售分析报告时使用本 skill。

## Scripts

本 skill 提供以下脚本，通过 `run_script` 调用：

### fetch_sales

从销售数据仓拉取指定区域和季度的数据并做清洗。

**参数:**
- `region` (string, required): 区域名称，如"华南"、"华东"
- `quarter` (string, required): 季度，格式 YYYYQN，如 2025Q3
- `min_revenue` (int, optional, default=0): 过滤掉营收低于此值的记录

**输出:** JSON，包含 region、quarter、row_count、cleaned_rows 字段。退出码非零表示数据缺失。

### compute_metrics

基于清洗后的销售数据计算核心指标（总营收、Top 产品等）。

**参数:**
- `input_file` (string, required): fetch_sales 输出的引用，用 `@<上一步的 result_id>` 形式
- `top_n` (int, optional, default=10): 返回前 N 个产品

**输出:** JSON，包含 total_revenue、total_revenue_wan、product_count、top_products

## 执行流程

### Step 1: 拉取销售数据

调用 `execute_skill_script`，传入真实的 region 和 quarter 值：

```
execute_skill_script(
    skill_name="sales_report",
    script_path="scripts/fetch_sales.py",
    args=["--region", "华南", "--quarter", "2025Q3"]
)
```

如果返回的 Exit Code 非零（如 2），说明数据缺失，调用 `final_answer` 告知用户哪个区域哪个季度未查到数据，不要继续后续步骤。

成功时记下返回中的 Result ID（形如 r_xxxxxxxx），下一步要用。

### Step 2: 计算核心指标

方式 A（推荐，用 `@result_id` 引用上一步输出文件）：

```
execute_skill_script(
    skill_name="sales_report",
    script_path="scripts/compute_metrics.py",
    args=["--input_file", "@r_abc12345", "--top_n", "5"]
)
```

方式 B（用 `input` 直接传 JSON 字符串，适合数据量小的情况）：

```
execute_skill_script(
    skill_name="sales_report",
    script_path="scripts/compute_metrics.py",
    args=["--top_n", "5"],
    input="{上一步 stdout 的完整 JSON 内容}"
)
```

### Step 3: 汇总并返回

解析 Step 2 输出的 JSON 指标，调用 `final_answer` 返回结构化报告：
- 区域、季度
- 总营收（用 total_revenue_wan 字段，单位"万元"）
- Top 5 产品名称和营收

## 注意事项

- 所有数据处理走本 skill 的脚本，不要绕过 skill 用其他基础工具自行实现
- 用户未指定季度时，默认用最近一个完整季度，并在答案中明确说明
- 金额展示统一用"万元"，保留两位小数
