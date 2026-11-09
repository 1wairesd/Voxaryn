#!/usr/bin/env python3
"""Save/restore round trip of the parallel mob pass (P1, P1.1).

Builds an arena scene, ticks it on the parallel pass, saves (`save-all
flush`) and stops; restarts on the sequential pass (threads 0) and again on
the parallel pass, and compares the entity counts per species. The parallel
pass must also come back on after the reload.

  py -3 tools/voxaryn/save_roundtrip.py --exe .local/baseline/horizon-client-p11-par.exe
"""
import argparse, json, re, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entity_bench as b  # noqa: E402


def counts(srv, species):
    out = {}
    for s in species + ["!player"]:
        mark = len(srv.log_text())
        sel = f"type={'' if s.startswith('!') else 'minecraft:'}{s}"
        srv.console(f"execute if entity @e[{sel}]")
        try:
            m = srv.wait_log(r"Test passed\. Count: (\d+)|Test failed", 20, after=mark)
            out[s] = int(m.group(1)) if m.group(1) else 0
        except RuntimeError:
            out[s] = None
    return out


def start(exe, d, port, threads):
    env = {"HORIZON_ENTITY_THREADS": str(threads)} if threads else {}
    srv = b.Server(exe, d, port, env)
    srv.wait_log(r"Done \(", 300)
    return srv


def parallel_in(srv, mark):
    par = [p.groups() for p in b.PARSTATS.finditer(srv.log_text()[mark:])]
    return sum(int(p[1]) for p in par), sorted({f for p in par for f in p[16].split() if f})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--scenario", default="mixedv_8x250")
    ap.add_argument("--ticks", type=int, default=300)
    ap.add_argument("--out", default=str(b.BASE / "save_roundtrip.json"))
    args = ap.parse_args()
    exe = Path(args.exe).resolve()
    spec = b.SCENARIOS[args.scenario]
    species = sorted(set(spec["keep"]))
    d = b.BASE / "run_save"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "eula.txt").write_text("eula=true\n")
    port = b.free_port()
    props = dict(b.PROPERTIES, **{"server-port": str(port)})
    (d / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    res = {"scenario": args.scenario, "exe": exe.name}
    srv = start(exe, d, port, 6)
    try:
        res["rules"] = b.apply_rules(srv)
        b.build(srv, spec)
        time.sleep(3)
        srv.console(b.clean_selector(spec["keep"]))
        time.sleep(5)
        mark = len(srv.log_text())
        srv.console(f"tick sprint {args.ticks}")
        srv.wait_log(b.REPORT.pattern, 1200, after=mark)
        res["parallel_passes_before_save"], res["fallbacks_before_save"] = parallel_in(srv, mark)
        srv.console("tick freeze")
        time.sleep(1)
        res["before_save"] = counts(srv, species)
        mark = len(srv.log_text())
        srv.console("save-all flush")
        srv.wait_log(r"Saved the game", 120, after=mark)
    finally:
        srv.stop()
    for label, threads in (("reload_sequential", 0), ("reload_parallel", 6)):
        srv = start(exe, d, port, threads)
        try:
            srv.console("tick freeze")
            # Horizon does not keep force-loaded chunks across a restart (no
            # chunks.dat): load the arenas again to see their entities.
            for (x, z) in b.arenas(spec["count"], spec["spacing"]):
                srv.console(f"forceload add {x - 24} {z - 24} {x + 40} {z + 40}")
            time.sleep(10)
            res[label] = counts(srv, species)
            if threads:
                srv.console("tick unfreeze")
                mark = len(srv.log_text())
                srv.console("tick sprint 100")
                srv.wait_log(b.REPORT.pattern, 600, after=mark)
                res["parallel_passes_after_reload"], res["fallbacks_after_reload"] = parallel_in(srv, mark)
        finally:
            srv.stop()
    res["counts_identical"] = res["before_save"] == res["reload_sequential"] == res["reload_parallel"]
    Path(args.out).write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
