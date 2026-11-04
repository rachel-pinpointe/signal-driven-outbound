"""Regression suite for the Phase 17 Stage A waterfall change (providers.find_contact_generic).

Before this change `max_candidates` was a TARGET: a rung returning one qualifying candidate
still fell through to web_research and then to paid Apollo discovery trying to reach three. So
a company QuickEnrich already had a real CFO for still cost an LLM call and a paid Apollo
search. The rungs now stop at the first one that yields at least one candidate passing the
title classifier, and `max_candidates` is only a cap WITHIN that rung.

What these lock in, in order of what would actually cost money if it regressed:
  - a free-rung hit means no LLM call and no paid call at all;
  - "yield" means passing classify_fn, not merely returning rows -- a roster of Financial
    Analysts is not a hit, and must still fall through;
  - the within-rung cap still works, so one rung's roster can still produce up to 3.

Run with: python -m unittest tests.test_waterfall_rungs -v
No network and no API keys: all three rungs are mocked, so nothing here spends a credit.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import providers as prov
from src.persona_finance import classify_title

TITLES = ["CFO", "Head of Finance"]


def _qe_record(first, last, title="CFO", has_email=True):
    """One row in the shape qe_contact_finder() returns."""
    return {"first_name": first, "last_name": last, "title": title,
            "has_email": has_email, "employee_linkedin": ""}


def _apollo_record(first, obfuscated_last, title="CFO", apollo_id="apollo-1"):
    """One row in the shape apollo_people_search() returns (surname masked)."""
    return {"apollo_id": apollo_id, "first_name": first,
            "last_name_obfuscated": obfuscated_last, "title": title}


class WaterfallTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = prov.CreditLedger()

    def _run(self, qe=None, wr=None, apollo=None, dry_run=False, max_candidates=3):
        """Runs find_contact_generic with all three rungs mocked, returning
        (result, qe_mock, wr_mock, apollo_mock) so a test can assert on both the
        outcome and which rungs were actually reached."""
        with patch.object(prov, "qe_contact_finder", return_value=qe or []) as m_qe, \
             patch.object(prov, "web_research_find_contact", return_value=wr) as m_wr, \
             patch.object(prov, "apollo_people_search", return_value=apollo or []) as m_apollo:
            result = prov.find_contact_generic(
                "acme.com", TITLES, classify_title, self.ledger,
                dry_run=dry_run, max_candidates=max_candidates)
        return result, m_qe, m_wr, m_apollo


class TestStopsAtFirstYieldingRung(WaterfallTestCase):
    def test_qe_hit_skips_both_later_rungs(self):
        result, _, m_wr, m_apollo = self._run(qe=[_qe_record("Jane", "Doe")])
        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["candidates"]), 1)
        m_wr.assert_not_called()
        m_apollo.assert_not_called()

    def test_one_free_candidate_is_not_topped_up_to_max_candidates(self):
        # The actual behavior change: one hit on the free rung used to trigger an LLM
        # call and a paid Apollo search purely to reach three candidates.
        result, _, m_wr, m_apollo = self._run(
            qe=[_qe_record("Jane", "Doe")], wr={"first_name": "Other", "last_name": "Person",
                                               "name": "Other Person", "title": "CFO",
                                               "linkedin": ""},
            apollo=[_apollo_record("Third", "P***n")], max_candidates=3)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["name"], "Jane Doe")
        m_wr.assert_not_called()
        m_apollo.assert_not_called()

    def test_trace_records_why_later_rungs_were_skipped(self):
        result, _, _, _ = self._run(qe=[_qe_record("Jane", "Doe")])
        self.assertTrue(
            any("stopping at QE rung" in t for t in result["trace"]),
            "the trace should say the waterfall stopped early: {}".format(result["trace"]))


class TestFallThroughWhenARungYieldsNothing(WaterfallTestCase):
    def test_empty_qe_falls_through_to_web_research_only(self):
        result, _, m_wr, m_apollo = self._run(
            qe=[], wr={"first_name": "Wes", "last_name": "Rung", "name": "Wes Rung",
                       "title": "Head of Finance", "linkedin": ""})
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["candidates"][0]["source"], "web_research")
        m_wr.assert_called_once()
        m_apollo.assert_not_called()

    def test_qe_and_web_research_both_empty_reach_apollo(self):
        result, _, m_wr, m_apollo = self._run(
            qe=[], wr=None, apollo=[_apollo_record("Ada", "L***e")])
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["candidates"][0]["source"], "apollo")
        m_wr.assert_called_once()
        m_apollo.assert_called_once()

    def test_non_qualifying_titles_are_not_a_yield(self):
        # "yields at least one candidate passing the title classifier" -- a roster of
        # Financial Analysts is rows, not candidates, and must still fall through.
        result, _, m_wr, _ = self._run(
            qe=[_qe_record("Nina", "Numbers", title="Financial Analyst"),
                _qe_record("Cal", "Clerk", title="Accounting Clerk")],
            wr={"first_name": "Real", "last_name": "Cfo", "name": "Real Cfo",
                "title": "CFO", "linkedin": ""})
        m_wr.assert_called_once()
        self.assertEqual(result["candidates"][0]["name"], "Real Cfo")

    def test_nothing_anywhere_reports_unresolved(self):
        result, _, _, _ = self._run(qe=[], wr=None, apollo=[])
        self.assertEqual(result["status"], "unresolved - no contact found")
        self.assertEqual(result["candidates"], [])


class TestMaxCandidatesStillCapsWithinARung(WaterfallTestCase):
    def test_one_rung_can_still_fill_the_cap(self):
        roster = [_qe_record("A", "One"), _qe_record("B", "Two"), _qe_record("C", "Three"),
                  _qe_record("D", "Four"), _qe_record("E", "Five")]
        result, _, m_wr, _ = self._run(qe=roster, max_candidates=3)
        self.assertEqual(len(result["candidates"]), 3)
        m_wr.assert_not_called()

    def test_cap_of_one_is_respected(self):
        roster = [_qe_record("A", "One"), _qe_record("B", "Two")]
        result, _, _, _ = self._run(qe=roster, max_candidates=1)
        self.assertEqual(len(result["candidates"]), 1)

    def test_roster_beyond_the_cap_is_recorded_in_the_trace(self):
        roster = [_qe_record("A", "One"), _qe_record("B", "Two"), _qe_record("C", "Three"),
                  _qe_record("D", "Four")]
        result, _, _, _ = self._run(qe=roster, max_candidates=3)
        self.assertTrue(any("not taken" in t for t in result["trace"]))


class TestDryRun(WaterfallTestCase):
    def test_dry_run_with_a_qe_hit_is_found_and_spends_nothing(self):
        result, _, m_wr, m_apollo = self._run(qe=[_qe_record("Jane", "Doe")], dry_run=True)
        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["candidates"]), 1)
        m_wr.assert_not_called()
        m_apollo.assert_not_called()

    def test_dry_run_without_a_qe_hit_stops_before_the_llm_rung(self):
        result, _, m_wr, m_apollo = self._run(qe=[], dry_run=True)
        self.assertEqual(result["status"], "dry-run - no QE candidate")
        self.assertEqual(result["candidates"], [])
        m_wr.assert_not_called()
        m_apollo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
