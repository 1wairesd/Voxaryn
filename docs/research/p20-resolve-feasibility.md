# P2.0 — collision resolve preparation feasibility

Research result, not an enabled optimisation. Baseline: P1.3.2 plus P1.4.2. The temporary probe recorded the exact resolve request made during a shadow mob tick, then compared it with the request in the real sequential pass. It did not apply speculative movement to the world.

## Question and method

A reusable collision result needs identical bounds, requested movement, step height, on-ground state and relevant collision context. Matching only an entity ID or position is insufficient. Earlier mobs can change velocity through pushing before the later mob travels.

Kiro's Windows 11 / i5-12400F measurements used fresh dedicated-server worlds, seed 12345, an idle bot and the entity benchmark rules. The tight pen disables max_entity_cramming to retain its population. Files are under the ignored .local/voxaryn-bench directory. Public per-run extracts, source filenames and SHA-256 hashes are in [the measurement CSV](../parallelism/p14-p21-data.csv).

- p20_probe_herds.json: two shadow-probe runs; this is instrumented execution, not production MSPT.
- p20_probe2_pen.json: two runs with the corrected request classification.
- p20_time_herds.json and p20_time_pen.json: sequential resolve timing; separate from shadow-probe overhead.

## Observations and limitations

In the corrected pen probe, only about 35% of requests matched exactly; the remaining requests had changed velocity. See the per-run resolve records in the local JSON for the full classifications. The two resolve totals were 6.405 and 6.765 ms per sequential pass. Predicting movement before preceding mobs push the subject therefore cannot simply reuse every result.

The shadow-probe full ticks were 177.42 and 177.69 ms in that pen. They include the speculative pass, request recording, validation and real sequential execution. They are diagnostic overhead, not evidence of a useful P2 implementation. The uninstrumented sequential pen controls were 19.19, 19.17 and 19.25 ms; these are different runs, not a controlled estimate of an individual probe's cost.

The sequential timing-only runs measured resolve at 4.549 / 4.561 ms for herds and 5.971 / 5.968 ms for the pen. Those numbers motivated examining allocations inside collision shape translation rather than building a new scheduler.

## Publication decision

Do not integrate parallel resolve preparation on this evidence. P2.0 supplied a diagnostic result, not an accepted runtime path. All new resolve-request recording, gather replay, scan counters, timers, their modules and the scan-probe Cargo feature have been removed from the publication tree. Their original files and patch are preserved in the external local publication backup. The older, published p2-speculate feature remains unchanged and off by default; this publication does not run or extend it.

Correctness of the synthetic request classification is not a proof of Vanilla parity. No claim of speeding up a single island through parallel execution is made.
