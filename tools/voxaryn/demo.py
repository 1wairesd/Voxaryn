#!/usr/bin/env python3
"""Watch the parallel mob pass in a game window (Voxaryn P1).

Starts a dedicated server (fresh world under .local/voxaryn-bench/demo) with
the experimental parallel mob pass, opens a game window that joins it as
`Viewer` (its own temporary HORIZON_HOME, so no real player data is touched),
builds glass pens of cows around the spawn and shows the live numbers in the
sidebar scoreboard and in this console. Type server commands here (e.g.
`tick query`, `summon minecraft:cow 0 121 0`); `stop` quits.

  py -3 tools/voxaryn/demo.py --exe .local/baseline/horizon-client-p11-par.exe
  py -3 tools/voxaryn/demo.py --exe ... --threads 0      # the sequential P0 pass, to compare
  py -3 tools/voxaryn/demo.py --exe ... --verify 20      # also check every 20th pass

Sidebar: MSPTx10 (tick time in tenths of a ms), TPSx10, Threads, Islands,
Largest (mobs in the largest island), SpeedupX10 (the pass's work over its
wall time, ×10) and Parallel_pct (share of passes that ran in parallel).
"""
import argparse, os, random, re, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entity_bench as b  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NAME = "Viewer"
PEN_Y = 120
CLEAN = [
    # Only the pens' cows stay: sea life and wild herds between the pens would
    # join two pens into one island (or keep the tick sequential).
    "kill @e[type=!minecraft:player,type=!minecraft:cow,type=!minecraft:item]",
    f"kill @e[type=minecraft:cow,x=-2000,y=-64,z=-2000,dx=4000,dy={PEN_Y - 64 - 2},dz=4000]",
]
QUIET = ("Killed", "No entity", "P50", "force", "Set score", "Average time", "The game is running",
         "Gamerule", "objective", "Changed the display")


