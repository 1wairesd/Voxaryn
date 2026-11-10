#!/usr/bin/env python3
"""Voxaryn entity-tick benchmarks and correctness runs (P0, P1, P1.1).

One fresh dedicated server per run (fresh world, seed 12345), one idle bot.
Measures a fixed number of GAME TICKS with `/tick sprint N`; the sprint report
and the parallel pass's PARSTATS lines are read from the run's
`logs/latest.log`. See tools/voxaryn/README.md for setup and examples.

  py -3 tools/voxaryn/entity_bench.py bench  --exe X --scenarios herds_8x250 --threads 0,6 --reps 3
  py -3 tools/voxaryn/entity_bench.py verify --exe X --scenarios species_land_8x250 --threads 2,6 --orders largest,shuffle:7

Outputs go to .local/voxaryn-bench/ (git-ignored): one JSON per invocation
(--out) with every run's numbers, and the last run's server folder.
"""
import argparse, json, os, re, shutil, socket, statistics, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
L = ROOT / ".local"
RES = L / "horizon-client" / "26.3"
EXE_SUFFIX = ".exe" if os.name == "nt" else ""
BOTS = ROOT / "target" / "release" / f"horizon-bots{EXE_SUFFIX}"
BASE = L / "voxaryn-bench"
BOT_NAME = "P1Bot"

REPORT = re.compile(r"Sprint completed with ([0-9.]+) ticks per second, or ([0-9.]+) ms per tick")
PARSTATS = re.compile(
    r"PARSTATS passes (\d+), parallel (\d+), threads (\d+), islands ([0-9.]+), largest ([0-9.]+), "
    r"mobs ([0-9.]+), work ([0-9.]+) ms, critical ([0-9.]+) ms, bound ([0-9.]+)x, plan ([0-9.]+) ms, "
    r"split ([0-9.]+) ms, run ([0-9.]+) ms, merge ([0-9.]+) ms, verify ([0-9.]+) ms, verified (\d+), "
    r"mismatches (\d+), fallbacks \[(.*?)\], blocking ([^,\s]+)"
    # P1.1 fields (absent in P1 binaries)
    r"(?:, gate ([0-9.]+) ms, busy_max ([0-9.]+) ms, est ([0-9.]+)x, allocs ([0-9.]+), alloc_kb ([0-9.]+)"
    r"(?:, step ([0-9.]+))?(?:, guard ([0-9.]+) ms, tripped (\d+), redone (\d+))?)?")
PAR_FIELDS = ["passes", "parallel", "threads", "islands", "largest", "mobs", "work_ms", "critical_ms",
              "bound", "plan_ms", "split_ms", "run_ms", "merge_ms", "verify_ms", "verified", "mismatches",
              "fallbacks", "blocking", "gate_ms", "busy_max_ms", "est", "allocs", "alloc_kb", "step", "guard_ms", "tripped", "redone"]
SPANS = re.compile(r"Horizon: SPANS (.*)")
SPAN_ITEM = re.compile(r"([a-z_]+) ([0-9.]+)(?:/[0-9.]+)?")
DIFFERS = re.compile(r"parallel entity pass differs|drew from the level random")
SLOW = re.compile(r"slow tick ([0-9.]+) ms")
TICK_AVG = re.compile(r"Average time per tick: ([0-9.]+) ?ms")
TICK_PCT = re.compile(r"P50: ([0-9.]+) ?ms P95: ([0-9.]+) ?ms P99: ([0-9.]+) ?ms")
RULE = re.compile(r"Game ?rule (\w+) is currently set to:? (\w+)")
SETUP_ERRORS = re.compile(r"Incorrect argument|Unknown or incomplete command|Unknown command|No player was found")

# Game rules (26.3 names; the camelCase names of older versions are rejected).
RULES = {"spawn_mobs": "false", "spawn_monsters": "false", "spawn_patrols": "false",
         "spawn_phantoms": "false", "spawn_wandering_traders": "false", "mob_griefing": "false",
         "advance_time": "false"}

