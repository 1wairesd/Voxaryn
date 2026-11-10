# Project origins and attribution

Voxaryn starts from the open-source **Horizon** project by Ludovic and other contributors:

https://github.com/ludovic111/horizon

The project provenance is **Horizon → unitycoder/mc-rust → 1wairesd/mc-rust → Voxaryn**. The intermediate [unitycoder/mc-rust](https://github.com/unitycoder/mc-rust) fork supplied the code obtained through the owner's `1wairesd/mc-rust` repository, which became Voxaryn. Horizon's historical ludovic111/horizon URL now redirects to [horizon-rust/horizon](https://github.com/horizon-rust/horizon); the intermediate fork is publicly listed as a fork of Horizon. The owner's historical mc-rust name is provenance, not a separately maintained upstream. The repository preserves the original Git commit history and authorship. The starting codebase is not an independent implementation written from scratch by the Voxaryn project: the game, the protocol, rendering, mob AI, the vanilla parity harnesses and their Java oracles, the launcher, the website and the CI/release tooling are Horizon's work.

Horizon is distributed under the MIT license. The original [LICENSE](LICENSE) file, its copyright notice and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) are retained. See Git history for prior authors and changes.

## What Voxaryn adds

- A research track on the server tick (profiling, benchmarks, design notes in `docs/research/`, mostly in Russian).
- **Entity Islands**, an experimental parallel mob pass, on experimental branches (published P1.3 and the prepared `experimental/entity-islands-p2.1`, not on `main`). It is off by default (cargo feature `parallel-entities` plus `HORIZON_ENTITY_THREADS`), covers only the mob pass and a list of verified species, and falls back to the sequential pass otherwise. Its status, safety checks, tests, limitations and single-machine measurements are in that branch's `PARALLELISM_PROGRESS.md`. Broader region-based multithreading remains a proposed direction, not existing functionality.
- Vanilla 26.3 parity fixes in the sequential mob pass that the experiment needed, and measurement tooling (`tools/voxaryn/`).

Much of this work was done with AI coding agents (including Kiro) under the project owner's direction, with external reviews by OpenAI Codex.

## Not affiliated

This project is not affiliated with or endorsed by the Horizon maintainers, Mojang or Microsoft. Horizon's Discord, website (playhorizon.dev), releases and issue tracker belong to Horizon; Voxaryn's issues are at https://github.com/1wairesd/Voxaryn/issues.
