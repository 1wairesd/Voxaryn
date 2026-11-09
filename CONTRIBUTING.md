# Contributing to Voxaryn

Thanks for helping. Voxaryn is a research project on Minecraft world simulation in Rust, built on
[Horizon](https://github.com/ludovic111/horizon) (Minecraft: Java Edition 26.3 rewritten in Rust; see
[PROJECT_ORIGINS.md](PROJECT_ORIGINS.md)). The rule that shapes everything it inherited still holds: **it plays
exactly like vanilla**, and every experiment (parallel ticks, new scheduling) must give the same results as the
sequential, vanilla-matching path. Bugs, questions and proposals go to
[GitHub issues](https://github.com/1wairesd/Voxaryn/issues). Voxaryn has no Discord of its own; Horizon's Discord
and issue tracker are for Horizon.

This repository holds the game, plus the launcher, the website, tools, tests and docs inherited from Horizon.
Not all of the inherited infrastructure is set up for Voxaryn (see [Inherited processes](#inherited-processes)).

## Build it

You need Rust (stable), Python 3.10+, JDK 25, and on Linux the ALSA and udev headers (`libasound2-dev libudev-dev`).
The code never downloads the game: it prepares its resources from your own copy of Minecraft 26.3, as described in
[Build from source](README.md#build-from-source).

```sh
cargo run --release -p horizon-prepare -- --prepare --server-jar ~/Downloads/server.jar
cargo run --release -p horizon-client
```

The launcher is its own Cargo workspace in `launcher/` (`cd launcher && cargo run`; on NixOS, `nix-shell` first).

Crate, binary and variable names are still Horizon's (`horizon-client`, `HORIZON_*`). Don't rename them in a
change that does something else.

## Vanilla first

Wherever vanilla has a feature, vanilla is the specification, down to the order of random calls. "Parity" here
means the game gives the same results as the pinned 26.3 client and server for the same inputs, checked by a
**harness**: a `tools/test_*_parity.py` script that runs a small Java oracle against the real game and a Rust
probe against this code, and compares their outputs. There are over 300 of them (326 scripts today), written by Horizon.

- **Read the vanilla code before writing ours.** `tools/rea_vanilla.py` queries the pinned game bytecode:
  `python3 tools/rea_vanilla.py method net.minecraft.world.level.block.DispenserBlock dispenseFrom`.
  Say in your pull request which vanilla classes and methods you followed.
- **Gameplay changes come with a harness**, new or extended, so the next change can't silently break them.
  [The parity workflow](docs/parity-workflow.md) shows how harnesses run; copy a nearby one.
- **Features vanilla doesn't have** (the launcher, the new menus, inherited from Horizon) follow
  [the design system](docs/design-system.md) and are off by default wherever they would change the vanilla look.
- **Everything players run is Rust.** Python and Java are fine for dev tools and harness drivers in `tools/`, never
  for player-facing code.

Never commit game files, decompiled source or prepared resources (`.local/`): `tools/check_public_tree.py` rejects
them. `git config core.hooksPath .githooks` runs it before every commit and push; the hooks call `python3`, so on
Windows run `py -3 tools/check_public_tree.py --history` yourself before pushing.

## Experiments and research

- **Off by default.** Experimental features (such as the parallel mob pass, `parallel-entities`) live behind
  cargo features that are off by default, with the sequential path as the fallback and the reference.
  Test-only knobs (deliberately unsafe settings, fault injection) belong in an explicit test feature
  (`parallel-entities-unsafe-test`), never in a build anyone plays on.
- **Compare against the sequential path.** A parallel result must equal the sequential pass bit for bit
  (`HORIZON_ENTITY_VERIFY=1`), and the sequential pass must keep matching vanilla (the harnesses).
- **Measure, don't assume.** Performance claims need the scenario, the machine, the build, the method
  (`/tick sprint N` windows, repeats, run-to-run spread) and the raw results. Say what was not measured.
  Unverified causes are hypotheses.
- Phase reports go to `docs/research/`; benchmark and test tools to `tools/voxaryn/`.

## Before asking for review

Run the self-check and paste the block it prints into your pull request:

```sh
python3 tools/pr_ready.py --base origin/main
```

It looks at what your branch changes and runs what matters: rustfmt, clippy with warnings denied, the tests of the
crates you touched (and the launcher's), generated files, `Cargo.lock`, the Python tests, and the vanilla harnesses
your change can affect. Without the vanilla data on your machine it lists those harnesses instead. `--quick` skips
the harnesses. It exits non-zero when something fails. On Windows, `tools/parity.py` (which it uses for harnesses)
does not run (it needs `fcntl`): run the affected `tools/test_<name>_parity.py` scripts one by one and say so.

For the game workspace, Clippy checks all targets of the touched crates and their transitive workspace dependents,
including examples. The dependency scope includes optional, platform, build and development dependencies.
Tests run library, binary, integration and doc tests without building unrelated example executables. Shared Cargo
configuration and toolchain changes select the whole workspace.

## Pull requests

- **Small.** One change per pull request, under about 500 lines of code where you can; split bigger work into
  pieces that each make sense alone.
- **Fill in the template**: what and why, the vanilla reference, the pr_ready block (or what you ran instead),
  measurements for performance work, screenshots for anything you can see.
- **Say what you didn't run.** Harnesses, platforms and tests you could not run are listed in the pull request.
- **AI agents** may write code, but everything an agent posts says whose agent it is and which tool, and agent
  help is named in the commit message (see [AGENTS.md](AGENTS.md)).

## Inherited processes

These come from Horizon and are **not verified for Voxaryn**. Don't rely on them, and don't describe them as
Voxaryn's:

- **Releases.** Horizon releases every merge to main automatically ([release flow](docs/release-flow.md)), signs
  them with its own key and announces them through its Discord bot. Voxaryn has published no release, holds no
  signing key and has no bot. The release workflows are still in `.github/workflows/`.
- **The `parity` CI job** runs the harnesses on Horizon's maintainer machine (a self-hosted Mac runner holding the
  game files). Voxaryn has no such runner, so jobs that need it don't run here; run the harnesses locally.
- **Labels** (`feature`, `skip-release`, `release-now`, `run-parity`, `release-blocker`) drive Horizon's release
  train and parity runs. They have no verified effect in Voxaryn.
- **Security** ([security.md](docs/security.md)) describes Horizon's agents, keys and machines.

## Talk to us

[GitHub issues](https://github.com/1wairesd/Voxaryn/issues) for bugs, questions and ideas. If you want to take
something on, say so in its issue so nobody else starts the same work. Horizon itself is at
https://github.com/ludovic111/horizon: report problems that also exist in Horizon there, and credit it when you
port its fixes.
