"""Offline tests: no network, no credentials. Run: python3 -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pace_budget", ROOT / "pace_budget.py")
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)


def run_cli(*argv):
    out = io.StringIO()
    old = sys.argv
    sys.argv = ["pace_budget.py", *argv]
    try:
        with contextlib.redirect_stdout(out):
            pb.main()
    finally:
        sys.argv = old
    return out.getvalue()


class AllocateTests(unittest.TestCase):
    def ents(self, *cur):
        return [{"id": str(i), "name": f"e{i}", "current": c} for i, c in enumerate(cur)]

    def test_hits_target_proportionally(self):
        e = self.ents(50, 50)
        pb.allocate(e, 120, floor=5, ceiling=1000, max_step=0.5)
        self.assertEqual(sum(x["new"] for x in e), 120)
        self.assertEqual(e[0]["new"], e[1]["new"])

    def test_step_cap_limits_move(self):
        e = self.ents(100)
        pb.allocate(e, 1000, floor=5, ceiling=1000, max_step=0.3)
        self.assertEqual(e[0]["new"], 130)

    def test_hold_is_pinned(self):
        e = self.ents(50, 50)
        e[0]["hold"] = 20
        pb.allocate(e, 100, floor=5, ceiling=1000, max_step=1.0)
        self.assertEqual(e[0]["new"], 20)
        self.assertEqual(e[1]["new"], 80)

    def test_below_average_never_raised(self):
        e = self.ents(50, 50)
        e[0]["perf_mult"] = 0.5
        pb.allocate(e, 200, floor=5, ceiling=1000, max_step=1.0)
        self.assertLessEqual(e[0]["new"], 50)

    def test_floor_wins_when_target_too_small(self):
        e = self.ents(50, 50)
        pb.allocate(e, 4, floor=10, ceiling=1000, max_step=0.3)
        self.assertTrue(all(x["new"] >= 10 for x in e))


class PerformanceTests(unittest.TestCase):
    def test_smoothing_and_bounds(self):
        ents = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        perf = {"a": {"spend": 1000, "leads": 50}, "b": {"spend": 1000, "leads": 5},
                "c": {"spend": 0, "leads": 0}}
        cpl = pb.apply_performance(ents, perf, {"prior_spend": 300, "mult_bounds": [0.5, 2.0]})
        self.assertAlmostEqual(cpl, 2000 / 55)
        self.assertGreater(ents[0]["perf_mult"], 1.0)
        self.assertLess(ents[1]["perf_mult"], 1.0)
        self.assertGreaterEqual(ents[1]["perf_mult"], 0.5)
        self.assertTrue(ents[2]["no_delivery"])


class EndToEndDryRun(unittest.TestCase):
    def test_fixture_run(self):
        out = run_cli("--config", str(ROOT / "budget-plan.example.json"),
                      "--fixture", str(ROOT / "tests" / "fixture.json"),
                      "--month", "2026-03", "--today", "2026-03-16",
                      "--state-dir", "/nonexistent-state")
        print(out)
        self.assertIn("DRY-RUN", out)
        self.assertIn("projected month-end", out)
        self.assertNotIn("Other Market Test", out)       # excluded by name
        self.assertIn("[held]", out)                      # Brand pinned at 20
        self.assertIn("nothing written", out)

    def test_missing_month_reports_no_target(self):
        out = run_cli("--config", str(ROOT / "budget-plan.example.json"),
                      "--fixture", str(ROOT / "tests" / "fixture.json"),
                      "--month", "2027-01", "--today", "2026-03-16")
        self.assertIn("no target for 2027-01", out)


if __name__ == "__main__":
    unittest.main()
