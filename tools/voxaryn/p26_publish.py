#!/usr/bin/env python3
"""Builds, verifies and (only on request) installs the P2.4-P2.6 publication data and charts. Stdlib only.

Inputs: raw benchmark JSON/logs under .local/voxaryn-bench/ (git-ignored, never published).
Outputs (docs/parallelism/): four CSV extracts and six SVG charts. The charts are drawn from the CSV
files only. Pinned in docs/parallelism/p26-manifest.json: the SHA-256 of every source file, the expected
row counts and the SHA-256 of every published output (all with LF line endings).

  py -3 tools/voxaryn/p26_publish.py                    # verify only: nothing in docs/ is written
  py -3 tools/voxaryn/p26_publish.py --tables           # also print the statistics used in the report
  py -3 tools/voxaryn/p26_publish.py --install          # replace the published files (transactional)
  py -3 tools/voxaryn/p26_publish.py --refresh-manifest # re-pin sources and outputs, then install
  py -3 tools/voxaryn/p26_publish.py --recover          # roll back an install that was interrupted

What a run does, in order; any failure stops it before a published file is touched:
 1. the manifest, the expected set of sources (from the fixed spec below: series, scenarios, rounds,
    modes) and the files found on disk must agree exactly: a missing, extra or modified source is an error;
 2. CSV extracts and charts are generated into a temporary directory;
 3. the prepared files are checked: row counts, unique run ids, ids == spec, headers, SVG well-formedness,
    every CSV value against its source file, and (unless --refresh-manifest) the SHA-256 of every output
    against the manifest and the published files against the prepared ones;
 4. only with --install or --refresh-manifest: the files are copied over the published ones with a backup
    and a journal; if any replacement fails, or the process dies, the old set is restored (--recover).

Verified automatically against the sources: scenario, round and mode (file name and the run's threads and
HORIZON_ENTITY_ADAPTIVE setting), sprint ticks, sprint MSPT, steady avg/p50/p95/p99 and query count, redo
(nav_redone), block changes, parallel and total passes, adaptive switches, probes and skipped passes,
steady-phase counters, mob counts, alarm and mismatch counters, every VERIFY/fault-injection field, and the
PARSTATS timeline (windows re-parsed from the logs with separate patterns, compared per window and as sums).
NOT verified automatically: the statistics typed into the Markdown reports (means, SDs, intervals; reproduce
them with --tables), the shape of the SVG drawings beyond "regenerated from the pinned CSV is byte-identical
and well-formed", and anything about the benchmark methodology itself.

Series (directories under .local/voxaryn-bench/):
  p26b   final P2.6 series: sprint MSPT + steady p95 (lambs scenes, 6 rounds, modes A/B/C rotated)
  p26bc  conflict phase: 20 TPS from the start, steady p95 while the lambs still eat (5 rounds)
  p26bd  two dimensions in one server tick (3 rounds)
  p26bk  no-conflict controls on the final build: herds_8x250, species_land_8x250 (6 rounds)
  p26bv, p25v, p24v, p251v, p251perf/verify_*  VERIFY and fault-injection runs
  p25abba (P2.4 vs P2.5), p251perf (P2.5 vs P2.5.1)
Not used for any chart: p26 and p26_v1 (earlier P2.6 builds, steady p95 was measured after the block
count had edited the world), p26bd_aborted_order1 (arena build order bug), p26v (earlier build verify).
"""
import csv, hashlib, json, os, re, shutil, statistics as st, sys, tempfile
import xml.dom.minidom
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from perf_charts import bar_chart, esc, COLORS  # noqa: E402

ROOT = HERE.parents[1]
BENCH = ROOT / ".local" / "voxaryn-bench"
OUT = ROOT / "docs" / "parallelism"
MANIFEST = OUT / "p26-manifest.json"
MODE = {"A": "sequential", "B": "parallel_adaptive_off", "C": "parallel_adaptive_on"}
MODE_LABEL = {"A": "A: sequential", "B": "B: parallel, adaptive off", "C": "C: parallel + adaptive"}
# two-sided 95% t quantiles by degrees of freedom (n - 1)
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}

# ---- the accepted set, fixed in code (independent of the manifest and of what is on disk) ----------
# (series label, directory, scenarios, rounds, sprint MSPT meaningful)
P26_SPEC = [
    ("p26-final", "p26b", ["lambs_shortgrass_8x250", "lambs_patch_8x250", "lambs_grass_8x250"], 6, True),
    ("p26-conflict-phase", "p26bc", ["lambs_shortgrass_8x250", "lambs_patch_8x250"], 5, False),
    ("p26-two-dimensions", "p26bd", ["twodim_shortgrass_herds", "twodim_patch_herds"], 3, False),
    ("p26-control", "p26bk", ["herds_8x250", "species_land_8x250"], 6, True),
]
EXPECTED_ROWS = {"p26-data.csv": 138, "p24-p251-data.csv": 107, "p24-p26-verify.csv": 64, "p26-timeline.csv": 602}
EXPECTED_P26_SERIES = {"p26-final": 54, "p26-conflict-phase": 30, "p26-two-dimensions": 18, "p26-control": 36}
EXPECTED_AB_SERIES = {"p24-vs-p25": 48, "p25-vs-p251": 32, "p251-block-change-cost": 18, "p251-forced-redo": 9}
EXPECTED_VERIFY_SERIES = {"p24-verify": 10, "p25-verify": 10, "p251-verify": 19, "p26-verify": 25}
TIMELINE_SCENE = "lambs_patch_8x250"
TIMELINE_LOGS = [f"p26b/{TIMELINE_SCENE}_r{r}_{m}.log" for r in range(1, 7) for m in "BC"]
OUTPUTS = ["p26-data.csv", "p24-p251-data.csv", "p24-p26-verify.csv", "p26-timeline.csv",
           "p26-mspt.svg", "p26-adaptive-gain.svg", "p26-redo-skips.svg", "p26-p95.svg", "p26-timeline.svg",
           "p26-two-dimensions.svg"]
# source groups found on disk by pattern; only used to detect files outside the accepted set
AB_GROUPS = [("p25abba", "*.json"), ("p251perf", "ab_*.json"), ("p251perf", "fb_*.json"), ("p251perf", "forced_herds_*.json")]
VERIFY_DIRS = [("p24-verify", "p24v", "*.json"), ("p25-verify", "p25v", "*.json"), ("p251-verify", "p251v", "*.json"),
               ("p251-verify", "p251perf", "verify_*.json"), ("p26-verify", "p26bv", "*.json")]


