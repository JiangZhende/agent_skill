#!/usr/bin/env python
"""预览 CSV 文件结构。"""
import argparse
import csv
import json
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv_path", required=True)
    args = p.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        sample = []
        row_count = 0
        for i, row in enumerate(reader):
            if i < 5:
                sample.append(row)
            row_count += 1

    result = {
        "file": str(path),
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
