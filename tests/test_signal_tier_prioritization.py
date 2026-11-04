"""Regression suite for Signal Tier write-back + daily-cap prioritization (built
2026-09-17 -- see claude/nycbound-gtm-signal-tier-prioritization-scope.md, Sales &
Marketing claude.ai Project).

Covers: mapping company_research.py's descriptive tier string down to Fibery's
enum name, the sort key that orders the active company list (Tier 1, then
never-classified, then Tier 2, then Tier 3, staleness-broken within a tier), and
that the sort never lets an inactive company back into the list.

Run with: python -m unittest tests.test_signal_tier_prioritization -v
No network, no API keys, no Fibery calls required.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import _company_priority_key, _map_signal_tier, read_company_list_from_fibery
from src import fibery_client as fc


class TestMapSignalTier(unittest.TestCase):
    def test_tier_1_prefix(self):
        self.assertEqual(_map_signal_tier("Tier 1 - explicit relocation/visa language"), "Tier 1")

    def test_tier_2_prefix(self):
        self.assertEqual(
            _map_signal_tier("Tier 2 - NYC presence required, no relocation support "
                              "(pitch as 'no existing solution')"),
            "Tier 2")

    def test_tier_3_prefix(self):
        self.assertEqual(
            _map_signal_tier("Tier 3 - NYC roles open, no relocation or onsite language found"),
            "Tier 3")

    def test_unexpected_string_returns_none(self):
        # "no signal" is company_research.py's blank/no-jobs-fetched default (line ~942) --
        # must never get written as a fake tier.
        self.assertIsNone(_map_signal_tier("no signal"))
        self.assertIsNone(_map_signal_tier(""))


class TestSignalTierFiberyMapping(unittest.TestCase):
    def test_all_three_tiers_present_no_noise(self):
        self.assertEqual(set(fc.SIGNAL_TIER.keys()), {"Tier 1", "Tier 2", "Tier 3"})
        self.assertNotIn("Noise", fc.SIGNAL_TIER)

    def test_every_map_signal_tier_output_has_a_fibery_id(self):
        for key in ("Tier 1", "Tier 2", "Tier 3"):
            self.assertIn(key, fc.SIGNAL_TIER)
            self.assertTrue(fc.SIGNAL_TIER[key])


class TestCompanyPriorityKey(unittest.TestCase):
    def test_tier_1_sorts_before_unclassified(self):
        tier1 = _company_priority_key({"signal_tier": "Tier 1"})
        unclassified = _company_priority_key({"signal_tier": None})
        self.assertLess(tier1, unclassified)

    def test_unclassified_sorts_before_tier_2(self):
        unclassified = _company_priority_key({"signal_tier": None})
        tier2 = _company_priority_key({"signal_tier": "Tier 2"})
        self.assertLess(unclassified, tier2)

    def test_tier_2_sorts_before_tier_3(self):
        tier2 = _company_priority_key({"signal_tier": "Tier 2"})
        tier3 = _company_priority_key({"signal_tier": "Tier 3"})
        self.assertLess(tier2, tier3)

    def test_blank_string_treated_same_as_none(self):
        blank = _company_priority_key({"signal_tier": ""})
        none_val = _company_priority_key({"signal_tier": None})
        self.assertEqual(blank[0], none_val[0])

    def test_unexpected_tier_value_falls_in_with_unclassified(self):
        # A hand-edited Fibery value, or a future tier name -- must not crash the sort.
        weird = _company_priority_key({"signal_tier": "Tier 7"})
        unclassified = _company_priority_key({"signal_tier": None})
        self.assertEqual(weird[0], unclassified[0])

    def test_never_checked_sorts_before_stale_checked_within_same_tier(self):
        never_checked = _company_priority_key({"signal_tier": "Tier 1", "last_checked_date": None})
        checked_recently = _company_priority_key(
            {"signal_tier": "Tier 1", "last_checked_date": "2026-09-15"})
        self.assertLess(never_checked, checked_recently)

    def test_oldest_checked_sorts_before_newest_checked_within_same_tier(self):
        older = _company_priority_key({"signal_tier": "Tier 1", "last_checked_date": "2026-08-01"})
        newer = _company_priority_key({"signal_tier": "Tier 1", "last_checked_date": "2026-09-15"})
        self.assertLess(older, newer)


class TestReadCompanyListFromFiberySorting(unittest.TestCase):
    def _active(self, name, domain, signal_tier=None, last_checked=None):
        return domain, {
            "name": name,
            "website": "https://" + domain,
            "research_status": "Qualified",
            "promotion_status": "Not Promoted",
            "signal_tier": signal_tier,
            "last_checked_date": last_checked,
        }

    def test_full_ordering_across_tiers_and_unclassified(self):
        company_map = dict([
            self._active("Tier3Co", "tier3.com", signal_tier="Tier 3"),
            self._active("Tier1Co", "tier1.com", signal_tier="Tier 1"),
            self._active("UnclassifiedCo", "unclassified.com", signal_tier=None),
            self._active("Tier2Co", "tier2.com", signal_tier="Tier 2"),
        ])
        out = read_company_list_from_fibery(company_map)
        self.assertEqual([c["company"] for c in out],
                         ["Tier1Co", "UnclassifiedCo", "Tier2Co", "Tier3Co"])

    def test_staleness_breaks_ties_within_a_tier(self):
        company_map = dict([
            self._active("CheckedRecently", "recent.com", signal_tier="Tier 1",
                         last_checked="2026-09-16"),
            self._active("NeverChecked", "never.com", signal_tier="Tier 1", last_checked=None),
            self._active("CheckedLongAgo", "old.com", signal_tier="Tier 1",
                         last_checked="2026-08-01"),
        ])
        out = read_company_list_from_fibery(company_map)
        self.assertEqual([c["company"] for c in out],
                         ["NeverChecked", "CheckedLongAgo", "CheckedRecently"])

    def test_disqualified_company_excluded_regardless_of_tier(self):
        company_map = dict([
            self._active("GoodCo", "good.com", signal_tier="Tier 3"),
        ])
        domain, disqualified = self._active("DisqualifiedTier1", "dq.com", signal_tier="Tier 1")
        disqualified["research_status"] = "Disqualified"
        company_map[domain] = disqualified

        out = read_company_list_from_fibery(company_map)
        self.assertEqual([c["company"] for c in out], ["GoodCo"])

    def test_promoted_company_excluded_regardless_of_tier(self):
        company_map = dict([
            self._active("GoodCo", "good.com", signal_tier="Tier 3"),
        ])
        domain, promoted = self._active("PromotedTier1", "promoted.com", signal_tier="Tier 1")
        promoted["promotion_status"] = "Promoted to CRM"
        company_map[domain] = promoted

        out = read_company_list_from_fibery(company_map)
        self.assertEqual([c["company"] for c in out], ["GoodCo"])

    def test_empty_company_map_returns_empty_list(self):
        self.assertEqual(read_company_list_from_fibery({}), [])


if __name__ == "__main__":
    unittest.main()
