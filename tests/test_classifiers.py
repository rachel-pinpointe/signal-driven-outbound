"""Regression suite for the title-classifier bug class (Phase 3, OPEN_ITEMS #7)
plus the shared multi-persona classifier (Phase 8).

Locks in two fixes so they can't silently regress:
  - the "Associate" entry-level bug (company_research.is_entry_level)
  - the domain-mismatch heuristic (providers._agency_domain_flag)

Phase 8 added a second suite: providers.classify_by_tiers (the shared
non-qualifying/tier-ladder logic every persona's classify_title() now
calls) plus a spot-check of each of the three live personas' own tier
ladders (persona_finance/persona_people_ops/persona_recruiter_ta),
covering senior/mid/individual-contributor titles and the shared
NON_QUALIFYING rejections (analyst, coordinator, intern, etc.) across
all three. persona_people_ops/persona_recruiter_ta's ladders are drafts
Rachel hasn't reviewed yet (see their module docstrings) -- these tests
lock in the CURRENT behavior of that draft, not a confirmed spec, so a
deliberate wording change to those ladders is expected to mean updating
these tests, not a bug.

Run with: python -m unittest tests.test_classifiers -v
No network, no API keys required -- pure logic tests, nothing here spends
real credits.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.company_research import is_entry_level
from src.providers import _agency_domain_flag, classify_by_tiers, NON_QUALIFYING
from src import persona_finance, persona_people_ops, persona_recruiter_ta


class TestAssociateClassifierBug(unittest.TestCase):
    """Real cases from CLAUDE.md's Known Issues: 'Associate Director' and
    'Associate General Counsel' at $260K were both being tagged entry-level
    on the word 'associate' alone, regardless of posted comp."""

    def test_associate_director_with_high_salary_is_not_entry_level(self):
        self.assertFalse(is_entry_level("Associate Director", 260_000))

    def test_associate_general_counsel_at_260k_is_not_entry_level(self):
        self.assertFalse(is_entry_level("Associate General Counsel", 260_000))

    def test_associate_with_salary_right_at_ceiling_is_not_entry_level(self):
        # >= the ceiling, not just strictly above it.
        self.assertFalse(is_entry_level("Associate Counsel", 150_000))

    def test_associate_with_low_salary_is_still_entry_level(self):
        self.assertTrue(is_entry_level("Marketing Associate", 60_000))

    def test_associate_with_unknown_salary_falls_back_to_entry_level(self):
        # No salary data at all -- keyword match still applies, same as
        # before the fix. Only a KNOWN salary at/above the ceiling overrides it.
        self.assertTrue(is_entry_level("Research Associate", None))

    def test_unambiguous_entry_level_keywords_are_not_overridden_by_salary(self):
        # SDR/BDR/intern/coordinator/junior/etc. stay absolute matches,
        # unlike "associate" -- a high posted number on one of these is a
        # base+steep-OTE artifact, not evidence of seniority. The fix must
        # not have broadened to these keywords.
        self.assertTrue(is_entry_level("Sales Development Representative", 200_000))
        self.assertTrue(is_entry_level("Marketing Coordinator", 180_000))
        self.assertTrue(is_entry_level("Junior Software Engineer", 175_000))

    def test_no_entry_level_keyword_still_uses_salary_only(self):
        # Unaffected control: a title with no entry-level word at all still
        # goes purely on salary, same as before the fix.
        self.assertTrue(is_entry_level("Software Engineer", 100_000))
        self.assertFalse(is_entry_level("Software Engineer", 200_000))
        self.assertFalse(is_entry_level("Software Engineer", None))


class TestDomainMismatchHeuristic(unittest.TestCase):
    """Modeled on a real case (address fictionalized): an unrelated-domain
    address correctly flagged as not a dbt Labs employee. Plus the false-positive gap the old substring-only
    check had on formatting differences, and the new ambiguous middle band.

    Phase 8: _agency_domain_flag moved from cfo_enrich.py (deleted) to
    providers.py, shared by all three personas' Stage B enrichment -- same
    function, same behavior, just a new home."""

    def test_real_documented_mismatch_still_flags(self):
        # The actual dbt Labs agency-domain case from CLAUDE.md's Known Issues.
        flag = _agency_domain_flag("pat@example.net", "dbtlabs.com")
        self.assertIn("WARNING", flag)
        self.assertIn("example.net", flag)

    def test_exact_domain_match_no_flag(self):
        self.assertEqual(_agency_domain_flag("person@acme.com", "acme.com"), "")

    def test_subdomain_match_no_flag(self):
        self.assertEqual(_agency_domain_flag("person@boards.acme.com", "acme.com"), "")

    def test_formatting_only_difference_no_longer_false_positive(self):
        # The concrete gap the old substring-only check had: same company,
        # different domain formatting. Must not be a hard WARNING anymore.
        flag = _agency_domain_flag("person@acme-inc.com", "acme.com")
        self.assertEqual(flag, "")

    def test_corp_suffix_variant_no_longer_false_positive(self):
        flag = _agency_domain_flag("person@acme-group.io", "acme.com")
        self.assertEqual(flag, "")

    def test_ambiguous_similar_domain_gets_human_glance_not_silent_decision(self):
        # "acme" vs "acmecorp" -- plausibly the same company, plausibly not.
        # Must be neither a silent pass nor a hard WARNING.
        flag = _agency_domain_flag("person@acmecorp.io", "acme.com")
        self.assertIn("VERIFY BY EYE", flag)
        self.assertNotIn("WARNING", flag)

    def test_no_company_domain_no_flag(self):
        self.assertEqual(_agency_domain_flag("person@acme.com", ""), "")

    def test_no_email_no_flag(self):
        self.assertEqual(_agency_domain_flag("", "acme.com"), "")


class TestSharedTierClassifier(unittest.TestCase):
    """providers.classify_by_tiers + NON_QUALIFYING (Phase 8): the logic
    every persona's classify_title() now shares, tested directly against a
    minimal tier ladder so these cases don't depend on any one persona's
    real title list."""

    TIERS = [
        (1, "Top", re.compile(r"\bchief\b", re.I)),
        (2, "Mid", re.compile(r"\bmanager\b", re.I)),
    ]

    def test_blank_title_returns_none(self):
        self.assertIsNone(classify_by_tiers("", self.TIERS))
        self.assertIsNone(classify_by_tiers(None, self.TIERS))

    def test_non_qualifying_title_returns_none_even_if_tier_would_match(self):
        # "Manager" matches tier 2, but "Analyst" in the same title is a
        # hard non-qualifying signal and must win.
        self.assertIsNone(classify_by_tiers("Financial Analyst Manager", self.TIERS))

    def test_matching_tier_returns_rank_and_label(self):
        result = classify_by_tiers("Chief Somebody Officer", self.TIERS)
        self.assertEqual(result, {"rank": 1, "tier": "Top"})

    def test_no_tier_match_returns_none(self):
        self.assertIsNone(classify_by_tiers("Staff Engineer", self.TIERS))

    def test_non_qualifying_rejects_common_junior_titles(self):
        for title in ("Financial Analyst", "Controller", "Bookkeeper",
                      "Accounts Payable Specialist", "Marketing Intern",
                      "HR Coordinator", "Executive Assistant to the CFO",
                      "Front Desk Receptionist", "Filing Clerk"):
            self.assertIsNone(classify_by_tiers(title, self.TIERS),
                              "expected {!r} to be rejected as non-qualifying".format(title))


class TestPersonaTierLadders(unittest.TestCase):
    """Spot-check of each live persona's own classify_title() (Phase 8).
    Not exhaustive -- one senior, one mid, one rejected title per persona,
    confirming each module's TITLE_TIERS + the shared NON_QUALIFYING
    filter are wired up correctly end to end."""

    def test_finance_cfo_is_top_tier(self):
        result = persona_finance.classify_title("Chief Financial Officer")
        self.assertEqual(result["rank"], 1)
        self.assertEqual(result["tier"], "CFO")

    def test_finance_vp_finance_tier(self):
        result = persona_finance.classify_title("VP of Finance")
        self.assertEqual(result["tier"], "VP Finance")

    def test_finance_rejects_financial_analyst(self):
        self.assertIsNone(persona_finance.classify_title("Financial Analyst"))

    def test_people_ops_cpo_is_top_tier(self):
        result = persona_people_ops.classify_title("Chief People Officer")
        self.assertEqual(result["rank"], 1)

    def test_people_ops_hr_manager_tier(self):
        result = persona_people_ops.classify_title("HR Manager")
        self.assertIsNotNone(result)
        self.assertEqual(result["tier"], "People Ops/HR Manager")

    def test_people_ops_rejects_hr_coordinator(self):
        self.assertIsNone(persona_people_ops.classify_title("HR Coordinator"))

    def test_recruiter_ta_head_of_talent_is_top_tier(self):
        result = persona_recruiter_ta.classify_title("Head of Talent")
        self.assertEqual(result["rank"], 1)

    def test_recruiter_ta_bare_recruiter_tier(self):
        # Unlike the other two personas, a bare individual-contributor
        # title is a real, valid target here -- not just a fallback.
        result = persona_recruiter_ta.classify_title("Recruiter")
        self.assertIsNotNone(result)
        self.assertEqual(result["tier"], "Recruiter / Talent Partner")

    def test_recruiter_ta_rejects_recruiting_coordinator(self):
        self.assertIsNone(persona_recruiter_ta.classify_title("Recruiting Coordinator"))


if __name__ == "__main__":
    unittest.main()
