# tools/voxaryn — entity-tick benchmarks and correctness runs

Dev tools of the Voxaryn research (docs/research/): the measurements behind
P0 (entity query reuse), P1 (parallel islands of mobs) and P1.1. Python only
drives a real dedicated server built from this tree; every number is read
from that server's own log.

Nothing these scripts write belongs in Git: worlds, logs and results go to
`.local/voxaryn-bench/` and binaries to `.local/baseline/` (both under the
ignored `.local/`).

## Setup

1. Vanilla resources in `.local/horizon-client/26.3` (`horizon-prepare`, see
   docs/parity-workflow.md).
2. The load-test bot: `cargo build --release -p horizon-bots` (expected at
   `target/release/horizon-bots[.exe]`).
3. The server binaries, `--release` (thin LTO, as players get it). Use a
   separate target directory so the builds don't evict each other:

   ```sh
   CARGO_TARGET_DIR=.local/p1-target cargo build --release -p horizon-client --bin horizon-client --features parallel-entities
   ```

   | Binary (copy to `.local/baseline/`) | Features | Use |
   |---|---|---|
   | `horizon-client-p11-seq` | (default) | the sequential pass exactly as shipped; regression baseline |
   | `horizon-client-p11-par` | `parallel-entities` | P1 pass; `HORIZON_ENTITY_THREADS=0` runs the P0 path in the same binary |
   | `horizon-client-p11-unsafe` | `parallel-entities-unsafe-test` | **test only**: honours `HORIZON_ENTITY_UNSAFE_REACH` (negative test of verify mode) |
   | `horizon-client-p11-alloc` | `parallel-entities,alloc-stats` | allocation counts (its atomics distort timings: count only) |
   | `horizon-client-p11-mimalloc` | `parallel-entities,exp-mimalloc` | allocator A/B |
   | `horizon-client-p11-phases` | `phase-spans` | spans around the phases of a mob's tick (with `--spans`) |
   | `horizon-client-p12-par` | `parallel-entities` | P1.2: movement proof, level random exclusion, guard and redo |
   | `horizon-client-p12-unsafe` | `parallel-entities-unsafe-test` | P1.2 negative tests: `HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1`, `HORIZON_ENTITY_UNSAFE_NO_GUARD=1`, `HORIZON_ENTITY_UNSAFE_SPECIES=axolotl` |
   | `horizon-client-p2-spec` | `p2-speculate` | the P2 shadow experiment (`HORIZON_P2_SPECULATE=1`, `HORIZON_P2_DEEP_EVERY=N`); the world always takes the sequential pass |

## Scripts

| Script | What it does |
|---|---|
| `entity_bench.py bench` | MSPT over a fixed number of game ticks (`/tick sprint N`), PARSTATS of the parallel pass, optional `/tick query` percentiles at 20 TPS (`--steady S`), CPU frequency (`--freq`), background load (`--burners N`), tick-phase spans (`--spans`) |
| `entity_bench.py verify` | the same with `HORIZON_ENTITY_VERIFY=N`: every N-th parallel pass is also run sequentially on copies and compared (mobs, events, deaths, level random); counts mismatches |
| `entity_tables.py` | markdown tables from result files |
| `save_roundtrip.py` | parallel run → `save-all` → reload sequential and parallel; compares entity counts per species |
| `demo.py` | the parallel pass in a game window, numbers in the sidebar |
| `repro_fast_mover.py` (`--env K=V`) | the P1.1 audit case: a cow launched across the gap between two islands within one tick (verify every pass) |
| `burn.py` | a one-core busy loop |
| `perf_charts.py` | P0–P1.3 tables and SVG charts (`docs/parallelism/perf-data.csv`) |
| `p26_publish.py` | P2.4–P2.6 publication: verifies the pinned sources (`docs/parallelism/p26-manifest.json`), generates the CSV extracts and six SVG charts in a temporary directory, checks every CSV value against its source, and by default writes nothing. `--install` replaces the published files transactionally, `--refresh-manifest` re-pins, `--recover` rolls back an interrupted install, `--tables` prints the report statistics. Tests: `tests/test_p26_publish.py` |

Every run: fresh world (seed 12345, view/simulation distance 10, difficulty
hard), one idle bot, the game rules `spawn_mobs/spawn_monsters/spawn_patrols/
spawn_phantoms/spawn_wandering_traders/mob_griefing/advance_time false`, read
back and reported as `rules_ok`; `setup_errors` counts rejected commands.
Repetitions alternate the thread order (ABBA). Arena scenarios remove every
mob of another species before the window (generation spawns sea life and
herds).

Scenarios (`--scenarios`): `cows_500/1000/2000`, `cows_spread`,
`zombies_2000`, `mixed` (P0's piles at 0 64 0); `herds_8x250`,
`herds_16x125`, `zombies_8x250`, `mixedv_8x250`, `herds_1x1000_7x100` (P1
arenas); `species_land_8x250`, `species_water_8x250`, `player_arena`,
`effects_breed_8x250`, `riders_leash` (P1.1); `axolotls_8x100` (P1.2 level
random negative test); `pen_2000_wide/mid/tight` (one island of 2000 cows at
three crowdings, P2).

Results also carry `alarms_total`, `redone_total`, `not_redone_total`,
`mismatch_lines_total` (the P1.2 safety net over the whole run) and, for
`p2-spec`, `spec` (the SPECSTATS means: accepted share, recomputes, phase
costs, contact groups at the proven, actual and fixed steps).

## Examples

```sh
# P0 vs P1 in one binary, same conditions
py -3 tools/voxaryn/entity_bench.py bench --exe .local/baseline/horizon-client-p11-par.exe \
    --scenarios herds_8x250,cows_2000 --threads 0,6 --reps 3 --ticks 1000 --out .local/voxaryn-bench/ab.json
# correctness under several schedules
py -3 tools/voxaryn/entity_bench.py verify --exe .local/baseline/horizon-client-p11-par.exe \
    --scenarios player_arena --threads 2,6 --orders largest,shuffle:7 --verify-every 5 --ticks 300 \
    --out .local/voxaryn-bench/verify.json
py -3 tools/voxaryn/entity_tables.py .local/voxaryn-bench/ab.json
```

## Environment of the published numbers

i5-12400F (6 cores / 12 threads), 31.8 GB, Windows 11 26300, rustc/cargo
1.99.1, Python 3.14.3, tree based on `ec4780cd` plus the uncommitted P0/P1/P1.1
changes (docs/research/p1-stabilization.md lists them). Windows only so far
(`--freq` uses `typeperf`); the scripts use `.exe` only on Windows.
