"""P2.6 audit: the steady p95 of tools/voxaryn/entity_bench.py must be measured on the untouched scene.

The block count edits the world (`fill ... replace`), so it has to run after every steady measurement.
"""
import ast
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "tools" / "voxaryn" / "entity_bench.py"
spec = importlib.util.spec_from_file_location("entity_bench", PATH)
eb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eb)


class FakeServer:
    """Records console commands and answers the log patterns the bench waits for."""

    def __init__(self):
        self.commands = []
        self._log = ""

    def log_text(self):
        return self._log

    def console(self, line):
        self.commands.append(line)
        if line == "tick query":
            self._log += "Average time per tick: 10.0 ms\nP50: 9.0 ms P95: 12.0 ms P99: 15.0 ms\n"
        elif line.startswith("fill "):
            self._log += "Successfully filled 7 block(s)\n"

    def wait_log(self, pattern, timeout, after=0):
        import re
        m = re.compile(pattern).search(self._log[after:])
        if not m:
            raise RuntimeError("no match")
        return m


class SteadyDoesNotMutateTheScene(unittest.TestCase):
    def test_measure_steady_sends_no_world_edit(self):
        srv = FakeServer()
        with mock.patch.object(eb.time, "sleep"):
            out = eb.measure_steady(srv, 15)
        self.assertEqual(out["p95"], 12.0)
        self.assertTrue(srv.commands and all(c == "tick query" for c in srv.commands), srv.commands)

    def test_block_count_is_the_mutating_step(self):
        srv = FakeServer()
        res = {"steady": {}}
        eb.count_remaining_blocks(srv, eb.SCENARIOS["lambs_shortgrass_8x250"], res)
        self.assertEqual([c.split()[0] for c in srv.commands], ["fill", "fill"])
        self.assertEqual((res["arena0_dirt"], res["arena0_short_grass"]), (7, 7))
        self.assertTrue(res["arena0_count_after_steady"])

    def test_run_measures_steady_before_the_block_count(self):
        """In run(): no `fill` before the steady step, and count_remaining_blocks comes after it."""
        tree = ast.parse(PATH.read_text(encoding="utf-8"))
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
        calls = []
        for node in ast.walk(run):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                calls.append((node.lineno, node.col_offset, name))
        line = {n: min(c[0] for c in calls if c[2] == n) for n in ("measure_steady", "count_remaining_blocks")}
        self.assertLess(line["measure_steady"], line["count_remaining_blocks"])
        # no inline world edit left in run() itself
        self.assertNotIn("fill ", ast.get_source_segment(PATH.read_text(encoding="utf-8"), run))


if __name__ == "__main__":
    unittest.main()