# Arena scenes keep only the species they were built with (chunk generation
# spawns other animals whatever the spawn rules say).
def clean_selector(keep):
    types = ",".join(f"type=!minecraft:{t}" for t in ["player"] + sorted(set(keep)))
    return f"kill @e[{types}]"

PROPERTIES = {
    # Local benchmark servers accept only the loopback bot/client.
    "server-ip": "127.0.0.1",
    "level-seed": "12345", "view-distance": "10", "simulation-distance": "10",
    "online-mode": "false", "white-list": "false", "gamemode": "creative",
    "force-gamemode": "true", "spawn-protection": "0", "difficulty": "hard",
    "max-players": "20", "pause-when-empty-seconds": "-1",
}

# Arenas: closed stone boxes in the sky (32 x 32, 2 blocks of headroom), far
# apart, their chunks force-loaded. Mobs cannot fall, leave or meet the bot.
ARENA_Y = 200


def arenas(count, spacing):
    side = int((count ** 0.5) + 0.999)
    return [(1000 + (k % side) * spacing, 1000 + (k // side) * spacing) for k in range(count)]


VERIFIED_P1 = ["cow", "mooshroom", "pig", "sheep", "chicken", "zombie"]
SCENARIOS = {
    # P0's scenarios (summoned at 0 64 0), for regression and fallback cost.
    "cows_500": {"kind": "pile", "mobs": [("cow", 500)], "time": "day"},
    "cows_1000": {"kind": "pile", "mobs": [("cow", 1000)], "time": "day"},
    "cows_2000": {"kind": "pile", "mobs": [("cow", 2000)], "time": "day"},
    "cows_spread": {"kind": "spread", "mobs": [("cow", 500)], "time": "day"},
    "zombies_2000": {"kind": "pile", "mobs": [("zombie", 2000)], "time": "midnight"},
    "mixed": {"kind": "pile", "mobs": [("cow", 500), ("zombie", 500), ("skeleton", 400),
                                        ("sheep", 300), ("spider", 300)], "time": "midnight"},
    # Guaranteed independent groups (arenas).
    "herds_8x250": {"kind": "arenas", "count": 8, "spacing": 128, "mobs": [("cow", 250)], "time": "day"},
    "herds_16x125": {"kind": "arenas", "count": 16, "spacing": 96, "mobs": [("cow", 125)], "time": "day"},
    "zombies_8x250": {"kind": "arenas", "count": 8, "spacing": 128, "mobs": [("zombie", 250)], "time": "midnight"},
    "mixedv_8x250": {"kind": "arenas", "count": 8, "spacing": 128,
                     "mobs": [("cow", 60), ("pig", 50), ("sheep", 50), ("chicken", 40), ("zombie", 50)],
                     "time": "midnight"},
    "herds_1x1000_7x100": {"kind": "arenas_custom", "spacing": 128,
                           "groups": [[("cow", 1000)]] + [[("cow", 100)]] * 7, "time": "day"},
    # P1.1: the species added to the verified set.
    "species_land_8x250": {"kind": "arenas", "count": 8, "spacing": 128,
                           "mobs": [("skeleton", 50), ("spider", 50), ("creeper", 50), ("goat", 50),
                                    ("zombie", 50)], "time": "midnight"},
    "species_water_8x250": {"kind": "arenas", "count": 8, "spacing": 128, "water": True,
                            "mobs": [("squid", 70), ("glow_squid", 60), ("dolphin", 40), ("drowned", 50),
                                     ("nautilus", 30)], "time": "midnight"},
    # P1.1 correctness: a player among the mobs (the bot, in survival and
    # unkillable, inside arena 0 with zombies, skeletons, spiders, creepers, cows).
    "player_arena": {"kind": "arenas", "count": 8, "spacing": 128, "bot_in": 0,
                     "count_after": {"arrows": "@e[type=minecraft:arrow]",
                                     "in_player_arena": "@e[type=!minecraft:player,x=1000,y=200,z=1000,dx=32,dy=4,dz=32]"},
                     "mobs": [("zombie", 40), ("skeleton", 30), ("spider", 30), ("creeper", 20),
                              ("cow", 80), ("chicken", 50)], "time": "midnight"},
    # P1.1 correctness: babies, in-love animals (breeding across the arena),
    # effects and burning mobs.
    "effects_breed_8x250": {"kind": "arenas", "count": 8, "spacing": 128, "effects": True,
                            "count_after": {"mobs": "@e[type=!minecraft:player]"},
                            "mobs": [("cow", 90), ("pig", 80), ("sheep", 80)], "time": "day"},
    # P1.1 correctness: riders and leads keep the whole tick sequential.
    "riders_leash": {"kind": "arenas", "count": 8, "spacing": 128, "links": True,
                     "mobs": [("cow", 200)], "time": "day"},
    # P1.2 negative test: axolotls (their brain draws from the level random),
    # let through only by a test build (HORIZON_ENTITY_UNSAFE_SPECIES=axolotl).
    "axolotls_8x100": {"kind": "arenas", "count": 8, "spacing": 128, "water": True,
                       "mobs": [("axolotl", 100)], "time": "day"},
    # P1.3 validation (negative test, unsafe build only): wolves hunt skeletons and alert each
    # other (HurtByTargetGoal.alertOthers -> SetTarget) when an arrow hits one; with
    # HORIZON_ENTITY_UNSAFE_SPECIES=wolf and a small HORIZON_ENTITY_UNSAFE_REACH an alert lands in
    # another island and the stranded-hit guard must redo the tick.
    "wolves_skeletons_8x": {"kind": "arenas", "count": 8, "spacing": 128,
                            "mobs": [("wolf", 40), ("skeleton", 30)], "time": "midnight"},
    # P2 feasibility: one large island at three crowdings (one pen each).
    "pen_2000_wide": {"kind": "arenas", "count": 1, "spacing": 128, "size": 96, "mobs": [("cow", 2000)],
                      "time": "day"},
    "pen_2000_mid": {"kind": "arenas", "count": 1, "spacing": 128, "size": 64, "mobs": [("cow", 2000)],
                     "time": "day"},
    "pen_2000_tight": {"kind": "arenas", "count": 1, "spacing": 128, "size": 40, "mobs": [("cow", 2000)],
                       "time": "day"},
}
for name, spec in SCENARIOS.items():
    if spec["kind"] in ("arenas", "arenas_custom"):
        kinds = [m for m, _ in spec.get("mobs", [])] + [m for g in spec.get("groups", []) for m, _ in g]
        spec.setdefault("keep", kinds)


class Server:
    def __init__(self, exe, directory, port, env_extra):
        self.dir = directory
        env = dict(os.environ, HORIZON_NO_AUDIO="1")
        for k in list(env):
            if k.startswith("HORIZON_ENTITY_") or k in ("HORIZON_PATH_STATS", "HORIZON_SPIKES"):
                env.pop(k)
        env.update(env_extra)
        self.p = subprocess.Popen(
            [str(exe), "--resources", str(RES), "--dedicated", "--nogui",
             "--universe", str(directory), "--world", "world", "--port", str(port)],
            cwd=str(directory), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)

    def log_text(self):
        try:
            return (self.dir / "logs" / "latest.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_log(self, pattern, timeout, after=0):
        rx = re.compile(pattern)
        end = time.time() + timeout
        while time.time() < end:
            m = rx.search(self.log_text()[after:])
            if m:
                return m
            if self.p.poll() is not None:
                raise RuntimeError("server exited:\n" + self.log_text()[-3000:])
            time.sleep(0.25)
        raise RuntimeError(f"no {pattern!r} within {timeout}s")

    def console(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def stop(self):
        if self.p.poll() is None:
            try:
                self.console("stop")
                self.p.wait(timeout=180)
            except Exception:
                self.p.kill()
                self.p.wait()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def count_entities(srv, selector):
    """`execute if entity`'s count, or None without an answer."""
    mk = len(srv.log_text())
    srv.console(f"execute if entity {selector}")
    try:
        cm = srv.wait_log(r"Test passed\. Count: (\d+)|Test failed", 15, after=mk)
    except RuntimeError:
        return None
    return int(cm.group(1)) if cm.group(1) else 0


def apply_rules(srv):
    """Sets the rules and reads them back; returns {rule: value as reported}."""
    for k, v in RULES.items():
        srv.console(f"gamerule {k} {v}")
    srv.console("difficulty hard")
    mark = len(srv.log_text())
    for k in RULES:
        srv.console(f"gamerule {k}")
    deadline = time.time() + 20
    got = {}
    while time.time() < deadline and len(got) < len(RULES):
        got = dict(RULE.findall(srv.log_text()[mark:]))
        time.sleep(0.25)
    return got


def build(srv, spec):
    srv.console(f"time set {spec['time']}")
    kind = spec["kind"]
    if kind == "pile":
        for mob, n in spec["mobs"]:
            for _ in range(n):
                srv.console(f"summon minecraft:{mob} 0 64 0")
        return
    if kind == "spread":
        for (dx, dz) in [(0, 0), (1500, 0), (0, 1500), (1500, 1500)]:
            for mob, n in spec["mobs"]:
                for _ in range(n):
                    srv.console(f"summon minecraft:{mob} {dx} 64 {dz}")
        return
    if kind == "arenas":
        places = arenas(spec["count"], spec["spacing"])
        groups = [spec["mobs"]] * spec["count"]
    else:
        groups = spec["groups"]
        places = arenas(len(groups), spec["spacing"])
    size = spec.get("size", 32)
    for (x, z) in places:
        srv.console(f"forceload add {x - 24} {z - 24} {x + size + 8} {z + size + 8}")
    time.sleep(8)  # let the forced chunks load
    inner = size - 4
    for a, ((x, z), mobs) in enumerate(zip(places, groups)):
        srv.console(f"fill {x} {ARENA_Y} {z} {x + size - 1} {ARENA_Y + 3} {z + size - 1} minecraft:stone hollow")
        if spec.get("water"):
            # One block of water on the floor, air above (dolphins breathe).
            srv.console(f"fill {x + 1} {ARENA_Y + 1} {z + 1} {x + size - 2} {ARENA_Y + 1} {z + size - 2} minecraft:water")
        k = 0
        for mob, n in mobs:
            for _ in range(n):
                px, pz = x + 2 + (k * 7) % inner, z + 2 + (k * 11 // inner) % inner
                nbt = ""
                if spec.get("effects"):
                    # A third babies, a quarter in love (they breed), some burning.
                    if k % 3 == 0:
                        nbt = "{Age:-24000}"
                    elif k % 4 == 1:
                        nbt = "{InLove:600}"
                    elif k % 11 == 2:
                        nbt = "{Fire:200s}"
                srv.console(f"summon minecraft:{mob} {px}.5 {ARENA_Y + 1} {pz}.5 {nbt}".rstrip())
                k += 1
        if spec.get("links") and a == 0:
            # A pig ridden by a zombie, and a cow on a lead tied to a fence.
            srv.console(f"summon minecraft:pig {x + 16}.5 {ARENA_Y + 1} {z + 16}.5 "
                        "{Passengers:[{id:\"minecraft:zombie\",PersistenceRequired:1b}]}")
            srv.console(f"setblock {x + 20} {ARENA_Y + 1} {z + 20} minecraft:oak_fence")
            srv.console(f"summon minecraft:cow {x + 21}.5 {ARENA_Y + 1} {z + 20}.5 "
                        f"{{leash:[I;{x + 20},{ARENA_Y + 1},{z + 20}]}}")
    if spec.get("effects"):
        for eff in ("speed", "slowness", "jump_boost", "regeneration", "glowing", "invisibility"):
            srv.console(f"effect give @e[type=!minecraft:player,limit=150,sort=random] minecraft:{eff} 60 1")


def setup_bot(srv, spec):
    """Moves the bot into an arena (survival, unkillable) when the scenario asks."""
    if "bot_in" not in spec:
        return
    x, z = arenas(spec["count"], spec["spacing"])[spec["bot_in"]]
    # The bot is the only player (horizon-bots names it P1Bot0).
    srv.console("gamemode survival @a")
    srv.console("effect give @a minecraft:resistance infinite 255 true")
    srv.console("effect give @a minecraft:regeneration infinite 255 true")
    srv.console("effect give @a minecraft:saturation infinite 255 true")
    srv.console(f"tp @a {x + 16}.5 {ARENA_Y + 1} {z + 16}.5")


class FreqSampler:
    """Samples Windows' '% Processor Performance' (actual vs base frequency)."""

    def __init__(self, path):
        self.path, self.p = path, None

    def start(self):
        if os.name != "nt":
            return
        # WMI class names are English on every Windows locale (typeperf's
        # counter paths are localized).
        script = ("while ($true) { (Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation "
                  "-Filter \"Name='_Total'\").PercentProcessorPerformance | "
                  f"Out-File -Append -Encoding ascii '{self.path}'; Start-Sleep -Milliseconds 900 }}")
        self.p = subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if not self.p:
            return None
        self.p.kill()
        self.p.wait()
        try:
            vals = [float(v) for v in self.path.read_text(encoding="ascii", errors="replace").split() if v.strip()]
            return round(statistics.fmean(vals), 1) if vals else None
        except (OSError, ValueError):
            return None


def summarize_parstats(window):
    par = [p.groups() for p in PARSTATS.finditer(window)]
    if not par:
        return {}
    def num(i):
        v = [float(p[i]) for p in par if p[i] is not None]
        return round(statistics.fmean(v), 3) if v else None
    tot = lambda i: sum(int(p[i]) for p in par)
    out = {"passes": tot(0), "parallel_passes": tot(1)}
    for i, name in enumerate(PAR_FIELDS):
        if name in ("passes", "parallel", "threads", "verified", "mismatches", "fallbacks", "blocking"):
            continue
        out[name] = num(i)
    out["tripped"] = sum(int(p[25] or 0) for p in par)
    out["redone"] = sum(int(p[26] or 0) for p in par)
    out["verified"] = tot(14)
    out["mismatches"] = tot(15)
    out["fallbacks"] = sorted({f for p in par for f in p[16].split() if f})
    out["blocking"] = sorted({p[17] for p in par if p[17] != "-"})
    return out


PATHSTATS_TICK = re.compile(r"Horizon: PATHSTATS tick ([0-9.]+) ms")
SPECSTATS = re.compile(
    r"SPECSTATS passes (\d+), mobs ([0-9.]+), accepted ([0-9.]+)%, recomputed ([0-9.]+), "
    r"rejects pending ([0-9.]+) reads ([0-9.]+) targets ([0-9.]+) rng ([0-9.]+) brain ([0-9.]+), "
    r"cascades ([0-9.]+), accepted_after_recompute ([0-9.]+), clone ([0-9.]+) ms, phase1 ([0-9.]+) ms, "
    r"phase1_work ([0-9.]+) ms, phase2 ([0-9.]+) ms, recompute ([0-9.]+) ms, spec ([0-9.]+) ms, "
    r"seq ([0-9.]+) ms, speedup ([0-9.]+)x, cheap (\d+)/(\d+), deep (\d+)/(\d+), skipped \[(.*?)\]"
    r"(?:, groups proven (\d+), step ([0-9.]+), groups ([0-9.]+), largest ([0-9.]+), alone ([0-9.]+), group_ms ([0-9.]+)"
    r"(?:, oracle (\d+) step ([0-9.]+) groups ([0-9.]+) largest ([0-9.]+) alone ([0-9.]+)"
    r"(?:, fixed 0\.3/0\.5/0\.8 groups ([0-9.]+)/([0-9.]+)/([0-9.]+) largest ([0-9.]+)/([0-9.]+)/([0-9.]+))?)?)?")
SPEC_FIELDS = ["passes", "mobs", "accepted_pct", "recomputed", "rej_pending", "rej_reads", "rej_targets",
               "rej_rng", "rej_brain", "cascades", "accepted_after_recompute", "clone_ms", "phase1_ms",
               "phase1_work_ms", "phase2_ms", "recompute_ms", "spec_ms", "seq_ms", "speedup"]


def summarize_spec(window):
    """Means of the P2 experiment's SPECSTATS lines (passes that ran)."""
    rows = [m.groups() for m in SPECSTATS.finditer(window)]
    ran = [r for r in rows if int(r[0]) > 0]
    if not rows:
        return {}
    out = {k: round(statistics.fmean(float(r[i]) for r in ran), 3) for i, k in enumerate(SPEC_FIELDS)} if ran else {}
    out["spec_passes"] = sum(int(r[0]) for r in ran)
    out["cheap_ok"] = f"{sum(int(r[19]) for r in rows)}/{sum(int(r[20]) for r in rows)}"
    out["deep_ok"] = f"{sum(int(r[21]) for r in rows)}/{sum(int(r[22]) for r in rows)}"
    out["exact"] = all(r[19] == r[20] and r[21] == r[22] for r in rows)
    out["skipped"] = sorted({s for r in rows for s in r[23].split() if s})
    g = [r for r in ran if r[24] is not None and int(r[24]) > 0]
    if g:
        for i, k in enumerate(["step", "groups", "largest_group", "alone", "group_ms"], start=25):
            out[k] = round(statistics.fmean(float(r[i]) for r in g), 3)
        out["group_proven_passes"] = sum(int(r[24]) for r in g)
    q = [r for r in ran if r[30] is not None and int(r[30]) > 0]
    if q:
        for i, k in enumerate(["oracle_step", "oracle_groups", "oracle_largest", "oracle_alone"], start=31):
            out[k] = round(statistics.fmean(float(r[i]) for r in q), 3)
        f = [r for r in q if r[35] is not None]
        if f:
            for i, k in enumerate(["groups_0.3", "groups_0.5", "groups_0.8", "largest_0.3", "largest_0.5", "largest_0.8"], start=35):
                out[k] = round(statistics.fmean(float(r[i]) for r in f), 1)
    return out
ENTSTATS = re.compile(r"ENTSTATS tick [0-9.]+ ms, mobs (\d+), near_calls (\d+), near_candidates (\d+), "
                      r"near_full_scans (\d+), los_calls (\d+), los_steps (\d+), push_calls (\d+), "
                      r"push_targets (\d+), cramming (\d+)")
P2STATS = re.compile(r"P2STATS passes (\d+), ticked (\d+), stale_push_reads (\d+) \(([0-9.]+)%\), "
                     r"neighbours/mob ([0-9.]+)")


def summarize_spans(window):
    """Mean ms per tick of each span. A SPANS line is the spans of one tick
    (every 20th), only those of at least 0.5 ms; `tick` is that tick's time."""
    sums, n = {}, 0
    for m in SPANS.finditer(window):
        n += 1
        for name, ms in SPAN_ITEM.findall(m.group(1)):
            sums[name] = sums.get(name, 0.0) + float(ms)
    if not n:
        return {}
    out = {k: round(v / n, 3) for k, v in sorted(sums.items(), key=lambda kv: -kv[1])}
    ticks = [float(t) for t in PATHSTATS_TICK.findall(window)]
    if ticks:
        out["tick"] = round(statistics.fmean(ticks), 3)
    out["samples"] = n
    ent = [tuple(map(int, e)) for e in ENTSTATS.findall(window)]
    if ent:
        names = ["mobs", "near_calls", "near_candidates", "near_full_scans", "los_calls", "los_steps",
                 "push_calls", "push_targets", "cramming"]
        out["entstats"] = {k: round(statistics.fmean(e[i] for e in ent), 1) for i, k in enumerate(names)}
    p2 = P2STATS.findall(window)
    if p2:
        out["p2"] = {"stale_pct": round(statistics.fmean(float(x[3]) for x in p2), 2),
                     "neighbours_per_mob": round(statistics.fmean(float(x[4]) for x in p2), 2),
                     "ticked_per_pass": round(statistics.fmean(int(x[1]) / int(x[0]) for x in p2), 1)}
    return out


def run(exe, scenario, args, threads, order=None, verify=False):
    spec = SCENARIOS[scenario]
    d = BASE / f"run_{scenario}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "eula.txt").write_text("eula=true\n")
    port = free_port()
    props = dict(PROPERTIES, **{"server-port": str(port)})
    (d / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    env = {}
    if threads > 0:
        env["HORIZON_ENTITY_THREADS"] = str(threads)
    if verify:
        env["HORIZON_ENTITY_VERIFY"] = str(args.verify_every)
    if order:
        env["HORIZON_ENTITY_ORDER"] = order
    if args.spans:
        env["HORIZON_PATH_STATS"] = "1"
    for kv in args.env:
        k, v = kv.split("=", 1)
        env[k] = v
    srv = Server(exe, d, port, env)
    bot = None
    burners = []
    try:
        srv.wait_log(r"Done \(", 300)
        bot = subprocess.Popen([str(BOTS), "--server", f"127.0.0.1:{port}", "--bots", "1", "--behaviour", "idle",
                                "--seconds", "3000", "--seed", "12345", "--name", BOT_NAME],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        srv.wait_log(r"joined the game", 90)
        time.sleep(2)
        setup_mark = len(srv.log_text())
        rules = apply_rules(srv)
        build(srv, spec)
        clean = spec["kind"] in ("arenas", "arenas_custom") or args.clean
        keep = spec.get("keep", [m for m, _ in spec.get("mobs", [])])
        if clean:
            time.sleep(2)
            srv.console(clean_selector(keep))
        setup_bot(srv, spec)
        time.sleep(args.warmup)
        if clean:
            srv.console(clean_selector(keep))
            time.sleep(1)
        setup_errors = SETUP_ERRORS.findall(srv.log_text()[setup_mark:])
        for _ in range(args.burners):
            burners.append(subprocess.Popen([sys.executable, "-c", "while True: pass"]))
        # P1.3: the living mobs at the start and the end of the window
        # (cramming kills crowds: runs are compared only at equal counts).
        mobs_start = count_entities(srv, "@e[type=!minecraft:player]")
        freq = FreqSampler(d / "freq.csv") if args.freq else None
        if freq:
            freq.start()
        mark = len(srv.log_text())
        srv.console(f"tick sprint {args.ticks}")
        m = srv.wait_log(REPORT.pattern, 3000, after=mark)
        freq_pct = freq.stop() if freq else None
        for b in burners:
            b.kill()
        burners = []
        window = srv.log_text()[mark:]
        res = {"scenario": scenario, "threads": threads, "order": order, "verify": verify,
               "exe": Path(exe).name, "ms_per_tick": float(m.group(2)),
               "rules_ok": all(rules.get(k) == v for k, v in RULES.items()),
               "setup_errors": len(setup_errors),
               "differs_lines": len(DIFFERS.findall(window)),
               "slow_over_100": sum(1 for s in SLOW.findall(window) if float(s) > 100)}
        res["mobs_start"] = mobs_start
        res["mobs_end"] = count_entities(srv, "@e[type=!minecraft:player]")
        if freq_pct is not None:
            res["cpu_perf_pct"] = freq_pct
        if args.burners:
            res["burners"] = args.burners
        res.update(summarize_parstats(window))
        spec_summary = summarize_spec(window)
        if spec_summary:
            res["spec"] = spec_summary
        # Safety-net lines over the whole run (they may fire while building).
        whole = srv.log_text()
        res["alarms_total"] = len(re.findall(r"drew from the level random|moved further than the plan", whole))
        res["redone_total"] = len(re.findall(r"the tick is redone sequentially", whole))
        res["not_redone_total"] = len(re.findall(r"NOT redone", whole))
        res["mismatch_lines_total"] = len(re.findall(r"differs from the sequential pass", whole))
        # What the scene did, counted after the window (e.g. skeletons'
        # arrows at the player, babies of the breeding animals).
        for label, sel in spec.get("count_after", {}).items():
            mk = len(srv.log_text())
            srv.console(f"execute if entity {sel}")
            try:
                cm = srv.wait_log(r"Test passed\. Count: (\d+)|Test failed", 15, after=mk)
                res[f"count_{label}"] = int(cm.group(1)) if cm.group(1) else 0
            except RuntimeError:
                res[f"count_{label}"] = None
        if args.spans:
            res["spans"] = summarize_spans(window)
        if args.steady > 0:
            time.sleep(6)
            q = []
            for _ in range(max(1, args.steady // 5)):
                mk = len(srv.log_text())
                srv.console("tick query")
                try:
                    pm = srv.wait_log(TICK_PCT.pattern, 10, after=mk)
                    am = TICK_AVG.search(srv.log_text()[mk:])
                    q.append((float(am.group(1)) if am else None, *map(float, pm.groups())))
                except RuntimeError:
                    pass
                time.sleep(5)
            if q:
                res["steady"] = {
                    "n": len(q),
                    "avg": round(statistics.fmean(x[0] for x in q if x[0] is not None), 2),
                    "p50": round(statistics.fmean(x[1] for x in q), 2),
                    "p95": round(statistics.fmean(x[2] for x in q), 2),
                    "p99": round(statistics.fmean(x[3] for x in q), 2),
                    "p99_max": max(x[3] for x in q),
                }
        return res
    finally:
        for b in burners:
            b.kill()
        if bot and bot.poll() is None:
            bot.terminate()
        srv.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["bench", "verify"])
    ap.add_argument("--exe", required=True, help="horizon-client binary to run as the server")
    ap.add_argument("--scenarios", required=True, help=f"comma list of: {', '.join(SCENARIOS)}")
    ap.add_argument("--threads", default="0,6", help="HORIZON_ENTITY_THREADS values (0 = sequential P0 pass)")
    ap.add_argument("--orders", default="largest", help="verify mode: island orders (largest, smallest, shuffle:N)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--warmup", type=int, default=20, help="seconds after building, outside the window")
    ap.add_argument("--ticks", type=int, default=600, help="the window: /tick sprint N")
    ap.add_argument("--out", required=True, help="results JSON")
    ap.add_argument("--verify-every", type=int, default=1, help="verify mode: HORIZON_ENTITY_VERIFY=N")
    ap.add_argument("--env", action="append", default=[], help="extra K=V for the server")
    ap.add_argument("--rule", action="append", default=[],
                    help="extra game rule K=V (P1.3: max_entity_cramming=0 keeps the crowds of earlier runs alive)")
    ap.add_argument("--clean", action="store_true", help="kill unverified species in any scenario")
    ap.add_argument("--steady", type=int, default=0, help="seconds of /tick query at 20 TPS after the sprint")
    ap.add_argument("--spans", action="store_true", help="HORIZON_PATH_STATS=1 and record the SPANS lines")
    ap.add_argument("--freq", action="store_true", help="sample the CPU's Processor Performance counter (Windows)")
    ap.add_argument("--burners", type=int, default=0, help="busy-loop processes during the window")
    args = ap.parse_args()
    for kv in args.rule:
        k, _, v = kv.partition("=")
        RULES[k] = v
    exe = Path(args.exe).resolve()
    for need in (exe, RES, BOTS):
        if not need.exists():
            sys.exit(f"missing {need} (see tools/voxaryn/README.md)")
    BASE.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    threads = [int(t) for t in args.threads.split(",")]
    orders = args.orders.split(",")
    meta = {"exe": str(exe), "args": vars(args), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0], "platform": sys.platform}
    for sc in args.scenarios.split(","):
        for rep in range(args.reps):
            seq = threads if rep % 2 == 0 else list(reversed(threads))  # ABBA
            for t in seq:
                for o in (orders if args.mode == "verify" else [None]):
                    label = f"[{sc}] rep {rep + 1}/{args.reps} threads {t}" + (f" order {o}" if o else "")
                    print(label, flush=True)
                    try:
                        r = run(exe, sc, args, t, order=o, verify=(args.mode == "verify"))
                    except Exception as e:
                        r = {"scenario": sc, "threads": t, "order": o, "error": str(e)[:400]}
                    r["rep"] = rep + 1
                    print("   ", json.dumps(r, ensure_ascii=False), flush=True)
                    results.append(r)
                    out.write_text(json.dumps({**meta, "results": results}, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
                    time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
