#!/usr/bin/env python3
"""Builds the performance tables and SVG charts of PARALLELISM_PROGRESS.md.

Reads the raw benchmark JSON that `entity_bench.py` (and the P0 A/B script)
wrote under .local/ (git-ignored, not published) and writes, under
docs/parallelism/:

- perf-data.csv: every run used, one line per run;
- p0-sequential.svg: P0 (sequential optimisations) against the Horizon
  baseline, MSPT;
- p13-islands.svg: P1.3, sequential pass (0 worker threads) against the
  island pass (6), MSPT;
- p11-scaling.svg: P1.1, MSPT of herds_8x250 by worker threads;
- p13-dense.svg: P1.3, dense crowds where the island pass falls back.

No plotting library: plain SVG, so the script runs with the standard library.

  py -3 tools/voxaryn/perf_charts.py
"""
import csv, json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / ".local" / "voxaryn-bench"
P0 = ROOT / ".local" / "entity-bench" / "ab_report.json"
OUT = ROOT / "docs" / "parallelism"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def runs(name):
    """(scenario, threads) -> [result dicts] of one entity_bench JSON."""
    d = load(BENCH / name)
    out = {}
    for r in d["results"]:
        out.setdefault((r["scenario"], r["threads"]), []).append(r)
    return d, out


def mean_sd(xs):
    return st.mean(xs), (st.stdev(xs) if len(xs) > 1 else 0.0)


