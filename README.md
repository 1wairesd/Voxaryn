# Voxaryn

**Minecraft world simulation research in Rust.**

Voxaryn is an experimental project exploring scalable game ticks, parallel region scheduling, and deterministic cross-region events.

> **Status: research / prototype.** Multithreaded world simulation is a development goal, not a completed feature. The first experiment, a parallel mob pass behind an off-by-default feature, and its early single-machine measurements are described in [PARALLELISM_PROGRESS.md](PARALLELISM_PROGRESS.md).

## Goals

- Preserve a single-threaded reference mode for regression testing.
- Introduce measurable tick-phase profiling before optimizing.
- Evaluate worker-owned regions and controlled cross-region event delivery.
- Validate game behavior against the original implementation.
- Benchmark tick duration and scaling under realistic workloads.

## Development

The starting codebase is the Rust Minecraft implementation [Horizon](https://github.com/ludovic111/horizon). Existing Rust crate and binary names remain unchanged until a deliberate code-level migration; this repository rename does **not** yet rename the application.

See [PROJECT_ORIGINS.md](PROJECT_ORIGINS.md) for attribution and [LICENSE](LICENSE) for license terms.

## Compatibility

Voxaryn is unofficial, not affiliated with Mojang or Microsoft. A legally obtained copy of Minecraft may be needed to prepare and run the original game's assets.