def expected_ab_ids():
    """Every A/B source of the accepted set, built from the run design (not from a directory listing)."""
    ids = []
    for sc in ("herds_8x250", "species_land_8x250"):  # P2.5 (A) vs P2.5.1 (B), ABBA, 4 blocks
        ids += [f"p251perf/ab_{sc}_b{b}_{i}_{v}.json" for b in range(1, 5) for i, v in enumerate("ABBA", 1)]
    for sc, t in (("herds_8x250", 8), ("pen_2000_tight", 0)):  # P2.4 (A) vs P2.5 (B), ABBA blocks b1-b4
        ids += [f"p25abba/{sc}_t{t}_b{b}_{i}_{v}.json" for b in range(1, 5) for i, v in enumerate("ABBA", 1)]
    ids += [f"p25abba/pen_2000_tight_t0_c{b}_{i}_{v}.json" for b in range(1, 5) for i, v in enumerate("BAAB", 1)]  # BAAB blocks c1-c4
    for sc in ("lambs_shortgrass_8x250", "lambs_grass_8x250"):
        ids += [f"p251perf/fb_{sc}_r{r}_{v}.json" for r in range(1, 4) for v in ("p25", "p251", "seq")]
    ids += [f"p251perf/forced_herds_r{r}_{v}.json" for r in range(1, 4) for v in ("off", "on", "seq")]
    return ids


_INJECT = ["inject_attack", "inject_attack_same", "inject_push", "inject_pushed_attack"]
EXPECTED_VERIFY_FILES = (
    [f"p24v/{n}.json" for n in ["verify"] + _INJECT] + [f"p25v/{n}.json" for n in ["verify"] + _INJECT] +
    [f"p251v/{n}.json" for n in ["verify", "forced", "inject_attack_same_forced"] + _INJECT] +
    [f"p251perf/verify_{s}.json" for s in ("lambs_grass_8x250", "lambs_shortgrass_8x250")] +
    [f"p26bv/{n}.json" for n in ["verify", "forced", "adaptive_verify_lambs_patch_8x250", "adaptive_verify_lambs_shortgrass_8x250",
                                  "twodim_verify_twodim_patch_herds", "twodim_verify_twodim_shortgrass_herds",
                                  "twodim_adaptive_verify_twodim_patch_herds", "twodim_adaptive_verify_twodim_shortgrass_herds"] + _INJECT])
JOURNAL = ".p26-install-journal.json"
BACKUP = ".p26-backup"


