"""Phase 17 step 7: one regression test per REAL pair that motivated this work.

Every case below is a specific pair of rows that actually reached Fibery or Apollo, not an
invented scenario. Each is pinned at the layer that would have stopped it, so a regression fails
on the case that caused it rather than on a synthetic abstraction of it.

Public copy: every name, email, LinkedIn slug and domain below is a fictional stand-in. Each
keeps the structure that made the real pair hard (shared first name, shared surname and initial,
nickname pair, masked surname, mismatched domain), so the tests exercise the same logic:

  Whitlock      -- QuickEnrich and Apollo, DIFFERENT emails for one person. The case that proves
                 why dedupe cannot be email-based: an email check is blind here by construction.
  Hollis     -- "Sam" and "Samantha", one roster and across two personas. Nickname-aware.
  Natarajan -- resolved twice, once via Hunter and once via Apollo. Hunter and Apollo are
                 Stage B resolvers, so the duplication showed up as two candidates each
                 resolved by a different provider; only one may now reach Stage B.
  Gorski        -- "Robert G" (Apollo-masked) vs "Robert Gorski", plus a stale formerco.io address.
  Marisol    -- a priorco.io email on a globex.co company record.
  Okafor     -- one email, two personas, and a contractco.io address on an Initech record.
  Quill / Renner / Sorensen -- one email shared across two companies' contact records.
  Lindqvist vs Oduya at Northwind -- the NEGATIVE case. Two real, different people who share a first
                 name. Nothing here may ever merge them; a false merge silently drops someone
                 from outreach, which is worse than the duplicates this phase exists to stop.

Run with: python -m unittest tests.test_phase17_real_pairs -v
No network, no API keys, no Fibery writes, and no Stage B contract changes -- every provider rung
and Stage B itself is mocked, and the manual-review log is redirected to a temp file.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as m
from src import providers as prov
from src.persona_finance import classify_title

TITLES = ["CFO", "Head of Finance"]


# --- shared fixtures ------------------------------------------------------

def _qe(first, last, title="CFO"):
    return {"first_name": first, "last_name": last, "title": title,
            "has_email": True, "employee_linkedin": ""}


def _candidate(name, first="", last="", masked_last="", source="quickenrich"):
    return {"rank": 1, "tier": "CFO", "title": "CFO", "name": name, "first_name": first,
            "last_name": last, "last_name_obfuscated": masked_last, "linkedin": "",
            "has_email": True, "source": source}


def _contact(name, email, linkedin=""):
    return {"contact_name": name, "email": email, "linkedin": linkedin}


def _seen(name, persona, email):
    return {"name": name, "persona": persona, "email": email, "linkedin": ""}


def _stage_a_candidates(*candidates):
    return {"candidates": list(candidates), "trace": ["stage A (mocked)"], "status": "found"}


def _run_stage_a(qe=None, apollo=None):
    ledger = prov.CreditLedger()
    with patch.object(prov, "qe_contact_finder", return_value=qe or []), \
         patch.object(prov, "web_research_find_contact", return_value=None), \
         patch.object(prov, "apollo_people_search", return_value=apollo or []):
        return prov.find_contact_generic("acme.com", TITLES, classify_title, ledger)


def _run_persona_waterfall(*candidates):
    """Returns (envelope, stage_b_calls). Stage B is mocked and each call returns a distinct
    email, so a duplicate surviving to Stage B is visible as a second call."""
    ledger = prov.CreditLedger()
    calls = []

    def fake_stage_b(candidate, domain, ledger_, dry_run=False):
        calls.append(candidate)
        return {"email": "resolved{}@acme.com".format(len(calls)), "source": "QuickEnrich",
                "confidence": "verified", "linkedin": "", "name": "",
                "status": "resolved", "trace": ["stage B (mocked)"]}

    with patch.object(prov.cache, "get_or_fetch",
                      side_effect=lambda table, company, website, fetch_fn, **kw: fetch_fn()), \
         patch.object(prov, "find_contact_generic",
                      return_value=_stage_a_candidates(*candidates)), \
         patch.object(prov, "enrich_contact_generic", side_effect=fake_stage_b):
        envelope = prov.find_persona_contacts_generic(
            "cfo", "Acme", "https://acme.com", TITLES, classify_title, ledger)
    return envelope, calls


# --- Whitlock ---------------------------------------------------------------

class TestWhitlock(unittest.TestCase):
    """QuickEnrich held dana@, Apollo held d.whitlock@ -- one person, two addresses."""

    def test_an_email_check_is_blind_to_this_pair(self):
        # Establishes WHY person-level dedupe had to exist: with no person context, the
        # second address sails through the cross-record email guard untouched.
        self.assertIsNone(m._contact_write_skip_reason(
            _contact("Dana Whitlock", "d.whitlock@acme.com"), [],
            company_domain="acme.com",
            existing_emails={"dana@acme.com": {"id": "c-1", "name": "Dana Whitlock",
                                                "company_id": "co-1"}}))

    def test_person_level_dedupe_catches_it(self):
        reason = m._contact_write_skip_reason(
            _contact("Dana Whitlock", "d.whitlock@acme.com"),
            [_seen("Dana Whitlock", "cfo", "dana@acme.com")],
            company_domain="acme.com")
        self.assertIsNotNone(reason)
        self.assertIn("same person already written", reason)


# --- Hollis --------------------------------------------------------------

class TestHollis(unittest.TestCase):
    """"Sam Hollis" and "Samantha Hollis" -- the same person under two spellings."""

    def test_one_roster_holding_both_spellings_yields_one_candidate(self):
        result = _run_stage_a(qe=[_qe("Samantha", "Hollis"), _qe("Sam", "Hollis")])
        self.assertEqual(len(result["candidates"]), 1)

    def test_a_second_persona_does_not_create_a_twin(self):
        reason = m._contact_write_skip_reason(
            _contact("Sam Hollis", "sam@acme.com"),
            [_seen("Samantha Hollis", "cfo", "samantha@acme.com")],
            company_domain="acme.com")
        self.assertIsNotNone(reason)
        self.assertIn("cfo", reason)


# --- Natarajan ----------------------------------------------------------

class TestNatarajan(unittest.TestCase):
    """Resolved twice, once by Hunter and once by Apollo. Hunter and Apollo are Stage B
    resolvers, so the duplication arrived as two Stage A candidates for one person."""

    def test_only_one_candidate_reaches_stage_b(self):
        envelope, calls = _run_persona_waterfall(
            _candidate("Vikram Natarajan", "Vikram", "Natarajan"),
            _candidate("Vikram Natarajan", "Vikram", "Natarajan", source="apollo"))
        self.assertEqual(len(calls), 1, "one human must cost one Stage B resolution")
        self.assertEqual(len(envelope["contacts"]), 1)

    def test_stage_b_contract_is_unchanged(self):
        # Step 4/7 change WHICH candidates reach Stage B, never what Stage B receives.
        _envelope, calls = _run_persona_waterfall(
            _candidate("Vikram Natarajan", "Vikram", "Natarajan"))
        self.assertEqual(calls[0]["first_name"], "Vikram")
        self.assertEqual(calls[0]["last_name"], "Natarajan")
        self.assertEqual(calls[0]["title"], "CFO")


# --- Gorski -----------------------------------------------------------------

class TestGill(unittest.TestCase):
    """Apollo's masked "Robert G" beside Hunter's "Robert Gorski", plus a stale formerco.io
    address left on the record."""

    def test_masked_surname_matches_the_full_one(self):
        self.assertTrue(prov.same_person({"name": "Robert G"}, {"name": "Robert Gorski"}))

    def test_a_second_record_for_him_is_not_created(self):
        reason = m._contact_write_skip_reason(
            _contact("Robert Gorski", "rgorski@acme.com"),
            [_seen("Robert G", "recruiter_ta", "rob@acme.com")],
            company_domain="acme.com")
        self.assertIsNotNone(reason)

    def test_the_stale_arine_io_address_is_caught_by_the_domain_guard(self):
        reason = m._contact_write_skip_reason(
            _contact("Robert Gorski", "robert@formerco.io"), [], company_domain="acme.com")
        self.assertIsNotNone(reason)
        self.assertIn("formerco.io", reason)
        self.assertIn("acme.com", reason)


# --- Marisol -------------------------------------------------------------

class TestMarisol(unittest.TestCase):
    """A priorco.io email sitting on a globex.co company record."""

    def test_the_mismatch_is_skipped_and_names_both_domains(self):
        reason = m._contact_write_skip_reason(
            _contact("Marisol", "m@priorco.io"), [], company_domain="globex.co")
        self.assertIsNotNone(reason)
        self.assertIn("priorco.io", reason)
        self.assertIn("globex.co", reason)

    def test_the_skip_is_logged_rather_than_dropped(self):
        log = Path(tempfile.mkdtemp()) / "manual_review_skips.log"
        reason = m._contact_write_skip_reason(
            _contact("Marisol", "m@priorco.io"), [], company_domain="globex.co")
        m._log_manual_review("Globex", "cfo", _contact("Marisol", "m@priorco.io"),
                             reason, log_path=log)
        written = log.read_text()
        self.assertIn("m@priorco.io", written)
        self.assertIn("globex.co", written)


# --- Okafor --------------------------------------------------------------

class TestOkafor(unittest.TestCase):
    """One email, two personas, and a contractco.io address on an Initech record."""

    def test_the_contractco_address_fails_the_domain_guard(self):
        reason = m._contact_write_skip_reason(
            _contact("Sam Okafor", "sam@contractco.io"), [], company_domain="initech.com")
        self.assertIsNotNone(reason)
        self.assertIn("contractco.io", reason)

    def test_the_same_email_from_a_second_persona_in_one_run_is_caught(self):
        # main.py adds each created email to the index mid-run, which is what makes this
        # reachable -- a startup snapshot alone could not see a create from this same run.
        reason = m._contact_write_skip_reason(
            _contact("S. Okafor", "sam@initech.com"), [],
            company_domain="initech.com",
            existing_emails={"sam@initech.com": {"id": "(this run)", "name": "Sam Okafor",
                                                   "company_id": "co-initech"}})
        self.assertIsNotNone(reason)
        self.assertIn("already on Fibery contact", reason)


# --- Quill / Renner / Sorensen -------------------------------------------

class TestQuillRennerSorensen(unittest.TestCase):
    """One address shared across two companies' contact records."""

    def test_the_second_company_does_not_get_its_own_record(self):
        shared = "shared@example.com"
        reason = m._contact_write_skip_reason(
            _contact("Renner", shared), [], company_domain="example.com",
            existing_emails={shared: {"id": "c-quill", "name": "Quill",
                                      "company_id": "co-first"}})
        self.assertIsNotNone(reason)
        self.assertIn("co-first", reason)

    def test_a_third_record_for_the_same_address_is_also_skipped(self):
        shared = "shared@example.com"
        index = {shared: {"id": "c-quill", "name": "Quill", "company_id": "co-first"}}
        for name in ("Renner", "Sorensen"):
            with self.subTest(name=name):
                self.assertIsNotNone(m._contact_write_skip_reason(
                    _contact(name, shared), [], company_domain="example.com",
                    existing_emails=index))