def pens(count, spacing):
    side = int(count ** 0.5 + 0.999)
    off = (side - 1) * spacing / 2
    return [(int(-off + (k % side) * spacing) - 16, int(-off + (k // side) * spacing) - 16) for k in range(count)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", required=True)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--verify", type=int, default=0)
    ap.add_argument("--pens", type=int, default=9)
    ap.add_argument("--cows", type=int, default=200, help="cows per pen")
    ap.add_argument("--spacing", type=int, default=128)
    ap.add_argument("--port", type=int, default=25599)
    ap.add_argument("--no-window", action="store_true")
    args = ap.parse_args()
    exe = Path(args.exe).resolve()

    d = b.BASE / "demo"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "eula.txt").write_text("eula=true\n")
    props = dict(b.PROPERTIES, **{"server-port": str(args.port), "motd": "Voxaryn P1 demo"})
    (d / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    env = {}
    if args.threads > 0:
        env["HORIZON_ENTITY_THREADS"] = str(args.threads)
    if args.verify > 0:
        env["HORIZON_ENTITY_VERIFY"] = str(args.verify)
    print(f"== Voxaryn P1 demo: {'parallel, %d threads' % args.threads if args.threads else 'sequential (P0)'} ==")
    srv = b.Server(exe, d, args.port, env)
    win = None
    home = Path(tempfile.mkdtemp(prefix="horizon-demo-"))
    try:
        srv.wait_log(r"Done \(", 300)
        if not args.no_window:
            wenv = dict(os.environ, HORIZON_HOME=str(home), HORIZON_NO_AUDIO="1")
            for k in list(wenv):
                if k.startswith("HORIZON_ENTITY_"):
                    wenv.pop(k)  # the window is only a client
            win = subprocess.Popen([str(exe), "--resources", str(b.RES), "--server",
                                    f"127.0.0.1:{args.port}", "--name", NAME], env=wenv)
        print(f"waiting for {NAME} to join 127.0.0.1:{args.port} ...", flush=True)
        srv.wait_log(rf"{NAME} joined the game", 600)
        time.sleep(2)
        srv.console(f"op {NAME}")
        b.apply_rules(srv)
        for r in ("advance_weather", "log_admin_commands", "send_command_feedback"):
            srv.console(f"gamerule {r} false")
        srv.console("time set day")
        srv.console("weather clear")
        srv.console('scoreboard objectives add voxaryn dummy "Voxaryn P1"')
        srv.console("scoreboard objectives setdisplay sidebar voxaryn")
        places = pens(args.pens, args.spacing)
        for (x, z) in places:
            srv.console(f"forceload add {x - 8} {z - 8} {x + 40} {z + 40}")
        srv.console(f"tp {NAME} 0 {PEN_Y + 40} 0")
        print("building glass pens and cows ...", flush=True)
        time.sleep(8)
        rnd = random.Random(12345)
        for (x, z) in places:
            srv.console(f"fill {x} {PEN_Y} {z} {x + 31} {PEN_Y + 3} {z + 31} minecraft:glass hollow")
            for _ in range(args.cows):
                # Random spots over the floor: the cows touch and push from the start.
                px, pz = x + 1.5 + rnd.random() * 29, z + 1.5 + rnd.random() * 29
                srv.console(f"summon minecraft:cow {px:.3f} {PEN_Y + 1} {pz:.3f}")
        for c in CLEAN:
            srv.console(c)
        srv.console(f"tp {NAME} 0 {PEN_Y + 40} 0 0 90")
        print(f"ready: {len(places)} pens x {args.cows} cows; you hover above the centre (creative).")
        print("numbers in the sidebar and here every 5 s; server commands work here; `stop` quits.\n", flush=True)

        stop = threading.Event()

        def typed():
            for line in sys.stdin:
                line = line.strip()
                if line in ("stop", "exit", "quit"):
                    stop.set()
                    return
                if line:
                    srv.console(line.lstrip("/"))

        threading.Thread(target=typed, daemon=True).start()
        pos = len(srv.log_text())
        last_clean = last_print = time.time()
        mspt = p95 = None
        g = None
        while not stop.is_set() and srv.p.poll() is None:
            srv.console("tick query")
            time.sleep(1)
            now = time.time()
            if now - last_clean > 10:
                for c in CLEAN:
                    srv.console(c)
                last_clean = now
            text = srv.log_text()
            new, pos = text[pos:], len(text)
            for ln in new.splitlines():
                if "System chat:" in ln:
                    msg = ln.split("System chat:", 1)[1].strip()
                    if not any(q in msg for q in QUIET):
                        print("  server:", msg[:160])
                if "differs from the sequential" in ln or "level random" in ln:
                    print("  !!", ln[:300])
            avg = b.TICK_AVG.findall(new)
            pct = b.TICK_PCT.findall(new)
            if avg:
                mspt = float(avg[-1])
            if pct:
                p95 = float(pct[-1][1])
            par = list(b.PARSTATS.finditer(new))
            if par:
                g = par[-1].groups()
            score = {}
            if mspt is not None:
                score["MSPTx10"] = round(mspt * 10)
                score["TPSx10"] = round(min(20.0, 1000.0 / mspt if mspt > 0 else 20.0) * 10)
            line = "sequential (P0)"
            if args.threads and g is not None:
                passes, parallel = int(g[0]), int(g[1])
                score["Threads"] = int(g[2])
                score["Parallel_pct"] = round(100 * parallel / max(1, passes))
                if parallel:
                    run, work = float(g[11]), float(g[6])
                    speed = work / run if run > 0 else 0.0
                    score.update(Islands=round(float(g[3])), Largest=round(float(g[4])), SpeedupX10=round(speed * 10))
                    line = (f"parallel {parallel}/{passes} | islands {float(g[3]):.0f} (largest {float(g[4]):.0f}) | "
                            f"mob work {work:.1f} ms in {run:.1f} ms = x{speed:.1f}")
                    if args.verify:
                        line += f" | checked {g[14]}, mismatches {g[15]}"
                else:
                    blocking = g[17] if g[17] != "-" else ""
                    line = f"SEQUENTIAL: {g[16].strip() or '-'}" + (f" (blocked by {blocking})" if blocking else "")
            for k, v in score.items():
                srv.console(f"scoreboard players set {k} voxaryn {v}")
            if now - last_print >= 5:
                last_print = now
                ms = f"MSPT {mspt:5.1f} ms" if mspt is not None else "MSPT   -  "
                if p95 is not None:
                    ms += f" (p95 {p95:5.1f})"
                print(f"[{time.strftime('%H:%M:%S')}] {ms} | {line}", flush=True)
    finally:
        print("stopping ...", flush=True)
        if win and win.poll() is None:
            win.terminate()
        srv.stop()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
