# Voxaryn

**Minecraft world simulation research in Rust.**

Voxaryn is an experimental project exploring scalable game ticks, parallel region scheduling, and deterministic cross-region events.

> **Status: research / prototype.** Multithreaded world simulation is a development goal, not a completed feature. The first experiment, a parallel mob pass behind an off-by-default feature (now with navigation-aware redo and an adaptive scheduling policy, P2.4–P2.6), and its single-machine measurements are described in [PARALLELISM_PROGRESS.md](PARALLELISM_PROGRESS.md); the latest report is [docs/research/p24-p26-navigation-and-adaptive-scheduling.md](docs/research/p24-p26-navigation-and-adaptive-scheduling.md). No general speed-up or Vanilla-parity claim is made.

## Goals

- Preserve a single-threaded reference mode for regression testing.
- Introduce measurable tick-phase profiling before optimizing.
- Evaluate worker-owned regions and controlled cross-region event delivery.
- Validate game behavior against the original implementation.
- Benchmark tick duration and scaling under realistic workloads.

## Development

The starting codebase is the Rust Minecraft implementation [Horizon](https://github.com/ludovic111/horizon) by Ludovic and other contributors. Voxaryn is a separate project, not Horizon and not maintained by Horizon's maintainers. Existing Rust crate and binary names (`horizon-client`, `horizon-prepare`, `HORIZON_*` variables…) remain unchanged until a deliberate code-level migration; this repository rename does **not** yet rename the application.

See [PROJECT_ORIGINS.md](PROJECT_ORIGINS.md) for attribution and [LICENSE](LICENSE) for license terms. How to work on the code: [CONTRIBUTING.md](CONTRIBUTING.md).

## Build from source

For developers. Requirements: Rust 1.90 or newer, Python 3.10 or newer, JDK 25, and on Linux the ALSA and udev development headers (`libasound2-dev libudev-dev` on Debian and Ubuntu).

The code contains no game files and never downloads them. It builds its resources from **your own copy** of Minecraft: Java Edition 26.3, verified against the checksums in `research-lock.json`:

1. **Client:** install Java Edition with the official launcher and play 26.3 once. The preparer reads `versions/26.3/`, `libraries/` and `assets/` from the launcher's game directory (`~/.minecraft` on Linux, `%APPDATA%\.minecraft` on Windows). If yours lives elsewhere, pass `--minecraft-dir` or set `HORIZON_MINECRAFT_DIR`.
2. **Server jar:** the 26.3 server jar, from the `downloads.server.url` in your launcher's `versions/26.3/26.3.json`. Pass it with `--server-jar` or set `HORIZON_SERVER_JAR`. It is only needed the first time.

```sh
git clone https://github.com/1wairesd/Voxaryn.git
cd Voxaryn
cargo run --release -p horizon-prepare -- --prepare --server-jar ~/Downloads/server.jar   # needs JDK 25
cargo run --release -p horizon-prepare -- sounds   # optional: sounds from your launcher install
cargo run --release -p horizon-client
```

Everything prepared lands in the gitignored `.local/`. It is derived from the game: never commit it or share it. The command-line options, controls and architecture are described in [the native client guide](docs/native-client.md); the rest of the developer documentation, inherited from Horizon, is indexed in [docs/README.md](docs/README.md).

## Feedback

Bugs, questions and proposals: [GitHub issues](https://github.com/1wairesd/Voxaryn/issues). Voxaryn has no Discord server or other community channel of its own; Horizon's Discord and issue tracker are for Horizon, so please don't report Voxaryn problems there.

## Inherited infrastructure

The repository still carries Horizon's GitHub workflows, release tooling, launcher, website and update/signing code. They were written for Horizon's own setup (its maintainer's machines, release keys, Discord bot and website) and are **not verified for Voxaryn**. Voxaryn has published no releases, no launcher builds and no website. Horizon's release, launcher, nightly-audit and run-parity workflows are disabled in this repository. See the notes at the top of [docs/release-flow.md](docs/release-flow.md) and [docs/security.md](docs/security.md).

## Compatibility

Voxaryn is unofficial, not affiliated with Mojang or Microsoft. A legally obtained copy of Minecraft may be needed to prepare and run the original game's assets.