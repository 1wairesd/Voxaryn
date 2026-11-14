"""Regression tests of tools/voxaryn/p26_publish.py (the P2.4-P2.6 publication generator).

Everything runs in temporary directories: the real .local benchmark data and the published files are only
read. The tests that regenerate the publication need the raw data under .local/voxaryn-bench/ (git-ignored)
and are skipped without it; the installer tests (E) need nothing.
"""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("p26_publish", ROOT / "tools" / "voxaryn" / "p26_publish.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

REAL_BENCH = ROOT / ".local" / "voxaryn-bench"
REAL_OUT = ROOT / "docs" / "parallelism"
HAVE_DATA = (REAL_OUT / "p26-manifest.json").is_file() and REAL_BENCH.is_dir() and all(
    (REAL_BENCH / s).is_file() for s in json.loads((REAL_OUT / "p26-manifest.json").read_text(encoding="utf-8"))["sources"])


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    saved = (p.BENCH, p.OUT, p.MANIFEST)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = p.main(list(args))
    finally:
        p.BENCH, p.OUT, p.MANIFEST = saved
    return rc, out.getvalue(), err.getvalue()


def snapshot(d):
    return {f.name: f.read_bytes() for f in sorted(Path(d).iterdir()) if f.is_file()}


def lf(b):
    return b.replace(b"\r\n", b"\n")


@unittest.skipUnless(HAVE_DATA, "raw benchmark data under .local/voxaryn-bench is not available")
class GeneratorWithData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="p26test-"))
        cls.bench = cls.root / "bench"
        manifest = json.loads((REAL_OUT / "p26-manifest.json").read_text(encoding="utf-8"))
        for s in manifest["sources"]:
            (cls.bench / s).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REAL_BENCH / s, cls.bench / s)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))
        self.bench = self.work / "bench"
        shutil.copytree(self.__class__.bench, self.bench)
        self.out = self.work / "out"
        self.out.mkdir()
        for n in p.OUTPUTS + ["p26-manifest.json"]:
            shutil.copyfile(REAL_OUT / n, self.out / n)

    def args(self, *extra):
        return ["--bench", str(self.bench), "--out", str(self.out), *extra]

    def test_a_missing_source_stops_before_any_output_changes(self):
        before = snapshot(self.out)
        (self.bench / "p26b" / "lambs_grass_8x250_r3_B.json").unlink()
        rc, _, err = run(*self.args("--install"))
        self.assertEqual(rc, 2)
        self.assertIn("missing source", err)
        self.assertEqual(snapshot(self.out), before)
        self.assertEqual(sorted(f.name for f in self.out.iterdir()), sorted(before))  # no journal, backup or stage left

    def test_a2_a_missing_timeline_log_and_a_missing_verify_file_are_caught(self):
        for rel in ("p26b/lambs_patch_8x250_r2_C.log", "p26bv/forced.json"):
            f = self.bench / rel
            data = f.read_bytes()
            f.unlink()
            rc, _, err = run(*self.args("--install"))
            self.assertEqual((rc, "missing source" in err or "not pinned" in err), (2, True), rel)
            f.write_bytes(data)

    def test_b_modified_source_is_a_sha256_error(self):
        before = snapshot(self.out)
        f = self.bench / "p26b" / "lambs_patch_8x250_r1_C.json"
        f.write_text(f.read_text(encoding="utf-8").replace('"ms_per_tick": 10.', '"ms_per_tick": 11.', 1), encoding="utf-8")
        rc, _, err = run(*self.args("--install"))
        self.assertEqual(rc, 2)
        self.assertIn("SHA-256 mismatch", err)
        self.assertEqual(snapshot(self.out), before)

    def test_b2_an_unpinned_extra_source_is_rejected(self):
        shutil.copyfile(self.bench / "p251perf" / "ab_herds_8x250_b1_1_A.json", self.bench / "p251perf" / "ab_herds_8x250_b9_1_A.json")
        rc, _, err = run(*self.args())
        self.assertEqual(rc, 2)
        self.assertIn("unexpected source", err)

    def test_b4_swapping_one_run_id_for_another_keeps_the_count_but_is_caught(self):
        for old, new in (("p251perf/ab_herds_8x250_b1_1_A.json", "p251perf/ab_herds_8x250_b9_1_A.json"),
                         ("p25abba/pen_2000_tight_t0_c2_3_A.json", "p25abba/pen_2000_tight_t0_c9_3_A.json"),
                         ("p24v/inject_push.json", "p24v/inject_push_2.json"),
                         ("p26b/lambs_patch_8x250_r4_C.json", "p26b/lambs_patch_8x250_r7_C.json")):
            (self.bench / old).rename(self.bench / new)
            before = snapshot(self.out)
            for flags in (["--install"], ["--refresh-manifest"]):
                rc, _, err = run(*self.args(*flags))
                self.assertEqual(rc, 2, (old, flags))
                self.assertIn("missing source" if flags == ["--install"] else "missing source", err, (old, flags))
                if flags == ["--install"]:
                    self.assertIn("unexpected source", err)
                self.assertEqual(snapshot(self.out), before)
            (self.bench / new).rename(self.bench / old)

    def test_b5_refresh_manifest_also_rejects_an_extra_run(self):
        shutil.copyfile(self.bench / "p251perf" / "ab_herds_8x250_b1_1_A.json", self.bench / "p251perf" / "ab_herds_8x250_b9_1_A.json")
        before = snapshot(self.out)
        rc, _, err = run(*self.args("--refresh-manifest"))
        self.assertEqual(rc, 2)
        self.assertIn("unexpected source", err)
        self.assertEqual(snapshot(self.out), before)

    def test_b6_the_fixed_id_lists_are_exactly_what_the_manifest_pins(self):
        m = json.loads((REAL_OUT / "p26-manifest.json").read_text(encoding="utf-8"))["sources"]
        want = {q for *_, q in p.p26_sources()} | set(p.TIMELINE_LOGS) | set(p.expected_ab_ids()) | set(p.EXPECTED_VERIFY_FILES)
        self.assertEqual(set(m), want)
        self.assertEqual(len(m), 288)
        self.assertEqual(len(p.expected_ab_ids()), 107)

    def test_b3_a_tampered_manifest_output_hash_is_rejected(self):
        m = json.loads((self.out / "p26-manifest.json").read_text(encoding="utf-8"))
        m["outputs"]["p26-data.csv"] = "0" * 64
        (self.out / "p26-manifest.json").write_text(json.dumps(m), encoding="utf-8")
        rc, _, err = run(*self.args("--install"))
        self.assertEqual(rc, 2)
        self.assertIn("differs from the pinned SHA-256", err)

    def test_c_an_ordinary_run_never_overwrites(self):
        # a published file that differs from the verified set is reported, not replaced
        (self.out / "p26-data.csv").write_text("sentinel\n", encoding="utf-8")
        before = snapshot(self.out)
        rc, out, _ = run(*self.args())
        self.assertEqual(rc, 1)
        self.assertIn("nothing was written", out)
        self.assertEqual(snapshot(self.out), before)
        # and an identical set is left alone, not even touched
        shutil.copyfile(REAL_OUT / "p26-data.csv", self.out / "p26-data.csv")
        mtimes = {f.name: f.stat().st_mtime_ns for f in self.out.iterdir()}
        rc, out, _ = run(*self.args())
        self.assertEqual(rc, 0)
        self.assertEqual({f.name: f.stat().st_mtime_ns for f in self.out.iterdir()}, mtimes)

    def test_d_regeneration_equals_the_published_results(self):
        for n in p.OUTPUTS:
            (self.out / n).write_text("old\n", encoding="utf-8")
        rc, out, err = run(*self.args("--install"))
        self.assertEqual(rc, 0, err)
        pinned = json.loads((self.out / "p26-manifest.json").read_text(encoding="utf-8"))["outputs"]
        for n in p.OUTPUTS:
            self.assertEqual(lf((self.out / n).read_bytes()), lf((REAL_OUT / n).read_bytes()), n)
            self.assertEqual(p.sha_lf(self.out / n), pinned[n], n)
        rows = {n: len(p.read_csv(self.out / n)) for n in p.OUTPUTS if n.endswith(".csv")}
        self.assertEqual(rows, p.EXPECTED_ROWS)
        self.assertEqual(rows, {"p26-data.csv": 138, "p24-p251-data.csv": 107, "p24-p26-verify.csv": 64, "p26-timeline.csv": 602})
        self.assertEqual(sorted(f.name for f in self.out.iterdir()), sorted(p.OUTPUTS + ["p26-manifest.json"]))

    def test_e3_a_failure_in_the_middle_of_a_real_install_restores_the_old_set(self):
        for n in p.OUTPUTS:
            (self.out / n).write_text("old " + n + "\n", encoding="utf-8")
        before = snapshot(self.out)
        calls = {"n": 0}
        real = p._replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 4:
                raise OSError("disk full (simulated)")
            real(src, dst)

        p._replace = flaky
        try:
            with self.assertRaises(OSError):
                run(*self.args("--install"))
        finally:
            p._replace = real
        self.assertEqual(calls["n"], 4)
        self.assertEqual(snapshot(self.out), before)
        self.assertEqual(sorted(f.name for f in self.out.iterdir()), sorted(before))


