"""The scale study's tables, from the raw results in results/scale/: seed
sizes and times, single-request latency per data size (with the growth from
one size to the next), throughput per size and worker count, the hot-row
writes and the background sweeps. docs/SCALING.md quotes them.

    python -m scripts.bench.scale_report > scripts/bench/results/scale/SUMMARY.md
"""
from __future__ import annotations

import json
import os

DIR = os.path.join(os.path.dirname(__file__), "results", "scale")
SIZES = [("s0", "S0 (bench)"), ("s1", "S1 (10x)"), ("s2", "S2 (lakh clubs)")]


def load(name: str):
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def ms(v) -> str:
    if v is None or v != v:
        return "—"
    if v >= 10000:
        return f"{v / 1000:,.0f} s"
    if v >= 1000:
        return f"{v / 1000:,.1f} s"
    if v >= 100:
        return f"{v:,.0f}"
    return f"{v:,.1f}"


def dur(v) -> str:
    """A duration in ms, with its unit."""
    out = ms(v)
    return out if out.endswith(" s") or out == "—" else out + " ms"


def gib(b) -> str:
    return f"{b / 2**30:.2f}"


def seeds() -> None:
    rows = [(label, load(f"{k}_seed.json")) for k, label in SIZES]
    rows = [(label, s) for label, s in rows if s]
    if not rows:
        return
    print("### Data sizes\n")
    print("| | " + " | ".join(label for label, _ in rows) + " |")
    print("|---|" + "---:|" * len(rows))
    for key in ("clubs", "upcoming", "past", "users", "registrations", "follows", "threads"):
        print(f"| {key} | " + " | ".join(f"{s['counts'][key]:,}" for _, s in rows) + " |")
    print("| cap on one event's registrations | " + " | ".join(f"{s['max_event_regs']:,}" for _, s in rows) + " |")
    print("| database size (GiB) | " + " | ".join(gib(s["database_bytes"]) for _, s in rows) + " |")
    for t in ("event_registrations_link", "events", "users", "club_users_link", "user_interests"):
        print(f"| {t} heap + indexes (GiB) | " + " | ".join(
            f"{gib(s['tables'][t]['heap_bytes'])} + {gib(s['tables'][t]['index_bytes'])}" for _, s in rows) + " |")
    print("| registrations bytes per row (all indexes) | " + " | ".join(
        f"{s['tables']['event_registrations_link']['bytes_per_row']:.0f}" for _, s in rows) + " |")
    print("| seed time (s) | " + " | ".join(f"{s['seconds'].get('vacuum', 0):,.0f}" for _, s in rows) + " |")
    print("| of which index builds (s) | " + " | ".join(f"{sum(s['index_build_seconds'].values()):,.0f}" for _, s in rows) + " |")
    print()
    last = rows[-1][1]
    slow = sorted(last["index_build_seconds"].items(), key=lambda kv: -kv[1])[:6]
    print(f"Slowest index builds at {rows[-1][0]}: " + ", ".join(f"`{k}` {v:.0f} s" for k, v in slow) + ".\n")


def single() -> None:
    runs = [(label, load(f"{k}_run.json")) for k, label in SIZES]
    runs = [(label, r) for label, r in runs if r]
    if not runs:
        return
    names: list[str] = []
    for _, r in runs:
        for row in r["results"]:
            if row["name"] not in names:
                names.append(row["name"])
    print("### One request at a time (in-process; p50 / p95 ms, SQL statements)\n")
    print("| endpoint | " + " | ".join(f"{label} p50 / p95 | stmts" for label, _ in runs) + " | growth S0→last (p50) |")
    print("|---|" + "---:|---:|" * len(runs) + "---:|")
    for name in names:
        cells, p50s = [], []
        for _, r in runs:
            row = next((x for x in r["results"] if x["name"] == name), None)
            if row is None:
                cells.append("— | —")
                p50s.append(None)
                continue
            if "unsupported" in row:
                cells.append(f"not offered ({row['unsupported'][:3]}) | —")
                p50s.append(None)
                continue
            st = str(row["statements_min"]) if row["statements_min"] == row["statements_max"] else f"{row['statements_min']}–{row['statements_max']}"
            cells.append(f"{ms(row['p50'])} / {ms(row['p95'])} | {st}")
            p50s.append(row["p50"])
        growth = f"{p50s[-1] / p50s[0]:.1f}×" if p50s and p50s[0] and p50s[-1] else "—"
        print(f"| {name} | " + " | ".join(cells) + f" | {growth} |")
    print()


