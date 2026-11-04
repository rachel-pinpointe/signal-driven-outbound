"""Regression suite for Phase 17 step 4: person-level dedupe at its three call sites.

providers.same_person itself is covered in tests/test_contact_dedupe.py. This file is about
whether each site actually APPLIES it, and at the right moment:

  1. providers.find_contact_generic  -- within a rung, before a candidate is accepted.
  2. providers.find_persona_contacts_generic -- before Stage B, so one human costs one email
     resolution rather than two. This is the one that protects real spend.
  3. main._contact_write_skip_reason -- before a Fibery create, across every persona at one
     company, first persona winning.

The asymmetry from test_contact_dedupe.py applies here too: a false merge silently drops a real
person, so every class below pairs its "these must merge" case with a "these must not" case.

Run with: python -m unittest tests.test_person_dedupe_sites -v
No network, no API keys, no cache writes, no Fibery -- every rung and Stage B is mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as m
from src import providers as prov
from src.persona_finance import classify_title

TITLES = ["CFO", "Head of Finance"]


def _qe(first, last, title="CFO"):
    return {"first_name": first, "last_name": last, "title": title,
            "has_email": True, "employee_linkedin": ""}


def _apollo(first, masked_last, apollo_id):
    return {"apollo_id": apollo_id, "first_name": first,
            "last_name_obfuscated": masked_last, "title": "CFO"}


def _stage_a(*candidates):
    return {"candidates": list(candidates), "trace": ["stage A (mocked)"], "status": "found"}


def _candidate(name, first="", last="", linkedin=""):
    return {"rank": 1, "tier": "CFO", "title": "CFO", "name": name,
            "first_name": first, "last_name": last, "linkedin": linkedin,
            "has_email": True, "source": "quickenrich"}


# --------------------------------------------------------------------------
# Site 1: find_contact_generic
# --------------------------------------------------------------------------

class TestFindContactGenericDedupe(unittest.TestCase):
    def _run(self, qe=None, apollo=None):
        ledger = prov.CreditLedger()
        with patch.object(prov, "qe_contact_finder", return_value=qe or []), \
             patch.object(prov, "web_research_find_contact", return_value=None), \
             patch.object(prov, "apollo_people_search", return_value=apollo or []):
            return prov.find_contact_generic("acme.com", TITLES, classify_title, ledger)

    def test_nickname_pair_in_one_roster_collapses_to_one_candidate(self):
        # Hollis: the same person listed twice under two spellings. The old exact
        # (first, last) key could not see this and produced two candidates.
        result = self._run(qe=[_qe("Samantha", "Hollis"), _qe("Sam", "Hollis")])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["name"], "Samantha Hollis")

    def test_the_drop_is_recorded_in_the_trace(self):
        result = self._run(qe=[_qe("Samantha", "Hollis"), _qe("Sam", "Hollis")])
        self.assertTrue(any("same-person duplicate" in t for t in result["trace"]),
                        "expected the drop in the trace: {}".format(result["trace"]))

    def test_two_different_people_sharing_a_first_name_are_both_kept(self):
        # Peter Lindqvist and Peter Oduya are both really at Northwind.
        result = self._run(qe=[_qe("Peter", "Lindqvist"), _qe("Peter", "Oduya")])
        self.assertEqual(len(result["candidates"]), 2)

    def test_apollo_masked_surnames_for_one_person_collapse(self):
        # Only reachable because last_name_obfuscated is now carried through from the
        # search result instead of being discarded.
        result = self._run(apollo=[_apollo("Robert", "Go***i", "a-1"),
                                   _apollo("Robert", "Go***i", "a-2")])
        self.assertEqual(len(result["candidates"]), 1)

    def test_apollo_masked_surnames_with_different_initials_stay_separate(self):
        result = self._run(apollo=[_apollo("Robert", "Go***i", "a-1"),
                                   _apollo("Robert", "Ha***s", "a-2")])
        self.assertEqual(len(result["candidates"]), 2)

    def test_masked_surname_is_kept_but_never_passed_off_as_a_real_one(self):
        result = self._run(apollo=[_apollo("Robert", "Go***i", "a-1")])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["last_name"], "")
        self.assertEqual(candidate["last_name_obfuscated"], "Go***i")


# --------------------------------------------------------------------------
# Site 2: find_persona_contacts_generic (the one that protects spend)
# --------------------------------------------------------------------------

class TestFindPersonaContactsDedupeBeforeStageB(unittest.TestCase):
    def _run(self, *candidates):
        """Patches the cache so fetch_fn runs directly (no .cache writes, no TTL) and
        counts Stage B calls."""
        ledger = prov.CreditLedger()
        calls = []

        def fake_stage_b(candidate, domain, ledger_, dry_run=False):
            calls.append(candidate)
            return {"email": "person{}@acme.com".format(len(calls)), "source": "QuickEnrich",
                    "confidence": "verified", "linkedin": "", "name": "",
                    "status": "resolved", "trace": ["stage B (mocked)"]}

        with patch.object(prov.cache, "get_or_fetch",
                          side_effect=lambda table, company, website, fetch_fn, **kw: fetch_fn()), \
             patch.object(prov, "find_contact_generic", return_value=_stage_a(*candidates)), \
             patch.object(prov, "enrich_contact_generic", side_effect=fake_stage_b):
            envelope = prov.find_persona_contacts_generic(
                "cfo", "Acme", "https://acme.com", TITLES, classify_title, ledger)
        return envelope, calls

    def test_duplicate_pair_costs_exactly_one_stage_b_resolution(self):
        # Natarajan: the pair reached Stage B as two candidates and was resolved twice,
        # by Hunter and by Apollo, before anything compared them as people.
        envelope, calls = self._run(
            _candidate("Vikram Natarajan", "Vikram", "Natarajan"),
            _candidate("Vikram Natarajan", "Vikram", "Natarajan"))
        self.assertEqual(len(calls), 1, "Stage B should run once for one human")
        self.assertEqual(len(envelope["contacts"]), 1)

    def test_nickname_pair_also_costs_one_resolution(self):
        _envelope, calls = self._run(_candidate("Sam Hollis", "Sam", "Hollis"),
                                     _candidate("Samantha Hollis", "Samantha", "Hollis"))
        self.assertEqual(len(calls), 1)

    def test_two_real_people_both_reach_stage_b(self):
        envelope, calls = self._run(_candidate("Peter Lindqvist", "Peter", "Lindqvist"),
                                    _candidate("Peter Oduya", "Peter", "Oduya"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(envelope["contacts"]), 2)

    def test_stage_b_itself_is_unchanged_and_still_receives_the_candidate_shape(self):
        # Step 4 changes WHICH candidates reach Stage B, never Stage B's own contract.
        _envelope, calls = self._run(_candidate("Jane Doe", "Jane", "Doe"))
        self.assertEqual(calls[0]["first_name"], "Jane")
        self.assertEqual(calls[0]["last_name"], "Doe")


# --------------------------------------------------------------------------
# Site 3: main._contact_write_skip_reason (cross-persona, before a Fibery create)
# --------------------------------------------------------------------------

def _seen(name, persona, email="seen@acme.com", linkedin=""):
    return {"name": name, "persona": persona, "email": email, "linkedin": linkedin}


class TestContactWriteSkipReason(unittest.TestCase):
    def test_no_one_seen_yet_never_skips(self):
        self.assertIsNone(m._contact_write_skip_reason(
            {"contact_name": "Jane Doe", "email": "jane@acme.com", "linkedin": ""}, []))

    def test_same_person_from_an_earlier_persona_is_skipped(self):
        reason = m._contact_write_skip_reason(
            {"contact_name": "Sam Hollis", "email": "sam@acme.com", "linkedin": ""},
            [_seen("Samantha Hollis", "cfo", "samantha@acme.com")])
        self.assertIsNotNone(reason)
        self.assertIn("already written this run", reason)
        self.assertIn("cfo", reason)

    def test_different_emails_for_one_person_are_still_caught(self):
        # Whitlock: the two records carry DIFFERENT emails, so an email-equality check
        # cannot see this at all. This is the case person-level dedupe exists for.
        reason = m._contact_write_skip_reason(
            {"contact_name": "Dana Whitlock", "email": "d.whitlock@acme.com", "linkedin": ""},
            [_seen("Dana Whitlock", "cfo", "dana@acme.com")])
        self.assertIsNotNone(reason)

    def test_last_initial_against_full_surname_is_caught(self):
        reason = m._contact_write_skip_reason(
            {"contact_name": "Robert Gorski", "email": "rgorski@acme.com", "linkedin": ""},
            [_seen("Robert G", "recruiter_ta", "rob@acme.com")])
        self.assertIsNotNone(reason)

    def test_a_genuinely_different_person_is_not_skipped(self):
        self.assertIsNone(m._contact_write_skip_reason(
            {"contact_name": "Peter Lindqvist", "email": "plindqvist@example.io", "linkedin": ""},
            [_seen("Peter Oduya", "cfo", "poduya@example.io")]))

    def test_first_persona_wins_ordering(self):
        # Whoever is already in the list wins; the reason names them, not the newcomer.
        reason = m._contact_write_skip_reason(
            {"contact_name": "Samantha Hollis", "email": "samantha@acme.com", "linkedin": ""},
            [_seen("Sam Hollis", "hr_people_ops", "sam@acme.com")])
        self.assertIn("hr_people_ops", reason)
        self.assertIn("Sam Hollis", reason)


if __name__ == "__main__":
    unittest.main()