# ---- SVG ------------------------------------------------------------------
COLORS = ["#8c8c8c", "#2f6fb0", "#d0802a", "#3a9a5b", "#9a4fb0", "#b03a3a"]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bar_chart(path, title, subtitle, groups, series, values, points=None, unit="ms / tick", note=None):
    """Grouped bars. values[g][s] = (mean, sd) or None; points[g][s] = [runs]."""
    w, h = 860, 460
    left, right, top, bottom = 70, 20, 70, 110
    pw, ph = w - left - right, h - top - bottom
    vmax = max((m + sd) for row in values for v in row if v for m, sd in [v])
    if points:
        vmax = max([vmax] + [p for row in points for ps in row if ps for p in ps])
    vmax = vmax * 1.15
    step = 5 if vmax > 20 else 2 if vmax > 8 else 1
    gw = pw / len(groups)
    bw = gw * 0.75 / len(series)
    y = lambda v: top + ph - v / vmax * ph
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
         f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{w}" height="{h}" fill="white"/>',
         f'<text x="{left}" y="26" font-size="16" font-weight="600">{esc(title)}</text>',
         f'<text x="{left}" y="46" fill="#555">{esc(subtitle)}</text>']
    t = 0.0
    while t <= vmax:
        o.append(f'<line x1="{left}" x2="{w - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="#e4e4e4"/>')
        o.append(f'<text x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end" fill="#555">{t:g}</text>')
        t += step
    o.append(f'<text x="18" y="{top + ph / 2}" transform="rotate(-90 18 {top + ph / 2})" text-anchor="middle" fill="#333">{esc(unit)}</text>')
    for gi, g in enumerate(groups):
        x0 = left + gi * gw + gw * 0.125
        for si, _ in enumerate(series):
            v = values[gi][si]
            if not v:
                continue
            m, sd = v
            x = x0 + si * bw
            o.append(f'<rect x="{x:.1f}" y="{y(m):.1f}" width="{bw - 2:.1f}" height="{y(0) - y(m):.1f}" fill="{COLORS[si]}"/>')
            o.append(f'<text x="{x + bw / 2 - 1:.1f}" y="{y(m) - 6:.1f}" text-anchor="middle" font-size="11">{m:.1f}</text>')
            if sd > 0:
                cx = x + bw / 2 - 1
                o.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(m - sd):.1f}" y2="{y(m + sd):.1f}" stroke="#222"/>')
            if points and points[gi][si]:
                for p in points[gi][si]:
                    o.append(f'<circle cx="{x + bw / 2 - 1:.1f}" cy="{y(p):.1f}" r="2.6" fill="white" stroke="#222"/>')
        o.append(f'<text x="{left + gi * gw + gw / 2:.1f}" y="{top + ph + 18}" text-anchor="middle">{esc(g)}</text>')
    o.append(f'<line x1="{left}" x2="{w - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="#333"/>')
    lx = left
    for si, s in enumerate(series):
        o.append(f'<rect x="{lx}" y="{h - 62}" width="12" height="12" fill="{COLORS[si]}"/>')
        o.append(f'<text x="{lx + 17}" y="{h - 52}">{esc(s)}</text>')
        lx += 17 + 7.2 * len(s) + 24
    if note:
        for i, line in enumerate(note.split("\n")):
            o.append(f'<text x="{left}" y="{h - 30 + i * 15}" fill="#555" font-size="11">{esc(line)}</text>')
    o.append("</svg>")
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []

    # ---- P0: Horizon baseline against P0, sequential ------------------------
    p0 = load(P0)
    scen = ["cows_500", "cows_1000", "cows_2000", "cows_spread", "zombies_2000", "mixed"]
    vals, pts = [], []
    for s in scen:
        r = p0["scenarios"][s]["runs"]
        pre, post = [x["avg"] for x in r["pre"]], [x["avg"] for x in r["post"]]
        vals.append([mean_sd(pre), mean_sd(post)])
        pts.append([pre, post])
        for k, xs in (("horizon-baseline", pre), ("p0", post)):
            for i, x in enumerate(xs):
                rows.append(["P0 A/B", "ab_report.json", s, k, 0, i + 1, x, "", ""])
    bar_chart(OUT / "p0-sequential.svg",
              "P0: sequential entity-tick optimisations",
              f"MSPT (avg of /tick query), {p0['reps']} fresh servers per bar, {p0['warmup_s']} s warm-up, {p0['measure_s']} s window, i5-12400F",
              scen, ["Horizon baseline", "P0 (sequential)"], vals, pts,
              note="zombies_2000 and mixed evolve over time: the faster build covers more game ticks in the same window,\n"
                   "so their gain is partly a different workload (see docs/research/p0-benchmarks.md, normalised -19.6% / -10.1%).")

    # ---- P1.3: sequential pass against islands --------------------------------
    d, r = runs("p13_islands_horizon-p13-par.json")
    scen = ["herds_8x250", "mixedv_8x250", "species_land_8x250"]
    vals, pts = [], []
    for s in scen:
        t0 = [x["ms_per_tick"] for x in r[(s, 0)]]
        t6 = [x["ms_per_tick"] for x in r[(s, 6)]]
        vals.append([mean_sd(t0), mean_sd(t6)])
        pts.append([t0, t6])
        for t, xs, rs in ((0, t0, r[(s, 0)]), (6, t6, r[(s, 6)])):
            for i, (x, rr) in enumerate(zip(xs, rs)):
                rows.append(["P1.3 islands", "p13_islands_horizon-p13-par.json", s, "p1.3", t, i + 1, x,
                             rr.get("parallel_passes", ""), rr.get("passes", "")])
    ticks = d["args"]["ticks"]
    bar_chart(OUT / "p13-islands.svg",
              "P1.3: sequential pass vs. entity islands (same binary)",
              f"MSPT over /tick sprint {ticks}, 2 fresh servers per bar (dots), 8 independent arenas of ~250 mobs, i5-12400F 6C/12T",
              scen, ["0 worker threads (sequential P0 path)", "6 worker threads (islands)"], vals, pts,
              note="Built before the P1.3.2 synchronous-damage change and its hotfix: not re-measured since.\n"
                   "Speed-up: herds x1.31, mixedv x1.27, species_land x1.41 (means of 2 runs; run-to-run spread 10-20%).")

    # ---- P1.3: dense crowds (fall back) -------------------------------------
    d, r = runs("p13_dense_horizon-p13-par.json")
    scen = ["cows_2000", "pen_2000_tight"]
    vals, pts = [], []
    for s in scen:
        t0 = [x["ms_per_tick"] for x in r[(s, 0)]]
        t6 = [x["ms_per_tick"] for x in r[(s, 6)]]
        vals.append([mean_sd(t0), mean_sd(t6)])
        pts.append([t0, t6])
        for t, xs, rs in ((0, t0, r[(s, 0)]), (6, t6, r[(s, 6)])):
            for i, (x, rr) in enumerate(zip(xs, rs)):
                rows.append(["P1.3 dense", "p13_dense_horizon-p13-par.json", s, "p1.3", t, i + 1, x,
                             rr.get("parallel_passes", ""), rr.get("passes", "")])
    bar_chart(OUT / "p13-dense.svg",
              "P1.3: dense crowds (no parallel pass)",
              "MSPT over /tick sprint 600, max_entity_cramming=0, 2 fresh servers per bar",
              scen, ["0 worker threads", "6 worker threads"], vals, pts,
              note="0 parallel passes in every 6-thread run (cows_2000: unverified background species / one island;\n"
                   "pen_2000_tight: movement bound refused). Differences are within this machine's run-to-run noise.")

    # ---- P1.1: thread scaling -------------------------------------------------
    d, r = runs("p11_scale.json")
    d0, r0 = runs("p11_final_ab.json")
    threads = [1, 2, 4, 6, 12]
    groups = ["0 (seq.)*"] + [str(t) for t in threads]
    t0 = [x["ms_per_tick"] for x in r0[("herds_8x250", 0)]]
    vals = [[mean_sd(t0)]] + [[mean_sd([x["ms_per_tick"] for x in r[("herds_8x250", t)]])] for t in threads]
    pts = [[t0]] + [[[x["ms_per_tick"] for x in r[("herds_8x250", t)]]] for t in threads]
    for i, x in enumerate(t0):
        rows.append(["P1.1 scaling", "p11_final_ab.json", "herds_8x250", "p1.1", 0, i + 1, x, "", ""])
    for t in threads:
        for i, rr in enumerate(r[("herds_8x250", t)]):
            rows.append(["P1.1 scaling", "p11_scale.json", "herds_8x250", "p1.1", t, i + 1, rr["ms_per_tick"],
                         rr.get("parallel_passes", ""), rr.get("passes", "")])
    bar_chart(OUT / "p11-scaling.svg",
              "P1.1: herds_8x250 by worker threads",
              "MSPT over /tick sprint 1000, 2 fresh servers per bar, i5-12400F (6 cores / 12 threads)",
              groups, ["herds_8x250"], vals, pts,
              note="* the sequential reference comes from p11_final_ab.json (same binary, same day, separate session).\n"
                   "1 worker = the island path's overhead alone. P1.1 build: before the P1.2 safety checks and P1.3.")

    with (OUT / "perf-data.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["series", "source_json", "scenario", "build", "worker_threads", "run", "ms_per_tick",
                    "parallel_passes", "passes"])
        w.writerows(rows)
    print(f"wrote {len(rows)} runs and 4 charts to {OUT}")


if __name__ == "__main__":
    main()