# --- NEGATIVE: Lindqvist vs Oduya ------------------------------------------

class TestLindqvistVsOduyaMustNotMerge(unittest.TestCase):
    """Peter Lindqvist and Peter Oduya are both genuinely at Northwind. A false merge drops one of
    them from outreach with no trace, which is worse than the duplicate rows Phase 17 stops --
    so this is the case every layer is checked against."""

    def test_same_person_says_no(self):
        self.assertFalse(prov.same_person({"name": "Peter Lindqvist"}, {"name": "Peter Oduya"}))

    def test_stage_a_keeps_both(self):
        result = _run_stage_a(qe=[_qe("Peter", "Lindqvist"), _qe("Peter", "Oduya")])
        names = sorted(c["name"] for c in result["candidates"])
        self.assertEqual(names, ["Peter Lindqvist", "Peter Oduya"])

    def test_both_reach_stage_b(self):
        envelope, calls = _run_persona_waterfall(
            _candidate("Peter Lindqvist", "Peter", "Lindqvist"),
            _candidate("Peter Oduya", "Peter", "Oduya"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(envelope["contacts"]), 2)

    def test_the_second_one_is_still_written_to_fibery(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact("Peter Oduya", "poduya@example.io"),
            [_seen("Peter Lindqvist", "cfo", "plindqvist@example.io")],
            company_domain="example.io"))

    def test_neither_is_blocked_by_the_other_s_email(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact("Peter Oduya", "poduya@example.io"), [],
            company_domain="example.io",
            existing_emails={"plindqvist@example.io": {"id": "c-1", "name": "Peter Lindqvist",
                                                   "company_id": "co-northwind"}}))


if __name__ == "__main__":
    unittest.main()
