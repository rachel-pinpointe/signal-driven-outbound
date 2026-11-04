"""Regression suite for Phase 18 step 1: the daily Apollo-enrollment company cap.

The cap counts DISTINCT COMPANIES per calendar day, across every run that day, and it bounds how
many companies get STARTED -- never how many contacts an already-started company yields. That
distinction is the whole point: a contact cap was tried before and removed as a mistake
(MAX_CONTACTS_PER_RUN, 2026-09-19), because it throttled the wrong axis -- leaving eligible
contacts at an in-progress company un-enrolled while doing nothing to bound how many companies
heard from us in a day.

So the tests below pair every "a new company is blocked" case with "an in-progress company is
not", and pin that recording is idempotent per company (a second contact at one company must not
consume a second slot).

Run with: python -m unittest tests.test_enroll_company_cap -v
No network, no API keys. The cache is redirected to a temp SQLite file, so nothing touches
.cache/gtm_cache.db.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae
from src import cache

TODAY = "2026-10-02"
TOMORROW = "2026-10-03"


class CacheBackedTestCase(unittest.TestCase):
    """Each test gets its own SQLite file, so counts never leak between tests or into the real
    cache. _connect_enroll_daily() reads CACHE_PATH at call time, which is what makes this work."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = patch.object(cache, "CACHE_PATH", Path(self.tmpdir) / "test_cache.db")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()


class TestEnrollDailyRunsTable(CacheBackedTestCase):
    def test_a_fresh_day_starts_at_zero(self):
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 0)

    def test_recording_a_company_counts_it_once(self):
        self.assertEqual(cache.record_enrolled_company(TODAY, "co-1"), 1)
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 1)

    def test_recording_the_same_company_again_is_idempotent(self):
        # A second (and third) contact enrolled at one company must not consume slots.
        cache.record_enrolled_company(TODAY, "co-1")
        cache.record_enrolled_company(TODAY, "co-1")
        self.assertEqual(cache.record_enrolled_company(TODAY, "co-1"), 1)
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 1)

    def test_distinct_companies_accumulate(self):
        for i in range(5):
            cache.record_enrolled_company(TODAY, "co-{}".format(i))
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 5)

    def test_has_company_enrolled_today(self):
        cache.record_enrolled_company(TODAY, "co-1")
        self.assertTrue(cache.has_company_enrolled_today(TODAY, "co-1"))
        self.assertFalse(cache.has_company_enrolled_today(TODAY, "co-2"))

    def test_counts_are_isolated_per_calendar_day(self):
        cache.record_enrolled_company(TODAY, "co-1")
        self.assertEqual(cache.get_enrolled_company_count(TOMORROW), 0)
        self.assertFalse(cache.has_company_enrolled_today(TOMORROW, "co-1"))

    def test_counts_survive_a_new_connection(self):
        # The cap has to hold across runs, which means across processes -- an in-memory
        # counter would reset on a second cron invocation the same day.
        cache.record_enrolled_company(TODAY, "co-1")
        cache.record_enrolled_company(TODAY, "co-2")
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 2)

    def test_blank_company_id_is_not_recorded(self):
        cache.record_enrolled_company(TODAY, "")
        self.assertEqual(cache.get_enrolled_company_count(TODAY), 0)
        self.assertFalse(cache.has_company_enrolled_today(TODAY, ""))


class TestCompanyCapReached(unittest.TestCase):
    """company_cap_reached is the decision itself, tested against stubbed counts so the cases
    are exact rather than built up through 25 inserts."""

    def _reached(self, company_id, already, count, cap=25):
        with patch.object(ae.cache, "has_company_enrolled_today", return_value=already), \
             patch.object(ae.cache, "get_enrolled_company_count", return_value=count):
            return ae.company_cap_reached(company_id, TODAY, cap=cap)

    def test_a_new_company_below_the_cap_is_allowed(self):
        self.assertFalse(self._reached("co-new", already=False, count=24))

    def test_a_new_company_at_the_cap_is_blocked(self):
        self.assertTrue(self._reached("co-new", already=False, count=25))

    def test_a_new_company_over_the_cap_is_blocked(self):
        self.assertTrue(self._reached("co-new", already=False, count=40))

    def test_an_in_progress_company_is_allowed_even_at_the_cap(self):
        # The cap bounds companies started, not contacts per company.
        self.assertFalse(self._reached("co-started", already=True, count=25))

    def test_an_in_progress_company_is_allowed_even_over_the_cap(self):
        self.assertFalse(self._reached("co-started", already=True, count=99))

    def test_the_cap_is_configurable_for_tests_but_defaults_to_25(self):
        self.assertEqual(ae.DAILY_ENROLL_COMPANY_CAP, 25)
        self.assertTrue(self._reached("co-new", already=False, count=3, cap=3))


class TestNoContactCapWasReintroduced(unittest.TestCase):
    def test_there_is_no_module_level_contact_cap_constant(self):
        # MAX_CONTACTS_PER_RUN was removed deliberately (69f8a5c). The company cap replaces the
        # *reason* someone reached for a contact cap; it must not bring one back.
        for name in ("MAX_CONTACTS_PER_RUN", "MAX_CONTACTS", "CONTACT_CAP"):
            self.assertFalse(hasattr(ae, name),
                             "{} is back -- the cap must be per-company, not per-contact".format(name))


if __name__ == "__main__":
    unittest.main()
