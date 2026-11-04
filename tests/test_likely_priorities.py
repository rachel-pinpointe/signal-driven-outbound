"""Regression suite for the "Likely Priorities" guardrailed-inference feature (built
2026-09-17 -- see claude/nycbound-gtm-likely-priorities-scope.md, Sales & Marketing
claude.ai Project).

This is deliberately the opposite design point from research_stated_goal(): that
function refuses to guess and requires a verified citation; this one is meant to
guess, but only within a tight structural/hedging guardrail. These tests lock in
that guardrail -- a response missing a required section, or containing no hedged
language at all, is rejected and never shipped -- and confirm the feature is a
no-op for personas that don't have profile grounding yet.

Run with: python -m unittest tests.test_likely_priorities -v
No network, no API keys required -- the LLM call itself is mocked throughout.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.company_research import (
    _summarize_signals_for_likely_priorities,
    _validate_likely_priorities,
    research_likely_priorities,
)
from src.providers import MissingKey

WELL_FORMED_RESPONSE = (
    "GOALS & PRIORITIES\n"
    "This contact likely wants to keep hiring velocity high without letting candidate "
    "experience slip. They may also be trying to keep onboarding smooth as hires relocate.\n\n"
    "CHALLENGES, PRIORITIES, AND RESPONSIBILITIES\n"
    "On a lean team, this contact appears to be stretched across recruiting support and "
    "onboarding logistics. Relocation friction could be an unplanned drain on their time.\n\n"
    "KPIS TO PRIORITIZE\n"
    "Offer acceptance rate. Time-to-productivity. Early retention."
)

MISSING_SECTION_RESPONSE = (
    "GOALS & PRIORITIES\n"
    "This contact likely wants to grow the team quickly.\n\n"
    "KPIS TO PRIORITIZE\n"
    "Offer acceptance rate."
)

NO_HEDGE_RESPONSE = (
    "GOALS & PRIORITIES\n"
    "This contact wants to grow the team quickly. They are the sole owner of onboarding.\n\n"
    "CHALLENGES, PRIORITIES, AND RESPONSIBILITIES\n"
    "This contact is overloaded and understaffed.\n\n"
    "KPIS TO PRIORITIZE\n"
    "Offer acceptance rate. Time-to-productivity."
)

SAMPLE_CONTACT = {"contact_name": "Jane Doe", "title": "Head of People", "email": "jane@acme.com"}
SAMPLE_JOBS = {"qualification": "QUALIFIED", "nyc_roles": ["Senior Recruiter", "HRBP"],
               "role_band_used": "Director", "band_base": 180000}
SAMPLE_STATED_GOAL_FOUND = {"found": True, "goal": "Expanding NYC team after a Series C.",
                            "source_type": "funding_news"}
SAMPLE_STATED_GOAL_EMPTY = {"found": False}


class TestValidateLikelyPriorities(unittest.TestCase):
    def test_well_formed_response_passes(self):
        self.assertTrue(_validate_likely_priorities(WELL_FORMED_RESPONSE))

    def test_missing_section_fails(self):
        self.assertFalse(_validate_likely_priorities(MISSING_SECTION_RESPONSE))

    def test_no_hedge_language_fails(self):
        # Rachel's core requirement: every response must actually use hedged language
        # somewhere, not just the right section headers.
        self.assertFalse(_validate_likely_priorities(NO_HEDGE_RESPONSE))

    def test_empty_text_fails(self):
        self.assertFalse(_validate_likely_priorities(""))
        self.assertFalse(_validate_likely_priorities(None))

    def test_lowercase_section_headers_still_pass(self):
        lower = WELL_FORMED_RESPONSE.replace("GOALS & PRIORITIES", "goals & priorities") \
            .replace("CHALLENGES, PRIORITIES, AND RESPONSIBILITIES",
                     "challenges, priorities, and responsibilities") \
            .replace("KPIS TO PRIORITIZE", "kpis to prioritize")
        self.assertTrue(_validate_likely_priorities(lower))


class TestSummarizeSignals(unittest.TestCase):
    def test_thin_signal_falls_back_to_role_level_note(self):
        summary = _summarize_signals_for_likely_priorities("Acme", {}, {})
        self.assertIn("role-level research only", summary)

    def test_real_signals_are_surfaced(self):
        summary = _summarize_signals_for_likely_priorities("Acme", SAMPLE_JOBS,
                                                            SAMPLE_STATED_GOAL_FOUND)
        self.assertIn("QUALIFIED", summary)
        self.assertIn("Senior Recruiter", summary)
        self.assertIn("Series C", summary)

    def test_unfound_stated_goal_is_not_surfaced_as_a_finding(self):
        summary = _summarize_signals_for_likely_priorities("Acme", SAMPLE_JOBS,
                                                            SAMPLE_STATED_GOAL_EMPTY)
        self.assertNotIn("Stated goal", summary)


class TestResearchLikelyPriorities(unittest.TestCase):
    def test_unknown_persona_returns_none_without_calling_llm(self):
        with patch("src.company_research._llm_extract") as mock_extract:
            result = research_likely_priorities(
                "not_a_real_persona", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS,
                SAMPLE_STATED_GOAL_FOUND)
        self.assertIsNone(result)
        mock_extract.assert_not_called()

    def test_well_formed_llm_response_is_returned(self):
        with patch("src.company_research._llm_extract", return_value=WELL_FORMED_RESPONSE):
            result = research_likely_priorities(
                "hr_people_ops", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS,
                SAMPLE_STATED_GOAL_FOUND)
        self.assertEqual(result, WELL_FORMED_RESPONSE)

    def test_guardrail_rejects_missing_section_and_returns_none(self):
        with patch("src.company_research._llm_extract", return_value=MISSING_SECTION_RESPONSE):
            result = research_likely_priorities(
                "cfo", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS, SAMPLE_STATED_GOAL_EMPTY)
        self.assertIsNone(result)

    def test_guardrail_rejects_unhedged_response_and_returns_none(self):
        with patch("src.company_research._llm_extract", return_value=NO_HEDGE_RESPONSE):
            result = research_likely_priorities(
                "recruiter_ta", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS, SAMPLE_STATED_GOAL_EMPTY)
        self.assertIsNone(result)

    def test_missing_api_key_returns_none_not_an_exception(self):
        with patch("src.company_research._llm_extract", side_effect=MissingKey("no key")):
            result = research_likely_priorities(
                "cfo", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS, SAMPLE_STATED_GOAL_EMPTY)
        self.assertIsNone(result)

    def test_prompt_includes_persona_profile_and_real_signals(self):
        captured = {}

        def fake_extract(prompt, ledger=None, max_tokens=500, call_site=None):
            captured["prompt"] = prompt
            return WELL_FORMED_RESPONSE

        with patch("src.company_research._llm_extract", side_effect=fake_extract):
            research_likely_priorities(
                "cfo", SAMPLE_CONTACT, "Acme", SAMPLE_JOBS, SAMPLE_STATED_GOAL_FOUND)
        self.assertIn("Jane Doe", captured["prompt"])
        self.assertIn("Head of People", captured["prompt"])
        self.assertIn("total people cost", captured["prompt"])  # from the CFO persona context
        self.assertIn("Series C", captured["prompt"])  # from the stated-goal signal


if __name__ == "__main__":
    unittest.main()