def throughput() -> None:
    found = []
    for k, label in SIZES:
        for w in (1, 4):
            r = load(f"{k}_load_w{w}.json")
            if r:
                found.append((label, w, r))
    if not found:
        return
    print("### Throughput (closed loop, the realistic mix; served by uvicorn on this laptop)\n")
    print("| data | workers | clients | req/s | p50 ms | p95 ms | p99 ms | errors | server cores | Postgres cores (est.) | loop lag p99 ms |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, w, r in found:
        for lv in r["results"]:
            cpu = lv.get("cpu_cores_busy", {})
            lag = lv.get("server", {}).get("loop_lag_ms", {}).get("p99")
            print(f"| {label} | {w} | {lv['concurrency']} | {lv['rps']:.0f} | {ms(lv['latency_ms']['p50'])} | {ms(lv['latency_ms']['p95'])} | "
                  f"{ms(lv['latency_ms']['p99'])} | {lv['error_rate'] * 100:.1f}% | {cpu.get('server', float('nan')):.2f} | "
                  f"{cpu.get('postgres_est', float('nan')):.1f} | {ms(lag)} |")
    print()
    print("Per kind at the lowest concurrency (p50 / p95 ms):\n")
    kinds = ["home", "search", "event", "club", "foryou", "list"]
    print("| data | workers | " + " | ".join(kinds) + " |")
    print("|---|---:|" + "---:|" * len(kinds))
    for label, w, r in found:
        lv = r["results"][0]
        print(f"| {label} | {w} | " + " | ".join(
            f"{ms(lv['by_kind'][k]['p50'])} / {ms(lv['by_kind'][k]['p95'])}" if k in lv["by_kind"] else "—" for k in kinds) + " |")
    print()


def hot() -> None:
    rows = []
    for k, label in SIZES:
        for r in load(f"{k}_hot.json") or []:
            rows.append((label, r))
    if not rows:
        return
    print("### Writes on one hot row\n")
    print("| data | test | before | answered/s | p50 ms | p95 ms | p99 ms | errors | lock wait in FOR UPDATE p95 ms | most waiting on a lock | counter = rows |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for label, r in rows:
        before = r.get("registered_before", r.get("followers_before"))
        errs = sum(v for c, v in r["codes"].items() if c != "200")
        fu = r.get("server", {}).get("for_update_ms", {}).get("p95") if r["kind"] == "event" else None
        print(f"| {label} | {r['label']} | {before:,} | {r['throughput_per_s']} | {ms(r['p50_ms'])} | {ms(r['p95_ms'])} | {ms(r['p99_ms'])} | "
              f"{errs} | {ms(fu)} | {r['max_lock_waiters']} | {'yes' if r['counter_matches'] else 'NO'} |")
    print()


def sweeps() -> None:
    rows = [(label, load(f"{k}_sweeps.json")) for k, label in SIZES]
    rows = [(label, s) for label, s in rows if s]
    if not rows:
        return
    print("### Background work each web worker does\n")
    print("| data | advance_clock (every minute) | counters.drift (each boot, then every 6 h) | email sweep: events with journey emails → rows read, ms |")
    print("|---|---:|---:|---|")
    for label, s in rows:
        clock = min(x["ms"] for x in s["clock"])
        drift = min(x["ms"] for x in s["drift"])
        em = "; ".join(f"{e['events_with_templates']:,} → {e['runs'][-1]['rows']:,} rows, {dur(min(x['query_ms'] + x['loop_ms'] for x in e['runs']))}"
                       for e in s["email_sweep"])
        print(f"| {label} | {dur(clock)} | {dur(drift)} | {em} |")
    print()


if __name__ == "__main__":
    print("## Scale study: raw tables\n")
    print("Generated by `python -m scripts.bench.scale_report` from the JSON files beside this one.\n")
    seeds()
    single()
    throughput()
    hot()
    sweeps()