class InstallerAtomicity(unittest.TestCase):
    """E: replacing a set of files is all-or-nothing, also when the process dies (journal + --recover)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p26inst-"))
        self.out = self.root / "out"
        self.out.mkdir()
        self.new = self.root / "new"
        self.new.mkdir()
        self.names = ["a.csv", "b.csv", "c.svg", "d.svg"]
        for n in self.names[:3]:  # d.svg does not exist yet
            (self.out / n).write_text("old " + n, encoding="utf-8")
        for n in self.names:
            (self.new / n).write_text("new " + n, encoding="utf-8")
        self.files = {n: self.new / n for n in self.names}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def read(self):
        return {f.name: f.read_text(encoding="utf-8") for f in sorted(self.out.iterdir()) if f.is_file()}

    def listing(self):
        return sorted(f.name for f in self.out.iterdir())

    def test_success_replaces_everything_and_leaves_no_residue(self):
        p.install(self.files, self.out)
        self.assertEqual(self.read(), {n: "new " + n for n in self.names})
        self.assertEqual(self.listing(), sorted(self.names))  # no journal, backup or stage left

    def test_failure_after_two_replacements_restores_the_old_set(self):
        real, calls = p._replace, {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("simulated")
            real(src, dst)

        p._replace = flaky
        try:
            with self.assertRaises(OSError):
                p.install(self.files, self.out)
        finally:
            p._replace = real
        self.assertEqual(self.read(), {n: "old " + n for n in self.names[:3]})  # d.svg is gone again
        self.assertEqual(self.listing(), sorted(self.names[:3]))

    def test_a_dead_process_leaves_a_journal_that_blocks_runs_until_recovered(self):
        real_replace, real_rollback, calls = p._replace, p._rollback, {"n": 0}

        def dying(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("the process was killed here")
            real_replace(src, dst)

        p._replace, p._rollback = dying, lambda out_dir: None  # a killed process runs no cleanup
        try:
            with self.assertRaises(OSError):
                p.install(self.files, self.out)
        finally:
            p._replace, p._rollback = real_replace, real_rollback
        self.assertTrue((self.out / p.JOURNAL).exists())
        self.assertEqual(self.read()["a.csv"], "new a.csv")  # mixed set on disk, as after a crash
        with self.assertRaises(p.PublishError):
            p.install(self.files, self.out)  # refuses to build on a half-installed set
        rc, out, _ = run("--out", str(self.out), "--recover")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(), {n: "old " + n for n in self.names[:3]})
        self.assertEqual(self.listing(), sorted(self.names[:3]))

    # --- interruption at the end of a transaction -------------------------------------------------
    def patched(self, **kw):
        saved = {k: getattr(p, k) for k in kw}
        for k, v in kw.items():
            setattr(p, k, v)
        self.addCleanup(lambda: [setattr(p, k, v) for k, v in saved.items()])

    def test_kill_before_the_journal_is_deleted_keeps_the_backup_and_can_be_rolled_back(self):
        real = p._unlink_journal

        def killed(journal):
            raise RuntimeError("killed before the commit point")

        self.patched(_unlink_journal=killed)
        with self.assertRaises(RuntimeError):
            p.install(self.files, self.out)
        # the new files are in place but the transaction is not committed: journal and backup must both remain
        self.assertTrue((self.out / p.JOURNAL).exists() and (self.out / p.BACKUP / "a.csv").exists())
        self.assertEqual(self.read()["a.csv"], "new a.csv")
        self.patched(_unlink_journal=real)
        with self.assertRaises(p.PublishError):
            p.install(self.files, self.out)  # refuses while the journal exists
        rc, _, _ = run("--out", str(self.out), "--recover")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(), {n: "old " + n for n in self.names[:3]})
        self.assertEqual(self.listing(), sorted(self.names[:3]))
    def test_kill_between_journal_and_backup_deletion_after_install_leaves_a_harmless_orphan(self):
        real, calls = p._remove_backup, {"n": 0}

        def killed_second(backup):
            calls["n"] += 1
            if calls["n"] == 2:  # the commit-time call (the first one clears leftovers at the start)
                raise RuntimeError("killed after the journal was deleted")
            real(backup)

        self.patched(_remove_backup=killed_second)
        with self.assertRaises(RuntimeError):
            p.install(self.files, self.out)
        self.assertFalse((self.out / p.JOURNAL).exists())
        self.assertTrue((self.out / p.BACKUP).exists())  # orphan
        self.assertEqual(self.read(), {n: "new " + n for n in self.names})  # the new set is final
        self.patched(_remove_backup=real)
        self.assertFalse(p._restore(self.out))  # nothing to roll back; the orphan is cleaned
        self.assertEqual(self.listing(), sorted(self.names))
        self.assertEqual(self.read(), {n: "new " + n for n in self.names})
        # a new install after such a kill works as well
        (self.out / p.BACKUP).mkdir()
        (self.out / p.BACKUP / "stale").write_text("x")
        p.install(self.files, self.out)
        self.assertEqual(self.listing(), sorted(self.names))

    def test_kill_during_a_restore_before_the_journal_is_deleted_can_be_restored_again(self):
        real_replace, real_unlink, calls = p._replace, p._unlink_journal, {"n": 0}

        def failing(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("simulated")
            real_replace(src, dst)

        def killed(journal):
            raise RuntimeError("killed while finishing the restore")

        self.patched(_replace=failing, _unlink_journal=killed)
        with self.assertRaises(RuntimeError):
            p.install(self.files, self.out)
        # the old files are back, but the journal and the backup are still there
        self.assertTrue((self.out / p.JOURNAL).exists() and (self.out / p.BACKUP).exists())
        self.patched(_replace=real_replace, _unlink_journal=real_unlink)
        rc, out, _ = run("--out", str(self.out), "--recover")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(), {n: "old " + n for n in self.names[:3]})
        self.assertEqual(self.listing(), sorted(self.names[:3]))
        self.assertEqual(run("--out", str(self.out), "--recover")[1].strip(), "nothing to recover")

    def test_kill_after_the_restore_deleted_the_journal_leaves_only_an_orphan_backup(self):
        real_replace, real_remove, calls = p._replace, p._remove_backup, {"n": 0}

        def failing(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("simulated")
            real_replace(src, dst)

        def killed_after_journal(backup):
            if not (self.out / p.JOURNAL).exists() and calls["n"] >= 3:
                raise RuntimeError("killed after the journal was deleted")
            real_remove(backup)

        self.patched(_replace=failing, _remove_backup=killed_after_journal)
        with self.assertRaises(RuntimeError):
            p.install(self.files, self.out)
        self.assertFalse((self.out / p.JOURNAL).exists())
        self.assertEqual(self.read(), {n: "old " + n for n in self.names[:3]})  # the old set is final
        self.patched(_replace=real_replace, _remove_backup=real_remove)
        rc, out, _ = run("--out", str(self.out), "--recover")
        self.assertEqual((rc, out.strip()), (0, "nothing to recover"))
        self.assertEqual(self.listing(), sorted(self.names[:3]))
        p.install(self.files, self.out)  # and the next run works
        self.assertEqual(self.read(), {n: "new " + n for n in self.names})


if __name__ == "__main__":
    unittest.main()
