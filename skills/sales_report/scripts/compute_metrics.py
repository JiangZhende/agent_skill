#!/usr/bin/env python
"""根据 fetch_sales 输出计算指标。参数 --input_file / --top_n。"""
import argparse
import json


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input_file", required=True)
    p.add_argument("--top_n", type=int, default=10)
    args = p.parse_args()

    with open(args.input_file, encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("cleaned_rows", []) if isinstance(data, dict) else data
    if not rows:
        print(json.dumps({"total_revenue": 0, "product_count": 0, "top_products": []}, ensure_ascii=False))
        return

    total = sum(r.get("revenue", 0) for r in rows)
    top = sorted(rows, key=lambda r: -r.get("revenue", 0))[:args.top_n]
    metrics = {
        "total_revenue": total,
        "total_revenue_wan": round(total / 10000, 2),
        "product_count": len(rows),
        "top_products": [
            {"name": p["product_name"], "revenue": p["revenue"], "revenue_wan": round(p["revenue"] / 10000, 2)}
            for p in top
        ],
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
