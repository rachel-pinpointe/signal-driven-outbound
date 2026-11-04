"""Regression test for the query() pagination fix (2026-09-18 -- see the Paradigm import
post-mortem in claude/nycbound-gtm-systemization-tracker.md, Sales & Marketing claude.ai Project).

Before this fix, query() always sent a single q/limit=1000 request with no offset loop. Once
GTM Pipeline/Companies passed ~1000 real rows (during the full Paradigm company-list import),
load_company_map() silently saw only a partial view of Fibery's actual state, so its
domain-based dedupe check couldn't see every already-created company -- paradigm_import.py went
on to create ~27 duplicate Company records before this was caught, diagnosed, and cleaned up by
hand. query(limit=None) (the default) must now paginate via q/offset until a page comes back
shorter than the page size, so every caller that wants "the whole table" (load_company_map, the
CRM/contact loaders, promote_to_crm.py) actually gets it regardless of table size.

Mocks fibery_client.call() directly -- no network, no real Fibery token required.

Run with: python -m unittest tests.test_fibery_query_pagination -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fibery_client as fc


def _ok(rows):
    return 200, [{"success": True, "result": rows}]


class TestQueryPagination(unittest.TestCase):
    def test_single_short_page_makes_one_call(self):
        with patch.object(fc, "call", return_value=_ok([{"id": 1}, {"id": 2}])) as mock_call:
            rows = fc.query("tok", "Some/Db", {"Name": "Some/Name"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(mock_call.call_count, 1)

    def test_exactly_one_full_page_still_makes_a_second_call_to_confirm_end(self):
        # A page exactly == _QUERY_PAGE_SIZE is ambiguous (could be the whole table, could be
        # the first slice of a bigger one) -- query() must always fetch the next page to find out
        # rather than assuming a full page means "done".
        full_page = [{"id": i} for i in range(fc._QUERY_PAGE_SIZE)]
        with patch.object(fc, "call", side_effect=[_ok(full_page), _ok([])]) as mock_call:
            rows = fc.query("tok", "Some/Db", {"Name": "Some/Name"})
        self.assertEqual(len(rows), fc._QUERY_PAGE_SIZE)
        self.assertEqual(mock_call.call_count, 2)

    def test_multiple_pages_are_concatenated_in_order(self):
        page1 = [{"id": i} for i in range(fc._QUERY_PAGE_SIZE)]
        page2 = [{"id": i} for i in range(fc._QUERY_PAGE_SIZE, fc._QUERY_PAGE_SIZE + 562)]
        with patch.object(fc, "call", side_effect=[_ok(page1), _ok(page2)]) as mock_call:
            rows = fc.query("tok", "Some/Db", {"Name": "Some/Name"})
        self.assertEqual(len(rows), fc._QUERY_PAGE_SIZE + 562)
        self.assertEqual([r["id"] for r in rows], list(range(fc._QUERY_PAGE_SIZE + 562)))
        self.assertEqual(mock_call.call_count, 2)

    def test_offset_advances_by_page_size_each_call(self):
        full_page = [{"id": i} for i in range(fc._QUERY_PAGE_SIZE)]
        with patch.object(fc, "call", side_effect=[_ok(full_page), _ok([{"id": "last"}])]) as mock_call:
            fc.query("tok", "Some/Db", {"Name": "Some/Name"})
        offsets = [call_args[0][1][0]["args"]["query"]["q/offset"] for call_args in mock_call.call_args_list]
        self.assertEqual(offsets, [0, fc._QUERY_PAGE_SIZE])

    def test_explicit_limit_bypasses_pagination_single_call(self):
        with patch.object(fc, "call", return_value=_ok([{"id": 1}])) as mock_call:
            rows = fc.query("tok", "Some/Db", {"Name": "Some/Name"}, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(mock_call.call_count, 1)
        sent_query = mock_call.call_args[0][1][0]["args"]["query"]
        self.assertEqual(sent_query["q/limit"], 1)
        self.assertNotIn("q/offset", sent_query)

    def test_where_clause_is_preserved_across_pages(self):
        full_page = [{"id": i} for i in range(fc._QUERY_PAGE_SIZE)]
        where = ["=", ["some/field"], "value"]
        with patch.object(fc, "call", side_effect=[_ok(full_page), _ok([])]) as mock_call:
            fc.query("tok", "Some/Db", {"Name": "Some/Name"}, where=where)
        for call_args in mock_call.call_args_list:
            self.assertEqual(call_args[0][1][0]["args"]["query"]["q/where"], where)

    def test_failed_page_raises_runtime_error(self):
        with patch.object(fc, "call", return_value=(500, [{"success": False}])):
            with self.assertRaises(RuntimeError):
                fc.query("tok", "Some/Db", {"Name": "Some/Name"})


if __name__ == "__main__":
    unittest.main()
