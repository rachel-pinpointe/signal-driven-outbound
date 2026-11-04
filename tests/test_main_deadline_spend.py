"""Regression suite for Phase 18 step 3: main.py's internal deadline, spend log, and SIGTERM handler.

All three exist for one reason: run-cron.sh's outer time-box kills the whole process group with
SIGTERM and then SIGKILL five seconds later. Before this, that killed a run mid-company with no
CSVs written, no Fibery write-back for the company in flight, and no record of what had been spent
-- which is exactly the run you most need a record of.

  - The internal deadline (default 1500s, 300s under the outer 1800s box) stops the run STARTING
    new companies, so it finishes cleanly and still writes its outputs.
  - The spend log is flushed per company, so the on-disk record is never behind by more than the
    company in hand, however the process ends.
  - The SIGTERM handler means even a real kill leaves a summary behind.

Run with: python -m unittest tests.test_main_deadline_spend -v
No network, no API keys. Logs go to temp files; no real signal is ever raised.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as m
from src.providers import CreditLedger


class TestDeadlineExceeded(unittest.TestCase):
    """Pure time arithmetic, driven by an injected `now` so the tests never actually wait."""

    def test_not_exceeded_before_the_budget(self):
        self.assertFalse(m._deadline_exceeded(1000.0, 1500, now=1000.0 + 1499))

    def test_exceeded_exactly_at_the_budget(self):
        self.assertTrue(m._deadline_exceeded(1000.0, 1500, now=1000.0 + 1500))

    def test_exceeded_past_the_budget(self):
        self.assertTrue(m._deadline_exceeded(1000.0, 1500, now=1000.0 + 5000))

    def test_a_falsy_deadline_disables_the_check(self):
        # An interactive run or a test can opt out entirely.
        for deadline in (None, 0):
            with self.subTest(deadline=deadline):
                self.assertFalse(m._deadline_exceeded(0.0, deadline, now=1e9))

    def test_the_default_sits_below_the_outer_time_box(self):
        # The 300s of headroom is the entire point -- it must not drift up to or past 1800.
        self.assertEqual(m.DEFAULT_MAIN_DEADLINE_SEC, 1500)
        self.assertLess(m.DEFAULT_MAIN_DEADLINE_SEC, 1800)


class TestSpendLog(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "nested" / "spend_20261002.log"
        self.ledger = CreditLedger(qe_budget=25)

    def _lines(self):
        return [l for l in self.log.read_text().splitlines() if l.strip()]

    def test_a_line_is_written_per_call(self):
        m._append_spend_line(self.ledger, "[1/3] Acme", log_path=self.log)
        m._append_spend_line(self.ledger, "[2/3] Globex", log_path=self.log)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Acme", lines[0])
        self.assertIn("Globex", lines[1])

    def test_each_line_carries_the_running_ledger_summary(self):
        self.ledger.charge("quickenrich", 3, remaining=100)
        m._append_spend_line(self.ledger, "[1/1] Acme", log_path=self.log)
        self.assertIn("QuickEnrich 3 credits", self._lines()[0])

    def test_the_line_is_timestamped(self):
        m._append_spend_line(self.ledger, "[1/1] Acme", log_path=self.log)
        self.assertRegex(self._lines()[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\t")

    def test_a_missing_parent_directory_is_created(self):
        self.assertFalse(self.log.parent.exists())
        m._append_spend_line(self.ledger, "x", log_path=self.log)
        self.assertTrue(self.log.exists())

    def test_an_unwritable_path_does_not_raise(self):
        try:
            m._append_spend_line(self.ledger, "x",
                                 log_path=Path("/dev/null/nope/spend.log"))
        except OSError as exc:
            self.fail("spend logging failure escaped: {!r}".format(exc))

    def test_the_default_path_is_a_dated_file_under_outputs(self):
        path = m._spend_log_path()
        self.assertEqual(path.parent, Path("outputs"))
        self.assertRegex(path.name, r"^spend_\d{8}\.log$")


class TestSigtermHandler(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "spend.log"
        self.ledger = CreditLedger(qe_budget=25)

    def test_installing_returns_a_callable_handler(self):
        with patch("signal.signal") as mock_signal:
            handler = m._install_sigterm_handler(self.ledger, log_path=self.log)
        self.assertTrue(callable(handler))
        mock_signal.assert_called_once()

    def test_the_handler_is_registered_for_sigterm(self):
        import signal as signal_mod
        with patch("signal.signal") as mock_signal:
            m._install_sigterm_handler(self.ledger, log_path=self.log)
        args, _ = mock_signal.call_args
        self.assertEqual(args[0], signal_mod.SIGTERM)

    def test_the_handler_writes_a_spend_line_and_exits_143(self):
        self.ledger.charge("quickenrich", 7, remaining=50)
        with patch("signal.signal"):
            handler = m._install_sigterm_handler(self.ledger, log_path=self.log)
        with self.assertRaises(SystemExit) as ctx:
            handler(15, None)
        self.assertEqual(ctx.exception.code, 143)
        written = self.log.read_text()
        self.assertIn("SIGTERM", written)
        self.assertIn("QuickEnrich 7 credits", written)

    def test_the_handler_prints_the_ledger_summary(self):
        self.ledger.charge("apollo", 2)
        with patch("signal.signal"):
            handler = m._install_sigterm_handler(self.ledger, log_path=self.log)
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit):
            handler(15, None)
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("Spend at SIGTERM", printed)
        self.assertIn("Apollo 2 verified emails", printed)

    def test_the_handler_surfaces_ledger_warnings(self):
        self.ledger.warnings.append("QuickEnrich balance low: 3 credits left")
        with patch("signal.signal"):
            handler = m._install_sigterm_handler(self.ledger, log_path=self.log)
        with patch("builtins.print") as mock_print, self.assertRaises(SystemExit):
            handler(15, None)
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("balance low", printed)


if __name__ == "__main__":
    unittest.main()