class PublishError(Exception):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_lf(path):
    """SHA-256 of the content with CRLF folded to LF: outputs are pinned in their LF form."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def first(path):
    j = json.loads(Path(path).read_text(encoding="utf-8"))
    return j, (j["results"] if "results" in j else j["runs"])[0]


def sd(v):
    return st.stdev(v) if len(v) > 1 else 0.0


def ci95(v):
    return T95.get(len(v) - 1, 1.96) * sd(v) / len(v) ** 0.5 if len(v) > 1 else 0.0


def num(x):
    return "" if x is None else x


def rel(path):
    return Path(path).relative_to(BENCH).as_posix()


# ---- source inventory ------------------------------------------------------------------------------
def p26_sources():
    """(series, scenario, round, mode letter, relative path) for every run of the fixed spec."""
    out = []
    for label, d, scenes, rounds, _ in P26_SPEC:
        for sc in scenes:
            for r in range(1, rounds + 1):
                for m in "ABC":
                    out.append((label, sc, r, m, f"{d}/{sc}_r{r}_{m}.json"))
    return out


def discovered_sources():
    """Sources found by pattern: AB, verify, plus the fixed timeline logs."""
    ab = sorted(f"{d}/{p.name}" for d, pat in AB_GROUPS for p in (BENCH / d).glob(pat))
    ver = sorted(f"{d}/{p.name}" for _, d, pat in VERIFY_DIRS for p in (BENCH / d).glob(pat))
    return ab, sorted(set(ver))


def check_inventory(manifest):
    """Manifest, spec and disk must agree exactly. Raises PublishError listing every problem."""
    problems = []
    src = manifest["sources"]
    ab_ids = expected_ab_ids()
    if len(ab_ids) != sum(EXPECTED_AB_SERIES.values()) or len(set(ab_ids)) != len(ab_ids):
        raise PublishError("internal: the fixed A/B id list does not match the pinned counts")
    need = {p for *_, p in p26_sources()} | set(TIMELINE_LOGS) | set(ab_ids) | set(EXPECTED_VERIFY_FILES)
    ab, ver = discovered_sources()
    on_disk = set(ab) | set(ver)
    for label, d, *_ in P26_SPEC:  # a stray run id next to the accepted ones (e.g. r7, a renamed file)
        on_disk |= {f"{d}/{q.name}" for q in (BENCH / d).glob("*.json")}
    for p in sorted(on_disk - need):
        problems.append(f"unexpected source (not in the accepted set): {p}")
    for p in sorted(need - set(src)):
        problems.append(f"not pinned in the manifest: {p}")
    for p in sorted(set(src) - need):
        problems.append(f"manifest lists a source outside the accepted set: {p}")
    for p in sorted(src):
        f = BENCH / p
        if not f.is_file():
            problems.append(f"missing source: {p}")
        elif sha(f) != src[p]["sha256"]:
            problems.append(f"SHA-256 mismatch (source modified): {p}")
    if problems:
        raise PublishError("source inventory failed:\n  " + "\n  ".join(problems[:40]) +
                           (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else ""))


# ---- extraction -----------------------------------------------------------------------------------
P26_FIELDS = ["series", "scenario", "round", "mode", "run_id", "source_json", "source_sha256", "exe",
              "sprint_ticks", "ms_per_tick", "steady_n", "steady_avg", "steady_p50", "steady_p95", "steady_p99",
              "parallel_passes", "passes", "nav_redone", "block_changes", "adaptive_switches", "adaptive_probes",
              "adaptive_skipped", "steady_passes", "steady_parallel_passes", "steady_nav_redone",
              "steady_adaptive_skipped", "mismatch_lines_total", "alarms_total", "mobs_start", "mobs_end",
              "mobs_b_start", "mobs_b_end"]


def p26_values(j, r, sprint_ok):
    """The CSV value of every measured field of one P2.6 run (the single mapping used to write and to check)."""
    sp, ph = r.get("steady") or {}, r.get("steady_phase") or {}
    g = lambda k: num(r.get(k))
    s = lambda k: num(r.get(k)) if sprint_ok else ""
    return {"sprint_ticks": j["args"]["ticks"], "exe": r["exe"], "ms_per_tick": r["ms_per_tick"] if sprint_ok else "",
            "steady_n": num(sp.get("n")), "steady_avg": num(sp.get("avg")), "steady_p50": num(sp.get("p50")),
            "steady_p95": num(sp.get("p95")), "steady_p99": num(sp.get("p99")),
            "parallel_passes": s("parallel_passes"), "passes": s("passes"), "nav_redone": s("nav_redone"),
            "block_changes": s("block_changes"), "adaptive_switches": g("adaptive_switches"),
            "adaptive_probes": g("adaptive_probes"), "adaptive_skipped": s("adaptive_skipped"),
            "steady_passes": num(ph.get("passes")), "steady_parallel_passes": num(ph.get("parallel_passes")),
            "steady_nav_redone": num(ph.get("nav_redone")), "steady_adaptive_skipped": num(ph.get("adaptive_skipped")),
            "mismatch_lines_total": g("mismatch_lines_total"), "alarms_total": g("alarms_total"),
            "mobs_start": g("mobs_start"), "mobs_end": g("mobs_end"),
            "mobs_b_start": g("mobs_b_start"), "mobs_b_end": g("mobs_b_end")}


def config_ok(j, r, mode):
    """The run was configured as its mode says (threads, adaptive switch)."""
    env = j["args"].get("env", [])
    if mode == "A":
        return str(j["args"]["threads"]) == "0" and r["threads"] == 0 and not any("ADAPTIVE" in e for e in env)
    want = "HORIZON_ENTITY_ADAPTIVE=" + ("0" if mode == "B" else "1")
    return str(j["args"]["threads"]) == "8" and r["threads"] == 8 and want in env


def extract_p26():
    sprint = {d: ok for _, d, _, _, ok in P26_SPEC}
    rows = []
    for label, sc, rnd, m, p in p26_sources():
        j, r = first(BENCH / p)
        d = p.split("/")[0]
        if "error" in r:
            raise PublishError(f"{p}: the run failed: {r['error'][:80]}")
        if r["scenario"] != sc or not config_ok(j, r, m):
            raise PublishError(f"{p}: scenario or configuration does not match its name (mode {m})")
        row = {"series": label, "scenario": sc, "round": rnd, "mode": MODE[m], "run_id": f"{sc}_r{rnd}_{m}",
               "source_json": p, "source_sha256": sha(BENCH / p)}
        row.update(p26_values(j, r, sprint[d]))
        rows.append(row)
    return rows


AB_FIELDS = ["series", "scenario", "block", "run", "build", "worker_threads", "run_id", "source_json", "source_sha256",
             "exe", "ms_per_tick", "steady_p50", "steady_p95", "parallel_passes", "passes", "nav_redone",
             "block_changes", "mobs_start", "mobs_end"]


def ab_values(r):
    sp = r.get("steady") or {}
    return {"worker_threads": r["threads"], "exe": r["exe"], "ms_per_tick": r["ms_per_tick"],
            "steady_p50": num(sp.get("p50")), "steady_p95": num(sp.get("p95")),
            "parallel_passes": num(r.get("parallel_passes")), "passes": num(r.get("passes")),
            "nav_redone": num(r.get("nav_redone")), "block_changes": num(r.get("block_changes")),
            "mobs_start": num(r.get("mobs_start")), "mobs_end": num(r.get("mobs_end"))}


def ab_identity(path):
    """(series, scenario, block, run, build) of a P2.4-P2.5.1 A/B source, from its file name."""
    d, name = path.split("/")
    if d == "p25abba":
        m = re.fullmatch(r"(.+)_t(\d+)_([bc])(\d+)_(\d)_([AB])\.json", name)
        sc, _, kind, blk, run, v = m.groups()
        return "p24-vs-p25", sc, f"{kind}{blk}", int(run), "P2.4" if v == "A" else "P2.5"
    if name.startswith("ab_"):
        m = re.fullmatch(r"ab_(.+)_b(\d+)_(\d)_([AB])\.json", name)
        sc, blk, run, v = m.groups()
        return "p25-vs-p251", sc, f"b{blk}", int(run), "P2.5" if v == "A" else "P2.5.1"
    if name.startswith("fb_"):
        m = re.fullmatch(r"fb_(.+)_r(\d+)_(p25|p251|seq)\.json", name)
        sc, rnd, v = m.groups()
        return "p251-block-change-cost", sc, f"r{rnd}", 1, {"p25": "P2.5 t8", "p251": "P2.5.1 t8", "seq": "sequential"}[v]
    m = re.fullmatch(r"forced_herds_r(\d+)_(on|off|seq)\.json", name)
    rnd, v = m.groups()
    return "p251-forced-redo", "herds_8x250", f"r{rnd}", 1, {"on": "forced redo every pass", "off": "no forcing", "seq": "sequential"}[v]


def extract_ab(manifest):
    ab = sorted(p for p in manifest["sources"] if p.split("/")[0] in ("p25abba", "p251perf") and not p.split("/")[1].startswith("verify_"))
    rows = []
    for p in ab:
        series, sc, block, run, build = ab_identity(p)
        j, r = first(BENCH / p)
        if r["scenario"] != sc:
            raise PublishError(f"{p}: scenario does not match its name")
        row = {"series": series, "scenario": sc, "block": block, "run": run, "build": build, "run_id": Path(p).stem,
               "source_json": p, "source_sha256": sha(BENCH / p)}
        row.update(ab_values(r))
        rows.append(row)
    rows.sort(key=lambda r: (r["series"], r["source_json"]))
    return rows


VERIFY_FIELDS = ["series", "scenario", "order", "source_json", "source_sha256", "exe", "mode", "ticks", "passes",
                 "parallel_passes", "verified", "mismatches", "redone_total", "nav_redone", "adaptive_switches",
                 "adaptive_skipped", "mismatch_lines_total", "alarms_total", "differs_lines", "tripped"]


def verify_values(j, r, stem):
    return {"scenario": r["scenario"], "order": r.get("order") or "", "exe": r["exe"], "mode": stem,
            "ticks": j["args"]["ticks"], "passes": num(r.get("passes")), "parallel_passes": num(r.get("parallel_passes")),
            "verified": num(r.get("verified")), "mismatches": num(r.get("mismatches")),
            "redone_total": num(r.get("redone_total")), "nav_redone": num(r.get("nav_redone")),
            "adaptive_switches": num(r.get("adaptive_switches")), "adaptive_skipped": num(r.get("adaptive_skipped")),
            "mismatch_lines_total": num(r.get("mismatch_lines_total")), "alarms_total": num(r.get("alarms_total")),
            "differs_lines": num(r.get("differs_lines")), "tripped": num(r.get("tripped"))}


def verify_label(p):
    d, name = p.split("/")
    for label, dd, pat in VERIFY_DIRS:
        if dd == d and (pat == "*.json" or name.startswith("verify_")):
            return label
    raise PublishError(f"{p}: no verify series")


def extract_verify(manifest):
    rows = []
    def order(p):
        label = verify_label(p)
        return ([i for i, (l, d, _) in enumerate(VERIFY_DIRS) if d == p.split("/")[0] and l == label][0], p)

    for p in sorted((p for p in manifest["sources"] if p.endswith(".json") and
                     (p.split("/")[0] in ("p24v", "p25v", "p251v", "p26bv") or
                      (p.split("/")[0] == "p251perf" and p.split("/")[1].startswith("verify_")))), key=order):
        j = json.loads((BENCH / p).read_text(encoding="utf-8"))
        for i, r in enumerate(j["results"] if "results" in j else j["runs"]):
            if "error" in r:
                continue
            row = {"series": verify_label(p), "source_json": p, "source_sha256": sha(BENCH / p)}
            row.update(verify_values(j, r, Path(p).stem))
            rows.append(row)
    return rows


PARSTATS = re.compile(r"PARSTATS passes (\d+), parallel (\d+),.*?mobs ([0-9.]+),.*?fallbacks \[(.*?)\].*?"
                      r"nav_redone (\d+) \(shape \d+\), adaptive switches (\d+), probes (\d+)")
TL_FIELDS = ["scenario", "round", "mode", "source_log", "source_sha256", "window_index", "passes_in_window",
             "parallel_in_window", "nav_redone_in_window", "adaptive_skipped_in_window", "cumulative_switches",
             "cumulative_probes"]


def sprint_lines(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    a = next(i for i, l in enumerate(lines) if "The game is sprinting" in l)
    b = next(i for i, l in enumerate(lines) if "Sprint completed" in l and i > a)
    return lines[a:b]


def extract_timeline():
    """One row per PARSTATS line (20 passes) of the sprint window of lambs_patch_8x250, modes B and C."""
    rows = []
    for p in TIMELINE_LOGS:
        m = re.fullmatch(rf"p26b/{TIMELINE_SCENE}_r(\d+)_([BC])\.log", p)
        rnd, mode = int(m.group(1)), m.group(2)
        k = 0
        for l in sprint_lines(BENCH / p):
            q = PARSTATS.search(l)
            if not q:
                continue
            skipped = sum(int(n) for n in re.findall(r"adaptive=(\d+)", q.group(4)))
            rows.append({"scenario": TIMELINE_SCENE, "round": rnd, "mode": MODE[mode], "source_log": p,
                         "source_sha256": sha(BENCH / p), "window_index": k, "passes_in_window": int(q.group(1)),
                         "parallel_in_window": int(q.group(2)), "nav_redone_in_window": int(q.group(5)),
                         "adaptive_skipped_in_window": skipped, "cumulative_switches": int(q.group(6)),
                         "cumulative_probes": int(q.group(7))})
            k += 1
    return rows


def write_csv(path, fields, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- statistics (from the CSV only) -----------------------------------------------------------------
def col(rows, series, scenario, mode, field):
    out = []
    for r in sorted((r for r in rows if r["series"] == series and r["scenario"] == scenario and r["mode"] == MODE[mode]),
                    key=lambda r: int(r["round"])):
        if r[field] != "":
            out.append(float(r[field]))
    return out


def paired(rows, series, scenario, a, b, field):
    """b - a per round."""
    ra = {int(r["round"]): float(r[field]) for r in rows if r["series"] == series and r["scenario"] == scenario
          and r["mode"] == MODE[a] and r[field] != ""}
    rb = {int(r["round"]): float(r[field]) for r in rows if r["series"] == series and r["scenario"] == scenario
          and r["mode"] == MODE[b] and r[field] != ""}
    return [rb[k] - ra[k] for k in sorted(set(ra) & set(rb))]


# ---- charts -----------------------------------------------------------------------------------------
SHORT = {"lambs_shortgrass_8x250": "lambs_shortgrass_8x250\n(grass + short grass)",
         "lambs_patch_8x250": "lambs_patch_8x250\n(20x20 patch)",
         "lambs_grass_8x250": "lambs_grass_8x250\n(no navigation change)"}
NAMES = {"lambs_shortgrass_8x250": "lambs_shortgrass", "lambs_patch_8x250": "lambs_patch", "lambs_grass_8x250": "lambs_grass",
         "herds_8x250": "herds_8x250", "species_land_8x250": "species_land_8x250",
         "twodim_shortgrass_herds": "two dimensions, shortgrass", "twodim_patch_herds": "two dimensions, patch"}


def bars(path, title, subtitle, groups, rows, series, scen_series, field, unit, note):
    """groups: [(label, scenario)]; values from the CSV rows."""
    vals, pts = [], []
    for _, sc in groups:
        vv, pp = [], []
        for m in "ABC":
            xs = col(rows, scen_series(sc), sc, m, field)
            vv.append((st.fmean(xs), sd(xs)) if xs else None)
            pp.append(xs)
        vals.append(vv)
        pts.append(pp)
    bar_chart(path, title, subtitle, [g for g, _ in groups], series, vals, pts, unit=unit, note=note)


def dot_chart(path, title, subtitle, rows_, xlabel, note):
    """rows_: [(label, [points], color)]; shows each paired difference, the mean and its 95% t interval."""
    w = 860
    left, right, top = 300, 30, 74
    rh = 34
    h = top + rh * len(rows_) + 110
    allv = [p for _, pts, _ in rows_ for p in pts]
    lo = min(min(allv), -0.2)
    hi = max(max(allv), 0.2)
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    x = lambda v: left + (v - lo) / (hi - lo) * (w - left - right)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
         f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{w}" height="{h}" fill="white"/>',
         f'<text x="24" y="26" font-size="16" font-weight="600">{esc(title)}</text>',
         f'<text x="24" y="46" fill="#555">{esc(subtitle)}</text>']
    t = int(lo) - 1
    while t <= hi:
        if lo <= t <= hi:
            o.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top - 8}" y2="{top + rh * len(rows_)}" stroke="#e4e4e4"/>')
            o.append(f'<text x="{x(t):.1f}" y="{top + rh * len(rows_) + 16}" text-anchor="middle" fill="#555">{t:g}</text>')
        t += 1
    o.append(f'<line x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top - 8}" y2="{top + rh * len(rows_)}" stroke="#333" stroke-width="1.5"/>')
    for i, (label, pts, color) in enumerate(rows_):
        cy = top + i * rh + rh / 2
        mean, c = st.fmean(pts), ci95(pts)
        o.append(f'<text x="{left - 12}" y="{cy + 4:.1f}" text-anchor="end">{esc(label)}</text>')
        o.append(f'<line x1="{x(mean - c):.1f}" x2="{x(mean + c):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="{color}" stroke-width="3"/>')
        for p in pts:
            o.append(f'<circle cx="{x(p):.1f}" cy="{cy:.1f}" r="3" fill="white" stroke="#222"/>')
        o.append(f'<rect x="{x(mean) - 4:.1f}" y="{cy - 7:.1f}" width="8" height="14" fill="{color}" stroke="#222"/>')
    o.append(f'<text x="{(left + w - right) / 2:.1f}" y="{top + rh * len(rows_) + 36}" text-anchor="middle" fill="#333">{esc(xlabel)}</text>')
    for k, line in enumerate(note.split("\n")):
        o.append(f'<text x="24" y="{h - 34 + k * 15}" fill="#555" font-size="11">{esc(line)}</text>')
    o.append("</svg>")
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


def line_chart(path, title, subtitle, panels, note):
    """panels: [(panel title, ylabel, [(label, color, xs, mean, lo, hi)])] stacked vertically."""
    w = 860
    left, right, top = 70, 24, 70
    ph, gap = 150, 56
    h = top + len(panels) * (ph + gap) + 70
    xmax = max(x for _, _, ss in panels for _, _, xs, _, _, _ in ss for x in xs)
    ymaxes = [max(max(hi) for _, _, _, _, _, hi in ss) for _, _, ss in panels]
    X = lambda v: left + v / xmax * (w - left - right)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
         f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{w}" height="{h}" fill="white"/>',
         f'<text x="{left}" y="26" font-size="16" font-weight="600">{esc(title)}</text>',
         f'<text x="{left}" y="46" fill="#555">{esc(subtitle)}</text>']
    for pi, (ptitle, ylabel, ss) in enumerate(panels):
        y0 = top + pi * (ph + gap)
        ym = max(1.0, ymaxes[pi] * 1.1)
        Y = lambda v, y0=y0, ym=ym: y0 + ph - v / ym * ph
        o.append(f'<text x="{left}" y="{y0 - 8}" font-weight="600">{esc(ptitle)}</text>')
        step = 5 if ym > 12 else 2 if ym > 5 else 1
        tt = 0
        while tt <= ym:
            o.append(f'<line x1="{left}" x2="{w - right}" y1="{Y(tt):.1f}" y2="{Y(tt):.1f}" stroke="#e4e4e4"/>')
            o.append(f'<text x="{left - 8}" y="{Y(tt) + 4:.1f}" text-anchor="end" fill="#555">{tt:g}</text>')
            tt += step
        o.append(f'<text x="18" y="{y0 + ph / 2}" transform="rotate(-90 18 {y0 + ph / 2})" text-anchor="middle" fill="#333">{esc(ylabel)}</text>')
        for label, color, xs, mean, lo, hi in ss:
            band = " ".join(f"{X(x):.1f},{Y(h_):.1f}" for x, h_ in zip(xs, hi)) + " " + \
                   " ".join(f"{X(x):.1f},{Y(l_):.1f}" for x, l_ in zip(reversed(xs), reversed(lo)))
            o.append(f'<polygon points="{band}" fill="{color}" fill-opacity="0.18"/>')
            o.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' %
                     (color, " ".join(f"{X(x):.1f},{Y(m):.1f}" for x, m in zip(xs, mean))))
        o.append(f'<line x1="{left}" x2="{w - right}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="#333"/>')
        lx = left + 4
        for label, color, *_ in ss:
            o.append(f'<rect x="{lx}" y="{y0 + 4}" width="12" height="4" fill="{color}"/>')
            o.append(f'<text x="{lx + 17}" y="{y0 + 10}" font-size="11">{esc(label)}</text>')
            lx += 17 + 6.4 * len(label) + 20
    for tx in range(0, int(xmax) + 1, 200):
        o.append(f'<text x="{X(tx):.1f}" y="{top + len(panels) * (ph + gap) - gap + 16}" text-anchor="middle" fill="#555">{tx}</text>')
    yb = top + len(panels) * (ph + gap) - gap + 40
    o.append(f'<text x="{(left + w - right) / 2:.1f}" y="{yb}" text-anchor="middle" fill="#333">'
             f'passes since the sprint began (one point = one PARSTATS window of 20 server passes)</text>')
    for k, line in enumerate(note.split("\n")):
        o.append(f'<text x="{left}" y="{yb + 22 + k * 15}" fill="#555" font-size="11">{esc(line)}</text>')
    o.append("</svg>")
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


def charts(p26, tl):
    S = {"A": "A: sequential", "B": "B: parallel, adaptive off", "C": "C: parallel + adaptive"}
    series = [S["A"], S["B"], S["C"]]
    final = lambda sc: "p26-final"
    scenes = ["lambs_shortgrass_8x250", "lambs_patch_8x250", "lambs_grass_8x250"]
    ticks = {"lambs_shortgrass_8x250": 400, "lambs_patch_8x250": 1000, "lambs_grass_8x250": 400}
    # 1. sprint MSPT
    bars(OUT / "p26-mspt.svg", "P2.6: sprint MSPT, sequential vs parallel vs adaptive",
         "Mean over 6 fresh servers per bar (whisker: 1 sample SD, dots: each run); /tick sprint, 8 workers, i5-12400F, mob_griefing=true",
         [(f"{NAMES[s]} ({ticks[s]} ticks)", s) for s in scenes], p26, series, final, "ms_per_tick", "ms per tick (lower is faster)",
         "Rounds rotate the mode order. Whole-server tick, not only mobs. Differences below ~0.3 ms are comparable to the run-to-run SD of this sample (see CSV).\n"
         "lambs_grass has no navigation-relevant block change, so the adaptive policy never engaged there (control).")
    # 2. paired improvement
    rows_ = []
    for s in scenes[:2] + scenes[2:]:
        d = paired(p26, "p26-final", s, "B", "C", "ms_per_tick")
        rows_.append((f"{NAMES[s]}: adaptive - parallel(off)", d, COLORS[2]))
    for s in scenes:
        d = paired(p26, "p26-final", s, "A", "C", "ms_per_tick")
        rows_.append((f"{NAMES[s]}: adaptive - sequential", d, COLORS[3]))
    dot_chart(OUT / "p26-adaptive-gain.svg", "P2.6: paired MSPT difference per round (negative = adaptive is faster)",
              "Dots: one round each (same round, same scene). Bar: mean. Line: 95% t interval of the mean (n = 6 rounds)",
              rows_, "difference in ms per tick",
              "Only 6 paired rounds per row. The interval describes this machine and these scenes, not other hardware or workloads.\n"
              "lambs_grass is the no-conflict control: no distinguishable slowdown was found in this sample;\n"
              "an interval that includes zero does not prove the modes are equivalent.")
    # 3. redo and skipped passes
    vals, pts = [], []
    for s in scenes:
        vv, pp = [], []
        for mode, field in (("B", "nav_redone"), ("C", "nav_redone"), ("C", "adaptive_skipped")):
            xs = col(p26, "p26-final", s, mode, field)
            vv.append((st.fmean(xs), sd(xs)))
            pp.append(xs)
        vals.append(vv)
        pts.append(pp)
    bar_chart(OUT / "p26-redo-skips.svg", "P2.6: navigation redos and passes the policy ran sequentially",
              "Per sprint window, mean of 6 runs (whisker: 1 sample SD, dots: each run); redo = whole tick redone by the P2.5.1 rule",
              [f"{NAMES[s]} ({ticks[s]} ticks)" for s in scenes],
              ["B: redone ticks (adaptive off)", "C: redone ticks (adaptive on)", "C: passes skipped by the policy"],
              vals, pts, unit="passes per sprint window", step=50,
              note="A redone tick costs a parallel attempt plus the sequential pass. Skipped passes ran sequentially instead of attempting parallel.\n"
                   "Counters come from PARSTATS windows of 20 passes inside the sprint; the last partial window is not logged.")
    # 4. p95
    groups = [("shortgrass: conflict phase", "lambs_shortgrass_8x250", "p26-conflict-phase"),
              ("patch: conflict phase", "lambs_patch_8x250", "p26-conflict-phase"),
              ("shortgrass: after the sprint", "lambs_shortgrass_8x250", "p26-final"),
              ("patch: after the sprint", "lambs_patch_8x250", "p26-final"),
              ("grass: after the sprint", "lambs_grass_8x250", "p26-final")]
    vals, pts = [], []
    for _, sc, ser in groups:
        vv, pp = [], []
        for m in "ABC":
            xs = col(p26, ser, sc, m, "steady_p95")
            vv.append((st.fmean(xs), sd(xs)))
            pp.append(xs)
        vals.append(vv)
        pts.append(pp)
    bar_chart(OUT / "p26-p95.svg", "P2.6: steady p95 tick time, conflict phase and after the sprint",
              "/tick query p95 at 20 TPS (mean of 4-5 queries per run); bars: mean of 5-6 runs, whisker 1 sample SD, dots: each run",
              [g for g, _, _ in groups], series, vals, pts, unit="p95 ms per tick (lower is better)",
              note="Conflict phase: 20 TPS from the start, the lambs are still eating (5 runs). After the sprint: conflicts mostly over (6 runs).\n"
                   "In the conflict phase adaptive is better than always-parallel but not better than sequential; do not read this as a universal win.")
    # 5. timeline
    def agg(mode, field):
        runs = {}
        for r in tl:
            if r["mode"] == MODE[mode]:
                runs.setdefault(int(r["round"]), {})[int(r["window_index"])] = float(r[field])
        n = min(len(v) for v in runs.values())
        xs = [(k + 1) * 20 for k in range(n)]
        mean = [st.fmean(v[k] for v in runs.values()) for k in range(n)]
        lo = [min(v[k] for v in runs.values()) for k in range(n)]
        hi = [max(v[k] for v in runs.values()) for k in range(n)]
        return xs, mean, lo, hi
    panels = []
    xb, mb, lb, hb = agg("B", "nav_redone_in_window")
    xc, mc, lc, hc = agg("C", "nav_redone_in_window")
    panels.append(("Redone ticks per 20-pass window", "redone ticks",
                   [("B: adaptive off", COLORS[1], xb, mb, lb, hb), ("C: adaptive on", COLORS[2], xc, mc, lc, hc)]))
    xs_, ms_, ls_, hs_ = agg("C", "adaptive_skipped_in_window")
    panels.append(("Passes run sequentially by the policy per 20-pass window (mode C)", "skipped passes",
                   [("C: adaptive on", COLORS[2], xs_, ms_, ls_, hs_)]))
    line_chart(OUT / "p26-timeline.svg", "P2.6: switching, cooldown and probes on lambs_patch_8x250",
               "Mean of 6 runs per mode (line) and min-max across runs (band), aligned by pass count since /tick sprint 1000 began",
               panels,
               "Window x is aligned by pass count, so run-to-run timing differences blur the shape. The first window already contains the first redos.\n"
               "Cooldown 30 passes, doubled after a failed probe (max 120); a probe is the first attempt after the cooldown.")
    # 6. two dimensions
    g2 = [("shortgrass: avg ms/tick", "twodim_shortgrass_herds", "steady_avg"), ("patch: avg ms/tick", "twodim_patch_herds", "steady_avg"),
          ("shortgrass: p95", "twodim_shortgrass_herds", "steady_p95"), ("patch: p95", "twodim_patch_herds", "steady_p95")]
    vals, pts = [], []
    for _, sc, f in g2:
        vv, pp = [], []
        for m in "ABC":
            xs = col(p26, "p26-two-dimensions", sc, m, f)
            vv.append((st.fmean(xs), sd(xs)))
            pp.append(xs)
        vals.append(vv)
        pts.append(pp)
    bar_chart(OUT / "p26-two-dimensions.svg", "P2.6: two dimensions ticked by one server thread",
              "Overworld lambs on short grass + Nether herds (2000 mobs each), 20 TPS, 12-18 /tick queries per run, 3 runs per bar",
              [g for g, _, _ in g2], series, vals, pts, unit="ms per tick (lower is faster)",
              note="One policy per server thread is shared by both dimensions. Only 3 rounds: the adaptive/always-parallel difference is not resolved.\n"
                   "Averages cover the whole 60 s (shortgrass) or 90 s (patch) window, conflicts included.")


# ---- verification of the prepared files against the sources ---------------------------------------
def eq(csv_value, want):
    if want == "" or want is None:
        return csv_value == ""
    try:
        return float(csv_value) == float(want)
    except ValueError:
        return csv_value == str(want)


def check_values(prepared):
    """Compare every published value with the source it came from. Returns a list of problems."""
    bad = []
    rows = prepared["p26-data.csv"]
    sprint = {d: ok for _, d, _, _, ok in P26_SPEC}
    for row in rows:
        p = row["source_json"]
        j, r = first(BENCH / p)
        m = row["run_id"][-1]
        d = p.split("/")[0]
        if (r["scenario"], MODE[m], str(row["round"])) != (row["scenario"], row["mode"], re.search(r"_r(\d+)_", p).group(1)):
            bad.append(f"{p}: scenario/mode/round differ from the source")
        if not config_ok(j, r, m):
            bad.append(f"{p}: threads / adaptive setting do not match mode {m}")
        for k, want in p26_values(j, r, sprint[d]).items():
            if not eq(row[k], want):
                bad.append(f"{p}: {k} = {row[k]!r}, source {want!r}")
    for row in prepared["p24-p251-data.csv"]:
        p = row["source_json"]
        j, r = first(BENCH / p)
        if (row["series"], row["scenario"], row["block"], int(row["run"]), row["build"]) != ab_identity(p) or r["scenario"] != row["scenario"]:
            bad.append(f"{p}: identity differs from the source name")
        for k, want in ab_values(r).items():
            if not eq(row[k], want):
                bad.append(f"{p}: {k} = {row[k]!r}, source {want!r}")
    by_file = {}
    for row in prepared["p24-p26-verify.csv"]:
        by_file.setdefault(row["source_json"], []).append(row)
    for p, rs in by_file.items():
        j = json.loads((BENCH / p).read_text(encoding="utf-8"))
        res = [r for r in (j["results"] if "results" in j else j["runs"]) if "error" not in r]
        if len(res) != len(rs):
            bad.append(f"{p}: {len(rs)} rows published, {len(res)} results in the source")
            continue
        for row, r in zip(rs, res):
            for k, want in verify_values(j, r, Path(p).stem).items():
                if not eq(row[k], want):
                    bad.append(f"{p}: {k} = {row[k]!r}, source {want!r}")
            if row["series"] != verify_label(p):
                bad.append(f"{p}: series differs")
    # timeline: re-parse every log with separate patterns; compare per window and as sums
    tl = prepared["p26-timeline.csv"]
    for p in TIMELINE_LOGS:
        rs = sorted((r for r in tl if r["source_log"] == p), key=lambda r: int(r["window_index"]))
        win = [l for l in sprint_lines(BENCH / p) if "PARSTATS" in l]
        if len(win) != len(rs) or [int(r["window_index"]) for r in rs] != list(range(len(win))):
            bad.append(f"{p}: {len(rs)} timeline rows, {len(win)} PARSTATS windows in the sprint")
            continue
        for l, r in zip(win, rs):
            got = (int(re.search(r"passes (\d+),", l).group(1)), int(re.search(r", parallel (\d+),", l).group(1)),
                   int(re.search(r"nav_redone (\d+) ", l).group(1)),
                   sum(int(x) for x in re.findall(r"adaptive=(\d+)", l)),
                   int(re.search(r"adaptive switches (\d+)", l).group(1)), int(re.search(r"probes (\d+)", l).group(1)))
            have = tuple(int(r[k]) for k in ("passes_in_window", "parallel_in_window", "nav_redone_in_window",
                                              "adaptive_skipped_in_window", "cumulative_switches", "cumulative_probes"))
            if got != have:
                bad.append(f"{p}: window {r['window_index']} differs: published {have}, source {got}")
        m = re.fullmatch(rf"p26b/{TIMELINE_SCENE}_r(\d+)_([BC])\.log", p)
        if (rs[0]["round"], rs[0]["mode"]) != (m.group(1), MODE[m.group(2)]):
            bad.append(f"{p}: round/mode differ from the file name")
    return bad


def check_structure(prepared, svgs):
    bad = []
    for name, want in EXPECTED_ROWS.items():
        if len(prepared[name]) != want:
            bad.append(f"{name}: {len(prepared[name])} rows, expected {want}")
    for name, key, want in (("p26-data.csv", "series", EXPECTED_P26_SERIES), ("p24-p251-data.csv", "series", EXPECTED_AB_SERIES),
                            ("p24-p26-verify.csv", "series", EXPECTED_VERIFY_SERIES)):
        got = {}
        for r in prepared[name]:
            got[r[key]] = got.get(r[key], 0) + 1
        if got != want:
            bad.append(f"{name}: per-series rows {got}, expected {want}")
    ids = [(r["series"], r["scenario"], r["round"], r["mode"]) for r in prepared["p26-data.csv"]]
    want = {(label, sc, str(rd), MODE[m]) for label, sc, rd, m, _ in p26_sources()}
    if len(set(ids)) != len(ids) or set(ids) != want:
        bad.append("p26-data.csv: series/scenario/round/mode ids are not exactly the accepted set")
    if len({r["source_json"] for r in prepared["p24-p251-data.csv"]}) != EXPECTED_ROWS["p24-p251-data.csv"]:
        bad.append("p24-p251-data.csv: duplicate or missing sources")
    for name in OUTPUTS:
        if name.endswith(".svg"):
            try:
                xml.dom.minidom.parseString(svgs[name])
            except Exception as e:  # noqa: BLE001
                bad.append(f"{name}: not well-formed SVG ({e})")
    return bad


# ---- build in a temporary directory --------------------------------------------------------------
def build(tmp, manifest):
    """Generate every output into `tmp` (LF line endings). Returns {name: rows} for the CSVs and {name: bytes} for the SVGs."""
    tmp = Path(tmp)
    p26, ab, ver, tl = extract_p26(), extract_ab(manifest), extract_verify(manifest), extract_timeline()
    write_csv(tmp / "p26-data.csv", P26_FIELDS, p26)
    write_csv(tmp / "p24-p251-data.csv", AB_FIELDS, ab)
    write_csv(tmp / "p24-p26-verify.csv", VERIFY_FIELDS, ver)
    write_csv(tmp / "p26-timeline.csv", TL_FIELDS, tl)
    global OUT
    saved, OUT = OUT, tmp  # the chart functions write to OUT
    try:
        charts(read_csv(tmp / "p26-data.csv"), read_csv(tmp / "p26-timeline.csv"))
    finally:
        OUT = saved
    for name in OUTPUTS:  # charts use write_text: fold platform newlines to LF so hashes are portable
        f = tmp / name
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))
    prepared = {n: read_csv(tmp / n) for n in OUTPUTS if n.endswith(".csv")}
    svgs = {n: (tmp / n).read_bytes() for n in OUTPUTS if n.endswith(".svg")}
    return prepared, svgs


def make_manifest():
    names = [p for *_, p in p26_sources()] + TIMELINE_LOGS + expected_ab_ids() + EXPECTED_VERIFY_FILES
    src = {}
    for p in sorted(set(names)):
        f = BENCH / p
        if not f.is_file():
            raise PublishError(f"missing source: {p}")
        src[p] = {"sha256": sha(f)}
    return {"format": 1, "note": "Pinned by tools/voxaryn/p26_publish.py --refresh-manifest. Sources and outputs are "
            "SHA-256 of the file content; outputs are pinned with LF line endings.", "sources": src,
            "rows": dict(EXPECTED_ROWS), "outputs": {}}


# ---- transactional installation ------------------------------------------------------------------
def _replace(src, dst):  # a seam for the tests
    os.replace(src, dst)


def _unlink_journal(journal):  # seams for the tests: the two steps that end a transaction
    Path(journal).unlink()


def _remove_backup(backup):
    shutil.rmtree(backup, ignore_errors=True)


def _same(a, b):
    return Path(a).is_file() and Path(b).is_file() and Path(a).read_bytes() == Path(b).read_bytes()


def _restore(out_dir):
    """Undo an unfinished install from its journal. Idempotent: it can be repeated after a kill.

    Order: restore every file and check it against the backup, then delete the journal (the point where the
    old set is final), then the backup. The backup is never touched before the restore is complete."""
    out_dir = Path(out_dir)
    journal = out_dir / JOURNAL
    backup = out_dir / BACKUP
    if not journal.exists():
        _remove_backup(backup)  # an orphan from a kill after the journal was gone: the set on disk is complete
        return False
    info = json.loads(journal.read_text(encoding="utf-8"))
    for name, existed in info["files"].items():
        target = out_dir / name
        if existed:
            tmp = out_dir / f".{name}.restore"
            shutil.copyfile(backup / name, tmp)
            os.replace(tmp, target)
        elif target.exists():
            target.unlink()
        for leftover in (out_dir / f".{name}.new", out_dir / f".{name}.restore"):
            if leftover.exists():
                leftover.unlink()
    for name, existed in info["files"].items():  # confirm before the journal goes
        ok = _same(out_dir / name, backup / name) if existed else not (out_dir / name).exists()
        if not ok:
            raise PublishError(f"restore of {name} could not be confirmed; the journal and backup were kept")
    _unlink_journal(journal)
    _remove_backup(backup)
    return True


_rollback = _restore  # a seam for the tests


def install(files, out_dir):
    """Copy {name: prepared path} into out_dir so that either the whole set is replaced or none of it is.

    A backup of the old files and a journal are written first. If a replacement fails the old set is
    restored; if the process dies, the journal stays and every later run refuses to continue until
    --recover has restored the old set. Commit order: every new file is checked against its prepared
    source, then the journal is deleted (the new set is final), then the backup. A kill between the last
    two steps leaves only an orphan backup, which the next install or --recover removes.
    (A single os.replace is atomic; a set of them is not.)"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / JOURNAL).exists():
        raise PublishError(f"an earlier install was interrupted ({out_dir / JOURNAL}); run with --recover first")
    backup = out_dir / BACKUP
    _remove_backup(backup)  # leftovers of an earlier run that had already committed or never started
    backup.mkdir()
    existed = {n: (out_dir / n).exists() for n in files}
    for n, e in existed.items():
        if e:
            shutil.copyfile(out_dir / n, backup / n)
    (out_dir / JOURNAL).write_text(json.dumps({"files": existed}), encoding="utf-8")
    try:
        for n, src in files.items():
            stage = out_dir / f".{n}.new"
            shutil.copyfile(src, stage)
            _replace(stage, out_dir / n)
        for n, src in files.items():
            if not _same(out_dir / n, src):
                raise PublishError(f"{n} was not installed correctly")
    except BaseException:
        _rollback(out_dir)
        raise
    _unlink_journal(out_dir / JOURNAL)  # the commit point
    _remove_backup(backup)

