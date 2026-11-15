# Known issues: world recovery and world upgrade (datafix)

Status: **open, development paused** (2026-11-15). Nothing on this page is fixed in the safe batch 1.
The safe batch keeps the world loading and the world upgrade exactly as they were in Voxaryn
(`crates/horizon-client/src/game/world_loading.rs` and `crates/horizon-world/src/datafix/files.rs` are
unchanged; `crates/horizon-client/src/save.rs` only gets Horizon's `a96dccf691`: a failed move of the
current file to `_old` in `write_dat` is now an error instead of being ignored).

Upstream Horizon (main `8f06e6d10a`, 2026-11-15) has the same problems in the upgrader; see the last section.

Each item says how it is known:

- **Reproduced**: shown on a real run or by a test that fails on this code.
- **Code reading**: follows from the code, not run.
- **Hypothesis**: a plausible scenario, not shown.

## 1. A world with an unreadable `level.dat` opens as a new world

- **Reproduced** (live client, throwaway world, Voxaryn `c23241f501`): `level.dat` corrupted while a good
  `level.dat_old` exists. The world opened with seed 0, survival and an empty inventory instead of seed 1,
  creative and the saved items.
- Cause (code reading): `Level::read` reads only `level.dat`; `None` is taken as "new world".
- **Hypothesis**: the next save calls `write_dat`, which moves the unreadable `level.dat` over
  `level.dat_old`, so the good copy is lost too. The file contents after the run were not compared.
- **Hypothesis**: with both files unreadable the result is the same (new world under the old name).

## 2. A failed upgrade is taken for finished on the next start

- **Reproduced** (test that fails on this code): `upgrade_world` writes `level.dat` with the current
  DataVersion before it converts chunks, players, saved data and JSON. If that later step fails, the next
  start sees the current DataVersion and skips the rest. Example: DataVersion 4898, a bad
  `players/stats/*.json`; the second start returns success with the data half converted.

## 3. A folder that cannot be listed is taken for an empty one

- **Reproduced** (Codex, real Windows error `WinError 32` on `players/data`; and a test that fails on this
  code): `subdirectories`, `files_with_suffix`, `regex_move` and the saved data walk turn `read_dir` errors into
  an empty list and drop entry errors (`unwrap_or_default`, `filter_map(Result::ok)`). The upgrade then
  "succeeds", stamps the current DataVersion and leaves the player files at the old version for good.
- A missing folder and a folder that cannot be read are not told apart.

## 4. The upgrader overwrites files in place

- **Reproduced** (tests that fail on this code): `datafix::files::write_dat` and the JSON writer call
  `fs::write` on the file itself: no temporary file, no `sync_all`, no rename.
- **Hypothesis**: a crash or a power loss during that write leaves a torn file, and for `level.dat` there
  may be no other good copy (see 5). Not shown with a real kill or power loss.

## 5. The upgrader deletes `level.dat_old` and `session.lock`

- **Code reading**: `fix_world_files` deletes both after the file fixes (vanilla deletes `level.dat_old`
  as well).
- **Code reading**: `world_loading.rs` runs `upgrade_older_world` and `migrate_legacy` **before**
  `SessionLock::acquire`. The upgrade runs without the world lock.
- **Hypothesis** (Unix): deleting a locked `session.lock` lets a second process lock a new file at the same
  path, so two instances can own the world. Not run on Unix.

## 6. File fixes are not transactional

- **Code reading**: the file fixes 1493, 4772 and 4773 move and delete files and folders one by one
  (`move_file`, `delete`). This affects worlds saved before 26.1 (DataVersion < 4772).
- **Hypothesis**: if one of them stops half way, the world is left in a mixed layout, and a retry can stop
  on "target already exists".

## 7. Writes without `sync`

- **Code reading**: the region rewrite in the upgrader (`upgrade_regions`: a `.datafix` staging file, then
  a rename) and the game's normal `save.rs::write_dat` do not call `sync_all`.
- **Hypothesis**: after a power loss these files may be empty or torn. Not tested.

## Experimental fixes (not merged)

These were built and tested, then set aside. They are kept for a later task "World Recovery & Datafix
Safety":

| Experiment | Where | What it does |
|---|---|---|
| Horizon `8f26771053` + recovery rework | `.crew-reports/upstream-sync/fix2/leveldat.patch` | restores `level.dat_old` before the upgrade, under the lock; refuses to open a world with no readable level data |
| Upgrade staging and lock continuity | `.crew-reports/upstream-sync/port1/FINAL1_DELTA.diff` | current DataVersion written last (4899 as a progress marker); `level.dat_old` and `session.lock` no longer deleted; one lock from start to close |
| Read errors and atomic writes | `.crew-reports/upstream-sync/recov1/RECOV1_DELTA.diff`, worktree `Voxaryn-wt-recov` | listing errors stop the upgrade; NBT and JSON written through `_new`, `sync_all` and a rename; recovery copies `level.dat_old` instead of moving it |

Reports: `FIX2_LEVELDAT_REPORT.md`, `FINAL1_REPORT.md`, `RECOVERY_UPSTREAM_REPORT.md`, Codex audits in
`.crew-reports/upstream-sync/codex-audits/`.

Remaining gaps of the experiments, even with all three applied: issues 6 and 7, power loss in general,
Unix and two real processes not run, an entry (`DirEntry`) error not testable on demand.

## Possible future solutions

1. Error-returning folder listing in the upgrader (a missing folder is empty; any other error stops the
   upgrade). Experiment above.
2. Per-file publication for the upgrader: temporary file, `sync_all`, rename, folder sync on Unix. The order
   Horizon uses in `save.rs::write_durable` (`a66f0fc57d`); `horizon-world` cannot call `horizon-client`,
   so it needs its own helper.
3. Current DataVersion written only after every step; a progress marker after the file fixes (Horizon
   `4269899778` does the first part, without the marker).
4. The world lock taken before recovery and upgrade and held without a gap; never delete `session.lock`.
5. Keep `level.dat_old` until the world has been saved whole again (copy, do not move, on recovery).
6. A progress marker after each file fix and an idempotent `move_file` (source gone and target present
   means already done).
7. `sync_all` in `RegionWriter::finish` and before renames; decide whether the normal save should be durable
   (cost).
8. Recovery as in Horizon's UI: list worlds with unreadable level data and ask the player
   (`9bdb5ec7fd`); the dedicated server falls back to `level.dat_old` (`4e8bdad871`).
9. Tests on Linux (two processes, unlinked lock file) and a real kill test, not only fault injection.

## Upstream Horizon

Checked on Horizon main `8f06e6d10a` (2026-11-15):

- Issues 2 to 5 are still in `crates/horizon-world/src/datafix/files.rs`.
- Horizon fixed only the stamping order (`4269899778`), and has the durable write only in client code
  (`a66f0fc57d`).
- Its recovery is a UI choice in the world list (`9bdb5ec7fd`). The dedicated server falls back to
  `level.dat_old` automatically (`4e8bdad871`).

Horizon has no ready, safe recovery design to port.
