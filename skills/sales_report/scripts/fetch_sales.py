#!/usr/bin/env python
"""拉取并清洗销售数据。参数通过 --region / --quarter 传递。"""
import argparse
import json
import sys


MOCK_SALES = [
    {"region": "华南", "product_id": "P001", "product_name": "产品A", "quarter": "2025Q3", "revenue": 1200000},
    {"region": "华南", "product_id": "P002", "product_name": "产品B", "quarter": "2025Q3", "revenue": 890000},
    {"region": "华南", "product_id": "P003", "product_name": "产品C", "quarter": "2025Q3", "revenue": 750000},
    {"region": "华东", "product_id": "P001", "product_name": "产品A", "quarter": "2025Q3", "revenue": 2100000},
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--quarter", required=True)
    p.add_argument("--min_revenue", type=int, default=0)
    args = p.parse_args()

    rows = [r for r in MOCK_SALES
            if r["region"] == args.region
            and r["quarter"] == args.quarter
            and r.get("revenue", 0) >= args.min_revenue]

    output = {
        "region": args.region,
        "quarter": args.quarter,
        "row_count": len(rows),
        "cleaned_rows": rows,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if not rows:
        print(f"[warn] 未查到 {args.region} {args.quarter} 的数据", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
