#!/usr/bin/env python3
"""Markdown tables from entity_bench.py result files.

  py -3 tools/voxaryn/entity_tables.py .local/voxaryn-bench/p11_*.json > tables.md

Groups runs by (binary, scenario, threads); MSPT is the sprint's ms per tick,
mean ± sample standard deviation over the repetitions. The speedup column is
against threads 0 of the same binary and scenario (the sequential P0 pass).
"""
import glob, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path


def ms(v):
    return f"{st.fmean(v):.2f}" + (f" ± {st.stdev(v):.2f}" if len(v) > 1 else "")


def mean(rs, k):
    v = [r[k] for r in rs if r.get(k) is not None]
    return st.fmean(v) if v else None


def f(v, fmt="{:.2f}"):
    return "—" if v is None else fmt.format(v)


def main(paths):
    runs = []
    for p in paths:
        for g in glob.glob(p):
            d = json.loads(Path(g).read_text(encoding="utf-8"))
            for r in d["results"]:
                r.setdefault("exe", Path(d["exe"]).name)
                r["_file"] = Path(g).name
                runs.append(r)
    errors = [r for r in runs if "error" in r]
    by = defaultdict(list)
    for r in runs:
        if "error" not in r:
            by[(r["exe"], r["scenario"], r["threads"])].append(r)
    print("| binary | scenario | threads | n | MSPT, ms | × vs 0 | pass run, ms | work, ms | critical, ms | "
          "busy_max, ms | islands | largest | split+merge, ms | est × | parallel | fallbacks | rules ok |")
    print("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|")
    for (exe, sc, t) in sorted(by, key=lambda k: (k[0], k[1], k[2])):
        rs = by[(exe, sc, t)]
        m = [r["ms_per_tick"] for r in rs]
        base = by.get((exe, sc, 0))
        sp = f"{st.fmean(r['ms_per_tick'] for r in base) / st.fmean(m):.2f}" if base else "—"
        sm = None
        if mean(rs, "split_ms") is not None:
            sm = mean(rs, "split_ms") + mean(rs, "merge_ms")
        par = f"{sum(r.get('parallel_passes', 0) for r in rs)}/{sum(r.get('passes', 0) for r in rs)}" \
            if any("passes" in r for r in rs) else "—"
        fb = ",".join(sorted({x for r in rs for x in r.get("fallbacks", [])})) or "—"
        ok = "yes" if all(r.get("rules_ok") for r in rs) else ("?" if any("rules_ok" not in r for r in rs) else "NO")
        print(f"| {exe} | {sc} | {t} | {len(rs)} | {ms(m)} | {sp} | {f(mean(rs, 'run_ms'))} | "
              f"{f(mean(rs, 'work_ms'))} | {f(mean(rs, 'critical_ms'))} | {f(mean(rs, 'busy_max_ms'))} | "
              f"{f(mean(rs, 'islands'), '{:.1f}')} | {f(mean(rs, 'largest'), '{:.0f}')} | {f(sm)} | "
              f"{f(mean(rs, 'est'))} | {par} | {fb} | {ok} |")
    if errors:
        print("\nErrors:")
        for r in errors:
            print(f"- {r['_file']} {r['scenario']} threads {r['threads']}: {r['error'][:160]}")


if __name__ == "__main__":
    main(sys.argv[1:] or [str(Path(__file__).resolve().parents[2] / ".local" / "voxaryn-bench" / "*.json")])
