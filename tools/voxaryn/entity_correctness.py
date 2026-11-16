"""Fail-closed correctness evaluation. No performance thresholds."""

import json
import math

# shortcut: PARSTATS is logged every 20 passes, so up to 19 trailing passes may legitimately be unreported;
# a tail that long can hide a truncated log. Upgrade: have the server log a final PARSTATS at sprint end.
PARSTATS_WINDOW = 20


def evaluate(row, expectation="parallel", verify=True):
    checks = []

    def check(name, ok, infra=False):
        checks.append({"invariant": name, "passed": bool(ok), "kind": "INFRA ERROR" if infra else "FAIL"})

    if not isinstance(row, dict):
        return {"status": "INFRA ERROR", "checks": [], "reason": "result is not an object"}
    check("process completed", not row.get("error") and row.get("completed") is True, True)
    check("log parsed completely", row.get("log_complete") is True, True)
    check("clean shutdown", row.get("process_exit") == 0, True)
    for key in ("setup_errors", "mobs_start", "mobs_end", "mismatch_lines_total", "not_redone_total"):
        check("metric " + key, type(row.get(key)) in (int, float) and math.isfinite(row[key]) and row[key] >= 0, True)
    check("rules read back", row.get("rules_ok") is True, True)
    check("setup accepted", row.get("setup_errors") == 0, True)
    check("no mismatch", row.get("mismatch_lines_total") == 0 and row.get("mismatches", 0) == 0)
    check("no unprotected redo", row.get("not_redone_total") == 0)
    check("population observed", isinstance(row.get("mobs_start"), int) and row["mobs_start"] > 0, True)
    if expectation != "sequential":
        for key in ("passes", "parallel_passes", "verified", "mismatches"):
            check("metric " + key, type(row.get(key)) is int and row[key] >= 0, True)
        ticks = row.get("ticks_requested")
        check("metric ticks_requested", type(ticks) is int and ticks >= PARSTATS_WINDOW, True)
        check(
            "passes cover the requested ticks",
            type(ticks) is int and type(row.get("passes")) is int and row["passes"] >= ticks - (PARSTATS_WINDOW - 1),
            True,
        )
        if expectation == "fallback":
            check(
                "expected fallback",
                row.get("parallel_passes") == 0 and bool(row.get("fallbacks")) and row.get("passes", 0) > 0,
            )
        else:
            check("parallel executed", row.get("parallel_passes", 0) > 0)
            if verify:
                check(
                    "every reported parallel pass verified",
                    row.get("verified", 0) == row.get("parallel_passes") and row.get("verified", 0) > 0,
                )
            check("no unexpected fallback", expectation == "injection" or not row.get("fallbacks"))
            if expectation == "parallel":
                # empty fallbacks promise a reason for every sequential pass: none means every pass ran parallel
                check("every pass ran in parallel", row.get("parallel_passes") == row.get("passes"))
    else:
        check("no parallel passes", row.get("parallel_passes", 0) == 0)
    if expectation == "injection":
        # Strict contract: exactly one injection, counted over the measured window (the scope of the PARSTATS counters);
        # injections in warmup or setup are counted apart. Supported for a single level only (see entity_bench.main).
        keys = (
            "injection_started",
            "injection_results",
            "injection_protected",
            "injection_started_total",
            "tripped",
            "redone",
            "nav_redone",
        )
        known = all(type(row.get(k)) is int and row[k] >= 0 for k in keys)
        check("injection markers and counters of the window are present", known, True)
        if known:
            n = row["injection_started"]
            check("exactly one injection in the measured window", n == 1)
            check("no injection outside the measured window", row["injection_started_total"] == n)
            check("every injection logged its result", row["injection_results"] == n, True)
            check("injection protected", row["injection_protected"] == 1)
            # `redone` also counts navigation (block-change) redos: those are expected sequential redos, not trips.
            trips, trip_redos = row["tripped"], row["redone"] - row["nav_redone"]
            if row["injection_protected"] == 1:
                # shortcut: a trip in the last <20 unreported passes is not visible, so this is INFRA, not FAIL
                check("the protected injection is seen in PARSTATS", trips >= 1 and trip_redos >= 1, True)
            check("exactly one trip and one redo for it", trips <= 1 and trip_redos <= 1)
    status = (
        "INFRA ERROR"
        if any(not c["passed"] and c["kind"] == "INFRA ERROR" for c in checks)
        else "FAIL" if any(not c["passed"] for c in checks) else "PASS"
    )
    return {"status": status, "checks": checks}


def read_result(path):
    """Reject absent, damaged, empty or unfinished evidence."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("complete") is not True or not data.get("results"):
            raise ValueError("incomplete result")
        if any(not isinstance(r, dict) or r.get("evaluation", {}).get("status") != "PASS" for r in data["results"]):
            return "FAIL"
        return "PASS" if data.get("status") == "PASS" else "INFRA ERROR"
    except (OSError, ValueError, TypeError):
        return "INFRA ERROR"
