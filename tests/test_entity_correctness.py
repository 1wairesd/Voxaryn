import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tools.voxaryn.entity_correctness import evaluate, read_result
from tools.voxaryn import entity_bench as b


def good():
    return dict(
        log_complete=True,
        completed=True,
        process_exit=0,
        setup_errors=0,
        rules_ok=True,
        mobs_start=20,
        mobs_end=20,
        mismatch_lines_total=0,
        not_redone_total=0,
        passes=20,
        parallel_passes=20,
        verified=20,
        mismatches=0,
        fallbacks=[],
        ticks_requested=20,
    )


class CorrectnessTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(evaluate(good())["status"], "PASS")

    def test_failures(self):
        for patch, status in [
            ({"mismatches": 1}, "FAIL"),
            ({"error": "launch failed"}, "INFRA ERROR"),
            ({"process_exit": 1}, "INFRA ERROR"),
            ({"error": "timeout"}, "INFRA ERROR"),
            ({"verified": None}, "INFRA ERROR"),
            ({"parallel_passes": 0}, "FAIL"),
            ({"completed": False}, "INFRA ERROR"),
            ({"fallbacks": ["species=20"]}, "FAIL"),
            ({"rules_ok": False}, "INFRA ERROR"),
            ({"mobs_end": None}, "INFRA ERROR"),
        ]:
            with self.subTest(patch=patch):
                self.assertEqual(evaluate(dict(good(), **patch))["status"], status)

    def test_fallback(self):
        r = dict(good(), parallel_passes=0, verified=0, fallbacks=["riders=20"])
        self.assertEqual(evaluate(r, "fallback")["status"], "PASS")
        self.assertEqual(evaluate(r)["status"], "FAIL")

    def test_injection(self):
        r = inj()
        self.assertEqual(evaluate(r, "injection")["status"], "PASS")
        r["injection_protected"] = 0
        self.assertEqual(evaluate(r, "injection")["status"], "FAIL")

    def test_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            for text in (None, "{", "{}", '{"complete":true,"results":[]}'):
                if text is not None:
                    p.write_text(text)
                self.assertEqual(read_result(p), "INFRA ERROR")

    def test_incomplete_log(self):
        self.assertEqual(b.summarize_parstats("PARSTATS passes 20, parallel"), {})
        r = good()
        for k in ("passes", "parallel_passes", "verified", "mismatches"):
            r.pop(k)
        self.assertEqual(evaluate(r)["status"], "INFRA ERROR")

    def test_launch_failure_exit_regression(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            exe = d / "fake.exe"
            exe.touch()
            argv = [
                "entity_bench.py",
                "verify",
                "--exe",
                str(exe),
                "--scenarios",
                "herds_8x250",
                "--threads",
                "6",
                "--out",
                str(d / "r.json"),
            ]
            with mock.patch.object(b, "BASE", d), mock.patch.object(b, "RES", d), mock.patch.object(
                b, "BOTS", exe
            ), mock.patch.object(b, "run", side_effect=RuntimeError("launch failed")), mock.patch.object(
                b.time, "sleep"
            ), mock.patch(
                "sys.argv", argv
            ):
                self.assertNotEqual(b.main() or 0, 0)
            data = json.loads((d / "r.json").read_text())
            self.assertIn("launch failed", data["results"][0]["error"])

    def test_server_crash_even_with_partial_success_log(self):
        srv = object.__new__(b.Server)
        srv.p = mock.Mock()
        srv.p.poll.return_value = 1
        srv.p.returncode = 1
        srv.log_text = lambda: "Sprint completed with 20 ticks per second, or 50 ms per tick"
        with self.assertRaisesRegex(RuntimeError, "server exited"):
            srv.wait_log(b.REPORT.pattern, 1)

    def test_server_timeout(self):
        srv = object.__new__(b.Server)
        srv.p = mock.Mock()
        srv.p.poll.return_value = None
        srv.log_text = lambda: "partial log"
        with mock.patch.object(b.time, "time", side_effect=[0, 0, 2]), mock.patch.object(b.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "within"):
                srv.wait_log(b.REPORT.pattern, 1)

    # N1: incomplete evidence must never be PASS (audit run_neg2 cases a, b, f).
    def test_truncated_log_is_not_pass(self):
        # 400 ticks were requested but PARSTATS only covers 20 passes.
        for contract, extra in (
            ("parallel", {}),
            ("fallback", dict(parallel_passes=0, verified=0, fallbacks=["links=20"])),
        ):
            with self.subTest(contract=contract):
                r = dict(good(), ticks_requested=400, **extra)
                self.assertEqual(evaluate(r, contract)["status"], "INFRA ERROR")

    def test_coverage_allows_the_unreported_tail_window(self):
        # PARSTATS is logged every 20 passes: up to 19 trailing passes may be unreported.
        ok = dict(good(), ticks_requested=39)
        self.assertEqual(evaluate(ok)["status"], "PASS")
        self.assertEqual(evaluate(dict(ok, ticks_requested=40))["status"], "INFRA ERROR")

    def test_missing_ticks_requested_is_infra(self):
        r = good()
        del r["ticks_requested"]
        self.assertEqual(evaluate(r)["status"], "INFRA ERROR")
        self.assertEqual(evaluate(dict(good(), ticks_requested=True))["status"], "INFRA ERROR")

    def test_unexplained_sequential_passes_fail_parallel_contract(self):
        # Empty fallbacks promise that every pass ran in parallel.
        self.assertEqual(evaluate(dict(good(), passes=20, parallel_passes=1, verified=1))["status"], "FAIL")

    def test_sequential_contract_rejects_parallel_passes(self):
        r = dict(good(), parallel_passes=3)
        self.assertEqual(evaluate(r, "sequential")["status"], "FAIL")
        seq = {
            k: v
            for k, v in good().items()
            if k not in ("passes", "parallel_passes", "verified", "mismatches", "fallbacks", "ticks_requested")
        }
        self.assertEqual(evaluate(seq, "sequential")["status"], "PASS")

    def test_cut_parstats_line_is_not_complete(self):
        full = (
            "Horizon: PARSTATS passes 20, parallel 20, threads 6, islands 8.0, largest 250.0, mobs 2000.0, work 1.000 ms, "
            "critical 1.000 ms, bound 1.00x, plan 0.100 ms, split 0.100 ms, run 1.000 ms, merge 0.100 ms, verify 0.500 ms, "
            "verified 20, mismatches 0, fallbacks [], blocking -, gate 0.000 ms, busy_max 1.000 ms, est 1.00x, allocs 0, "
            "alloc_kb 0.0, step 0.00, guard 0.000 ms, tripped 0, redone 0, block_changes 0, nav_redone 0 (shape 0), "
            "adaptive switches 0, probes 0"
        )
        self.assertTrue(b.parstats_log_complete(full))
        self.assertFalse(b.parstats_log_complete(full[: full.index(", gate ")]))
        self.assertFalse(b.parstats_log_complete(full + ", newfield 7"))


def inj():
    """A correct strict-injection row: one marker, one protected result, one trip and one redo, all in the window."""
    return dict(
        good(),
        injection_started=1,
        injection_results=1,
        injection_protected=1,
        injection_started_total=1,
        tripped=1,
        redone=1,
        nav_redone=0,
    )


class InjectionScopeTests(unittest.TestCase):
    # Blocker 1: markers (log) and counters (PARSTATS) must be counted over the same measured window.
    def test_one_correct_trip(self):
        self.assertEqual(evaluate(inj(), "injection")["status"], "PASS")

    def test_two_trips_for_one_injection_fail(self):
        # the audited false PASS: started=1, protected=1, tripped=2, redone=2
        self.assertEqual(evaluate(dict(inj(), tripped=2, redone=2), "injection")["status"], "FAIL")
        self.assertEqual(evaluate(dict(inj(), redone=2), "injection")["status"], "FAIL")

    def test_zero_injections_fail(self):
        r = dict(
            inj(),
            injection_started=0,
            injection_results=0,
            injection_protected=0,
            injection_started_total=0,
            tripped=0,
            redone=0,
        )
        self.assertEqual(evaluate(r, "injection")["status"], "FAIL")

    def test_two_injections_fail(self):
        r = dict(
            inj(),
            injection_started=2,
            injection_results=2,
            injection_protected=2,
            injection_started_total=2,
            tripped=2,
            redone=2,
        )
        self.assertEqual(evaluate(r, "injection")["status"], "FAIL")

    def test_injection_in_warmup_is_outside_the_window(self):
        r = dict(
            inj(),
            injection_started=0,
            injection_results=0,
            injection_protected=0,
            injection_started_total=1,
            tripped=0,
            redone=0,
        )
        self.assertEqual(evaluate(r, "injection")["status"], "FAIL")
        # a second injection outside the window next to a correct one is not "exactly one" either
        self.assertEqual(evaluate(dict(inj(), injection_started_total=2), "injection")["status"], "FAIL")

    def test_truncated_log_has_no_result_line(self):
        r = dict(inj(), injection_results=0, injection_protected=0)
        self.assertEqual(evaluate(r, "injection")["status"], "INFRA ERROR")

    def test_unprotected_result_is_fail(self):
        self.assertEqual(evaluate(dict(inj(), injection_protected=0), "injection")["status"], "FAIL")

    def test_markers_and_counters_disagree(self):
        # protected result in the window but no trip counted: scope or tail unreported, not provable
        self.assertEqual(evaluate(dict(inj(), tripped=0, redone=0), "injection")["status"], "INFRA ERROR")

    def test_undetermined_scope_is_never_pass(self):
        for key in ("injection_started_total", "injection_results", "nav_redone", "tripped"):
            r = inj()
            del r[key]
            with self.subTest(missing=key):
                self.assertEqual(evaluate(r, "injection")["status"], "INFRA ERROR")

    def test_navigation_redo_is_not_a_second_trip(self):
        # redone also counts block-change (navigation) redos: those are expected sequential redos
        self.assertEqual(evaluate(dict(inj(), redone=3, nav_redone=2), "injection")["status"], "PASS")

    def test_ordinary_contracts_ignore_injection_fields(self):
        self.assertEqual(evaluate(good(), "parallel")["status"], "PASS")

    def test_injection_counts_split_warmup_from_window(self):
        start = "Horizon: TEST fault injection on this pass: Inject { kind: Attack }\n"
        prot = (
            "Horizon: TEST fault injection result: tripped true, redone true, target (health, target, velocity) "
            "None -> None, unapplied events 0, MobHit events 1\n"
        )
        warm = start + prot
        win = start + prot
        c = b.injection_counts(win, warm + win)
        self.assertEqual(
            (c["injection_started"], c["injection_results"], c["injection_protected"], c["injection_started_total"]),
            (1, 1, 1, 2),
        )
        c = b.injection_counts("", warm)
        self.assertEqual((c["injection_started"], c["injection_started_total"]), (0, 1))
        c = b.injection_counts(start, start)
        self.assertEqual((c["injection_started"], c["injection_results"]), (1, 0))


class ExeCopyTests(unittest.TestCase):
    # Blocker 2: every run in a series executes one verified private copy of the exe.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.src = self.d / "server.exe"
        self.src.write_bytes(b"BUILD-A" * 1000)
        self.sha_a = hashlib.sha256(self.src.read_bytes()).hexdigest()
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, side=None, extra=(), reps=2, git=True):
        out = self.d / "r.json"

        def fake_run(exe, scenario, args, threads, order=None, verify=False):
            self.calls.append((Path(exe), Path(exe).read_bytes()))
            if getattr(self, "boom", None):
                raise self.boom
            if side:
                side(len(self.calls), Path(exe))
            return dict(good(), scenario=scenario, threads=threads, order=order, ticks_requested=args.ticks)

        argv = [
            "entity_bench.py",
            "verify",
            "--exe",
            str(self.src),
            "--scenarios",
            "herds_8x250",
            "--threads",
            "6",
            "--expect",
            "parallel",
            "--ticks",
            "20",
            "--reps",
            str(reps),
            "--out",
            str(out),
            *extra,
        ]
        patches = [
            mock.patch.object(b, "BASE", self.d / "bench"),
            mock.patch.object(b, "RES", self.d),
            mock.patch.object(b, "BOTS", self.src),
            mock.patch.object(b, "run", side_effect=fake_run),
            mock.patch.object(b.time, "sleep"),
            mock.patch("sys.argv", argv),
        ]
        if not git:
            patches.append(mock.patch.object(b.subprocess, "run", side_effect=FileNotFoundError("git")))
        for p in patches:
            p.start()
        try:
            code = b.main()
        finally:
            for p in reversed(patches):
                p.stop()
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_two_unchanged_runs_use_a_private_copy(self):
        code, data = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 2)
        for path, content in self.calls:
            self.assertNotEqual(path, self.src.resolve())
            self.assertEqual(content, self.src.read_bytes())
        self.assertEqual(data["exe_sha256"], self.sha_a)
        for r in data["results"]:
            self.assertEqual(r["exe_sha256"], self.sha_a)
            self.assertEqual(r["exe_path"], str(self.calls[0][0]))
        self.assertFalse(self.calls[0][0].exists(), "temporary copy must be removed")
        self.assertTrue((self.d / "r.json").exists())
        self.assertTrue(self.src.exists())

    def test_source_swapped_between_runs_still_runs_the_old_build(self):
        code, data = self.main(side=lambda n, p: self.src.write_bytes(b"BUILD-B") if n == 1 else None)
        self.assertEqual(code, 0)
        self.assertEqual([c for _, c in self.calls], [b"BUILD-A" * 1000] * 2)
        self.assertEqual({r["exe_sha256"] for r in data["results"]}, {self.sha_a})
        self.assertTrue(data["exe_source_changed"])

    def test_modified_copy_is_infra_error_and_stops_the_series(self):
        code, data = self.main(side=lambda n, p: p.write_bytes(b"tampered") if n == 1 else None)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(data["results"][0]["evaluation"]["status"], "INFRA ERROR")
        self.assertIn("changed", data["results"][0]["error"])

    def test_copy_modified_before_the_second_run(self):
        # modified after run 1 returned but before run 2 starts: detected before run 2 (hook on the sleep between runs)
        def tamper(n, p):
            if n == 1:
                self._tamper_next = p

        with mock.patch.object(b, "sha256_file", side_effect=self._flip_after_first(b.sha256_file)):
            code, data = self.main(side=tamper)
        self.assertEqual(code, 2)

    @staticmethod
    def _flip_after_first(real):
        state = {"n": 0}

        def f(path):
            state["n"] += 1
            # 1: source, 2: copy after copying, 3: before run 1, 4: after run 1, 5: before run 2 -> corrupt
            return "0" * 64 if state["n"] == 5 else real(path)

        return f

    def test_pin_mismatch_refuses_before_any_run(self):
        with self.assertRaises(SystemExit) as cm:
            self.main(extra=["--exe-sha256", "00" * 32])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.d / "bench").glob("exe_*")), [])

    def test_pin_match_runs(self):
        code, _ = self.main(extra=["--exe-sha256", self.sha_a.upper()])
        self.assertEqual(code, 0)

    def test_copy_error_is_infra_error(self):
        with mock.patch.object(b.shutil, "copy2", side_effect=OSError("disk full")):
            code, data = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("disk full", data["results"][0]["error"])

    def test_unreadable_exe_is_infra_error(self):
        with mock.patch.object(b, "sha256_file", side_effect=PermissionError("locked")):
            code, data = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_crash_is_infra_error_and_copy_is_removed(self):
        self.boom = RuntimeError("server exited: 3")
        code, data = self.main()
        self.assertEqual(code, 2)
        self.assertIn("server exited", data["results"][0]["error"])
        self.assertEqual(list((self.d / "bench").glob("exe_*")), [])

    def test_git_failure_is_unknown_not_clean(self):
        code, data = self.main(git=False)
        self.assertEqual(data["source_dirty"], "UNKNOWN")
        self.assertEqual(data["source_sha"], "UNKNOWN")
