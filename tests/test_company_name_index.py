"""Regression test for load_company_name_index() (2026-09-18 -- see the Paradigm import
post-mortem in claude/nycbound-gtm-systemization-tracker.md, Sales & Marketing claude.ai Project).

load_company_map() is keyed by normalized Domain and silently drops any company with a blank
Domain. paradigm_import.py's needs_review name-collision safety net was built on top of that
domain-keyed map, so a blank-domain company already in Fibery was invisible to it -- a repeat
import (or even a fresh --dry-run) would report it as "would create" instead of flagging the name
collision, and a real run would create a second blank-domain record for the same company.
load_company_name_index() fixes this by indexing every company by name regardless of Domain.

Mocks fibery_client.query() directly -- no network, no real Fibery token required.

Run with: python -m unittest tests.test_company_name_index -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fibery_client as fc


class TestLoadCompanyNameIndex(unittest.TestCase):
    def test_blank_domain_company_is_indexed_by_name(self):
        rows = [{"id": "abc-1", "Name": "Covenant", "Domain": None}]
        with patch.object(fc, "query", return_value=rows):
            idx = fc.load_company_name_index("tok")
        self.assertIn("covenant", idx)
        self.assertEqual(idx["covenant"]["id"], "abc-1")
        self.assertIsNone(idx["covenant"]["domain"])

    def test_domain_having_company_is_also_indexed_by_name(self):
        rows = [{"id": "abc-2", "Name": "Norm AI", "Domain": "norm.ai"}]
        with patch.object(fc, "query", return_value=rows):
            idx = fc.load_company_name_index("tok")
        self.assertIn("norm ai", idx)
        self.assertEqual(idx["norm ai"]["domain"], "norm.ai")

    def test_name_matching_is_case_and_whitespace_insensitive_on_lookup_key(self):
        rows = [{"id": "abc-3", "Name": "  Covenant  ", "Domain": None}]
        with patch.object(fc, "query", return_value=rows):
            idx = fc.load_company_name_index("tok")
        self.assertIn("covenant", idx)

    def test_blank_name_rows_are_skipped(self):
        rows = [{"id": "abc-4", "Name": "", "Domain": None},
                {"id": "abc-5", "Name": None, "Domain": "x.com"}]
        with patch.object(fc, "query", return_value=rows):
            idx = fc.load_company_name_index("tok")
        self.assertEqual(idx, {})

    def test_name_collision_keeps_the_first_one_seen(self):
        rows = [{"id": "first", "Name": "Wint", "Domain": None},
                {"id": "second", "Name": "Wint", "Domain": "wint.ai"}]
        with patch.object(fc, "query", return_value=rows):
            idx = fc.load_company_name_index("tok")
        self.assertEqual(idx["wint"]["id"], "first")


if __name__ == "__main__":
    unittest.main()
