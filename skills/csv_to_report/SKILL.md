---
name: csv_to_report
description: 把用户上传的 CSV 数据转换为格式化 Excel 报表。当用户上传 CSV 文件并要求"生成报表"、"做成 Excel"、"导出报告"时使用。
triggers:
  - 生成报表
  - 转 Excel
  - 数据导出
---

# CSV 转报表

## 适用场景

用户上传一个 CSV 文件（客户清单、销售记录、库存等），要求转成一份带格式的 Excel 报表。

## Scripts

### inspect_csv

预览 CSV 文件结构，返回列名、行数、前几行样本。**用于了解数据后才能决定如何生成报表。**

**参数:**
- `csv_path` (string, required): CSV 文件路径，通常用 `@input:<filename>` 引用用户上传的文件

**输出:** JSON，包含 columns、row_count、sample_rows 字段

### generate_excel

把 CSV 转为带格式的 Excel 报表（自动调整列宽、表头加粗）。

**参数:**
- `csv_path` (string, required): 输入 CSV 路径，可用 `@input:<filename>` 引用
- `output_path` (string, required): 输出文件路径，用 `@output:<filename>` 形式（如 `@output:report.xlsx`）
- `title` (string, optional): 报表标题，会写到 Excel 第一行

**输出:** JSON，包含生成的文件路径和大小

## 执行流程

### Step 1: 确认有哪些输入文件

调用 `list_inputs` 查看用户上传了什么 CSV 文件。

### Step 2: 预览 CSV 结构

```
execute_skill_script(
    skill_name="csv_to_report",
    script_path="scripts/inspect_csv.py",
    args=["--csv_path", "@input:<用户上传的文件名>"]
)
```

了解列名和数据规模，方便给报表起个有意义的标题。

### Step 3: 生成 Excel

```
execute_skill_script(
    skill_name="csv_to_report",
    script_path="scripts/generate_excel.py",
    args=[
        "--csv_path", "@input:<用户上传的文件名>",
        "--output_path", "@output:report.xlsx",
        "--title", "根据数据内容总结的标题，如'客户清单 - 2025Q3'"
    ]
)
```

### Step 4: 返回结果

调用 `final_answer` 告知用户报表已生成，包含文件名和数据行数。

## 注意事项

- 不要 cat / 读取整个 CSV 内容到 stdout，会爆 context。inspect_csv 已经处理好了采样
- 输出文件统一用 `@output:xxx.xlsx` 引用，框架会持久化到用户指定的 outputs 目录
