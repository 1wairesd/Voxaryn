#!/usr/bin/env python3
"""Fast-mob safety test of the parallel mob pass (P1.1 audit, redone for P1.3).

The P1.1 version summoned cows with `Motion:[62,0,0]`. Since P1.3 E Horizon
loads motion as vanilla 26.3's `Entity.load` does: a component beyond 10
either way loads as 0, so those cows never moved and the test checked nothing.

This version stays within vanilla's limits: each "flyer" cow is summoned with
`Motion:[SPEED,0,0]`, SPEED <= 10 (10 itself is kept by `Entity.load`), on the
stone floor 9.5 blocks before the first column of herd B1, in line with one of
its rows. In its first tick it moves the full SPEED blocks along x (the move
comes before ground friction) and lands on a herd cow, which it pushes and is
pushed by. 10 blocks a tick is far above the step the pass can prove
(`islands::STEP_MAX` = 3), so:

- protected (default build settings): the proof refuses those ticks
  (`motion` fallback, sequential), verify (every pass) finds nothing;
- `HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1` (test build): the plan assumes a
  step of 4 and the check after the pass sees the flyer move 10: alarm, the
  tick is redone sequentially, verify finds nothing;
- with also `HORIZON_ENTITY_UNSAFE_REACH=3` (islands planned too small for
  the flyer to be with the herd) and `HORIZON_ENTITY_UNSAFE_NO_GUARD=1` (no
  redo): the flyer's island and the herd's are separate, their pushes are
  lost, and verify reports the difference.

With the real reach (follow range >= 16 plus the margin) a vanilla-legal
speed cannot by itself move a mob from one island into another within one
tick, which is why the last control also shrinks the reach.

  py -3 tools/voxaryn/repro_fast_mover.py --exe .local/p1-unsafe-target/release/horizon-client.exe --out X.json
  py -3 tools/voxaryn/repro_fast_mover.py --exe ... --env HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1 --out Y.json
"""
import argparse, json, re, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entity_bench as b  # noqa: E402

Y = 200
B1 = (1060, 1000)  # herd B1's corner: cows at x 1060.5 + 2i, z 1010.5 + 2j
B2 = (1300, 1000)
# 9.5 blocks before B1's first column: one tick at 10 lands on its cow.
FLYER_X = B1[0] + 0.5 - 9.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--flyers", type=int, default=10)
    ap.add_argument("--speed", type=float, default=10.0,
                    help="Motion x of a flyer; vanilla's Entity.load keeps at most 10 (a larger one loads as 0)")
    ap.add_argument("--out", default=str(b.BASE / "repro_fast_mover.json"))
    ap.add_argument("--env", action="append", default=[], help="extra K=V for the server")
    args = ap.parse_args()
    if abs(args.speed) > 10.0:
        print(f"note: Motion {args.speed} is beyond vanilla's 10 and loads as 0 (the flyers will not move)")
    exe = Path(args.exe).resolve()
    d = b.BASE / "run_repro_fast"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "eula.txt").write_text("eula=true\n")
    port = b.free_port()
    props = dict(b.PROPERTIES, **{"server-port": str(port)})
    (d / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    env = {"HORIZON_ENTITY_THREADS": "6", "HORIZON_ENTITY_VERIFY": "1"}
    env.update(kv.split("=", 1) for kv in args.env)
    srv = b.Server(exe, d, port, env)
    bot = None
    try:
        srv.wait_log(r"Done \(", 300)
        bot = subprocess.Popen([str(b.BOTS), "--server", f"127.0.0.1:{port}", "--bots", "1", "--behaviour", "idle",
                                "--seconds", "600", "--seed", "12345", "--name", b.BOT_NAME],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        srv.wait_log(r"joined the game", 90)
        rules = b.apply_rules(srv)
        srv.console("time set day")
        srv.console("forceload add 990 990 1340 1040")
        time.sleep(8)
        # One stone floor from the flyers' start to past B1, and one for B2.
        srv.console(f"fill 1040 {Y} 995 1090 {Y} 1035 minecraft:stone")
        srv.console(f"fill {B2[0] - 5} {Y} 995 {B2[0] + 35} {Y} 1035 minecraft:stone")
        for (x, z) in (B1, B2):
            for k in range(100):
                srv.console(f"summon minecraft:cow {x + (k % 10) * 2}.5 {Y + 1} {z + 10 + (k // 10) * 2}.5")
        time.sleep(3)
        srv.console(b.clean_selector(["cow"]))
        time.sleep(10)
        mark = len(srv.log_text())
        srv.console("tick sprint 400")
        for i in range(args.flyers):
            z = B1[1] + 10 + (i % 10) * 2
            srv.console(f"summon minecraft:cow {FLYER_X} {Y + 1} {z}.5 {{Motion:[{args.speed},0.0,0.0]}}")
            time.sleep(0.5)
        srv.wait_log(b.REPORT.pattern, 600, after=mark)
        window = srv.log_text()[mark:]
        res = {"exe": exe.name, "env": args.env, "rules_ok": all(rules.get(k) == v for k, v in b.RULES.items()),
               "flyers": args.flyers, "speed": args.speed,
               "differs_lines": len(b.DIFFERS.findall(window)),
               "alarms": len(re.findall(r"moved further than the plan|drew from the level random", window)),
               "redone_lines": len(re.findall(r"the tick is redone sequentially", window)),
               "not_redone_lines": len(re.findall(r"NOT redone", window)),
               "first_alarm": next(iter(re.findall(r"parallel entity pass: (.{0,200})", window)), None),
               "first_difference": next(iter(re.findall(r"differs from the sequential pass: (.{0,300})", window)), None)}
        res.update(b.summarize_parstats(window))
        # Cows in herd B1's area afterwards (the flyers may overshoot it and
        # push herd cows out, so this is a rough number).
        mk = len(srv.log_text())
        srv.console(f"execute if entity @e[type=minecraft:cow,x={B1[0] - 3},y={Y},z=990,dx=30,dy=4,dz=50]")
        m = srv.wait_log(r"Test passed\. Count: (\d+)|Test failed", 15, after=mk)
        res["cows_at_b1_after"] = int(m.group(1)) if m.group(1) else 0
        # Flyers still where they were summoned: all of them when the motion
        # was dropped at load, none when they flew.
        mk = len(srv.log_text())
        srv.console(f"execute if entity @e[type=minecraft:cow,x={FLYER_X - 2},y={Y},z={B1[1]},dx=4,dy=4,dz=40]")
        m = srv.wait_log(r"Test passed\. Count: (\d+)|Test failed", 15, after=mk)
        res["flyers_left_at_start"] = int(m.group(1)) if m.group(1) else 0
        Path(args.out).write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(res, indent=2, ensure_ascii=False))
    finally:
        if bot and bot.poll() is None:
            bot.terminate()
        srv.stop()


if __name__ == "__main__":
    main()