# ---- statistics tables --------------------------------------------------------------------------
def tables(p26, ab):
    print("== P2.6 final series (CSV-derived)")
    for ser, scenes in (("p26-final", ["lambs_shortgrass_8x250", "lambs_patch_8x250", "lambs_grass_8x250"]),
                        ("p26-conflict-phase", ["lambs_shortgrass_8x250", "lambs_patch_8x250"]),
                        ("p26-control", ["herds_8x250", "species_land_8x250"]),
                        ("p26-two-dimensions", ["twodim_shortgrass_herds", "twodim_patch_herds"])):
        for sc in scenes:
            print(f"-- {ser} {sc}")
            for f in ("ms_per_tick", "steady_p95", "steady_p50", "steady_avg", "nav_redone", "adaptive_skipped",
                      "steady_nav_redone", "steady_adaptive_skipped", "adaptive_switches", "parallel_passes", "passes"):
                line = []
                for m in "ABC":
                    xs = col(p26, ser, sc, m, f)
                    if xs:
                        line.append(f"{m}: n={len(xs)} mean {st.fmean(xs):.2f} sd {sd(xs):.2f} [{min(xs):.2f}..{max(xs):.2f}]")
                if line and f in ("ms_per_tick", "steady_p95", "steady_avg", "nav_redone", "adaptive_skipped", "steady_nav_redone",
                                  "adaptive_switches"):
                    print(f"   {f:18s} " + " | ".join(line))
            for f in ("ms_per_tick", "steady_p95", "steady_avg"):
                for a, b in (("B", "C"), ("A", "C"), ("A", "B")):
                    d = paired(p26, ser, sc, a, b, f)
                    if len(d) > 1:
                        se = sd(d) / len(d) ** 0.5
                        print(f"   {f:12s} {b}-{a}: mean {st.fmean(d):+.2f} 95%CI +-{ci95(d):.2f} t={st.fmean(d) / se if se else float('inf'):.1f} "
                              f"n={len(d)} {b} lower {sum(x < 0 for x in d)}/{len(d)}")
    print("== A/B series")
    keys = sorted({(r["series"], r["scenario"]) for r in ab})
    for ser, sc in keys:
        by = {}
        for r in ab:
            if r["series"] == ser and r["scenario"] == sc:
                by.setdefault(r["build"], []).append(float(r["ms_per_tick"]))
        print(f"-- {ser} {sc}: " + " | ".join(f"{b}: n={len(v)} mean {st.fmean(v):.2f} sd {sd(v):.2f}" for b, v in by.items()))



