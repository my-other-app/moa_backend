"""A markdown table from run.py's JSON results, one column group per run.

    python -m scripts.bench.report scripts/bench/results/before.json scripts/bench/results/signing.json scripts/bench/results/after.json
"""
from __future__ import annotations

import json
import sys


def _ms(v: float) -> str:
    if v >= 10000:
        return f"{v / 1000:,.0f} s"
    if v >= 1000:
        return f"{v / 1000:,.1f} s"
    if v >= 100:
        return f"{v:,.0f}"
    return f"{v:,.1f}"


def main(paths: list[str]) -> None:
    runs = [json.load(open(p)) for p in paths]
    names: list[str] = []
    for run in runs:
        for row in run["results"]:
            if row["name"] not in names:
                names.append(row["name"])
    head = "| endpoint | " + " | ".join(f"{r['label']} p50 / p95 ms | stmts" for r in runs) + " |"
    rule = "|---|" + "|".join("---:|---:" for _ in runs) + "|"
    print(head)
    print(rule)
    for name in names:
        cells = []
        for run in runs:
            row = next((r for r in run["results"] if r["name"] == name), None)
            if row is None:
                cells.append("— | —")
                continue
            stmts = str(row["statements_min"]) if row["statements_min"] == row["statements_max"] else f"{row['statements_min']}–{row['statements_max']}"
            cells.append(f"{_ms(row['p50'])} / {_ms(row['p95'])} (n={row['n']}) | {stmts}")
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