def main(argv=None):
    global BENCH, OUT, MANIFEST
    import argparse
    ap = argparse.ArgumentParser(description="P2.4-P2.6 publication data (see the module docstring)")
    ap.add_argument("--bench", type=Path, default=BENCH, help="raw benchmark directory")
    ap.add_argument("--out", type=Path, default=OUT, help="published directory")
    ap.add_argument("--manifest", type=Path, default=None, help="manifest file (default: <out>/p26-manifest.json)")
    ap.add_argument("--tables", action="store_true", help="print the statistics used in the report")
    ap.add_argument("--install", action="store_true", help="replace the published files (transactional)")
    ap.add_argument("--refresh-manifest", action="store_true", help="re-pin sources and outputs, then install")
    ap.add_argument("--recover", action="store_true", help="roll back an interrupted install")
    a = ap.parse_args(argv)
    BENCH, OUT = a.bench, a.out
    MANIFEST = a.manifest or (OUT / "p26-manifest.json")
    tmp = None
    try:
        if a.recover:
            print("recovered: the previous set was restored" if _restore(OUT) else "nothing to recover")
            return 0
        if (OUT / JOURNAL).exists():
            raise PublishError(f"an earlier install was interrupted ({OUT / JOURNAL}); run with --recover first")
        if a.refresh_manifest:
            manifest = make_manifest()
        else:
            if not MANIFEST.is_file():
                raise PublishError(f"{MANIFEST} is missing; pinning is an explicit step: --refresh-manifest")
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        check_inventory(manifest)
        tmp = Path(tempfile.mkdtemp(prefix="p26-publish-"))
        prepared, svgs = build(tmp, manifest)
        problems = check_structure(prepared, svgs) + check_values(prepared)
        built = {n: sha_lf(tmp / n) for n in OUTPUTS}
        if a.refresh_manifest:
            manifest["outputs"] = built
        else:
            if manifest.get("rows") != EXPECTED_ROWS:
                problems.append("the manifest's row counts differ from the counts fixed in the code")
            for n in OUTPUTS:
                if manifest["outputs"].get(n) != built[n]:
                    problems.append(f"{n}: generated content differs from the pinned SHA-256")
        if problems:
            raise PublishError("verification failed; no published file was touched:\n  " + "\n  ".join(problems[:40]) +
                               (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else ""))
        stale = [n for n in OUTPUTS if not (OUT / n).is_file() or sha_lf(OUT / n) != built[n]]
        print(f"verified: {sum(len(v) for v in prepared.values())} rows in {len(prepared)} CSV files, {len(svgs)} SVG files, "
              f"{len(manifest['sources'])} pinned sources; all values match their sources")
        if a.tables:
            tables(prepared["p26-data.csv"], prepared["p24-p251-data.csv"])
        if a.install or a.refresh_manifest:
            files = {n: tmp / n for n in OUTPUTS}
            if a.refresh_manifest:
                (tmp / MANIFEST.name).write_bytes((json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8"))
                if MANIFEST.parent == OUT:
                    files[MANIFEST.name] = tmp / MANIFEST.name
            install(files, OUT)
            print(f"installed {len(files)} files into {OUT}" if stale or a.refresh_manifest else "installed (content unchanged)")
            return 0
        if stale:
            print("published files differ from the verified set or are missing; nothing was written "
                  f"(use --install): {', '.join(stale)}")
            return 1
        print("published files are identical to the verified set (nothing written)")
        return 0
    except PublishError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
