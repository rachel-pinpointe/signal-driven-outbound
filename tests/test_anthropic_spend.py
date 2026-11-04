"""Regression suite for Phase 19 step 7: Anthropic usage and cost tracking.

Every Anthropic call in the pipeline is tagged by call site and accumulated on the CreditLedger,
with a flushed per-call line in outputs/spend_YYYYMMDD.log. The point is answering "where did the
money go", which a single total cannot.

Two properties matter more than the arithmetic and are pinned hardest below:

  - This is observability and must never change run behavior. record_anthropic_call() cannot
    raise, whatever shape of response it is handed, and there is no ceiling anywhere.
  - The rates are editable constants, not derived values. A test asserts the table is reachable
    and that an unknown model falls back to a non-zero rate -- a zero would read as "this is free"
    rather than "nobody added the rates".

Run with: python -m unittest tests.test_anthropic_spend -v
No network, no API keys. Responses are plain stubs; the log goes to a temp file.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import providers as prov


class _Usage:
    """Attribute-shaped usage block, as the SDK returns it."""

    def __init__(self, input_tokens=0, output_tokens=0, web_search_requests=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        if web_search_requests is not None:
            self.server_tool_use = type("STU", (), {"web_search_requests": web_search_requests})()


class _Block:
    """A text content block, as the SDK returns inside response.content."""

    def __init__(self, text="ok"):
        self.type = "text"
        self.text = text


class _Response:
    """Shaped like a real SDK response: `content` is always present, since _llm_extract and
    _llm_web_search both read it after usage is recorded. `usage` is omitted when None so the
    no-usage-block case stays testable."""

    def __init__(self, usage=None, content=None):
        if usage is not None:
            self.usage = usage
        self.content = [_Block()] if content is None else content


class TestCostEstimation(unittest.TestCase):
    def test_a_known_model_uses_its_table_rates(self):
        # 1M in / 1M out on Sonnet at 3.00/15.00 per Mtok.
        cost = prov.anthropic_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 18.00, places=6)

    def test_partial_millions_scale_linearly(self):
        cost = prov.anthropic_cost_usd("claude-sonnet-4-6", 1_000, 500)
        self.assertAlmostEqual(cost, (1_000 / 1e6) * 3.00 + (500 / 1e6) * 15.00, places=9)

    def test_web_searches_are_charged_per_request(self):
        cost = prov.anthropic_cost_usd("claude-sonnet-4-6", 0, 0, web_searches=3)
        self.assertAlmostEqual(cost, 3 * prov.ANTHROPIC_WEB_SEARCH_PER_REQUEST, places=9)

    def test_an_unknown_model_falls_back_to_a_nonzero_rate(self):
        cost = prov.anthropic_cost_usd("some-unreleased-model", 1_000_000, 0)
        self.assertGreater(cost, 0.0,
                           "an unknown model must not estimate as free -- that reads as 'no cost' "
                           "rather than 'rates not added'")
        self.assertAlmostEqual(cost, prov.ANTHROPIC_PRICING_FALLBACK["input_per_mtok"], places=6)

    def test_a_blank_or_none_model_still_estimates(self):
        self.assertGreater(prov.anthropic_cost_usd(None, 1_000_000, 0), 0.0)
        self.assertGreater(prov.anthropic_cost_usd("", 1_000_000, 0), 0.0)

    def test_zero_usage_costs_nothing(self):
        self.assertEqual(prov.anthropic_cost_usd("claude-sonnet-4-6", 0, 0, 0), 0.0)

    def test_the_rates_table_is_editable_plain_values(self):
        # Guards the shape the comment promises: a dict of plain numbers, not computed objects.
        for model, rates in prov.ANTHROPIC_PRICING.items():
            with self.subTest(model=model):
                self.assertIsInstance(rates["input_per_mtok"], float)
                self.assertIsInstance(rates["output_per_mtok"], float)
        self.assertIsInstance(prov.ANTHROPIC_WEB_SEARCH_PER_REQUEST, float)

    def test_the_five_required_call_sites_are_declared(self):
        self.assertEqual(set(prov.ANTHROPIC_CALL_SITES),
                         {"research", "extract", "likely-priorities", "ghost-email", "preflight"})


class TestExtractUsage(unittest.TestCase):
    def test_attribute_shaped_usage(self):
        self.assertEqual(prov._extract_usage(_Response(_Usage(120, 30))), (120, 30, 0))

    def test_web_search_requests_are_read(self):
        self.assertEqual(
            prov._extract_usage(_Response(_Usage(120, 30, web_search_requests=2))), (120, 30, 2))

    def test_dict_shaped_usage(self):
        resp = {"usage": {"input_tokens": 7, "output_tokens": 9,
                          "server_tool_use": {"web_search_requests": 1}}}
        self.assertEqual(prov._extract_usage(resp), (7, 9, 1))

    def test_a_response_with_no_usage_block_reads_as_zero(self):
        self.assertEqual(prov._extract_usage(_Response()), (0, 0, 0))
        self.assertEqual(prov._extract_usage(object()), (0, 0, 0))

    def test_missing_fields_read_as_zero_rather_than_raising(self):
        self.assertEqual(prov._extract_usage(_Response(_Usage())), (0, 0, 0))

    def test_none_valued_fields_read_as_zero(self):
        resp = {"usage": {"input_tokens": None, "output_tokens": None}}
        self.assertEqual(prov._extract_usage(resp), (0, 0, 0))


class TestLedgerAccumulation(unittest.TestCase):
    def setUp(self):
        self.ledger = prov.CreditLedger()

    def test_a_fresh_ledger_has_no_anthropic_usage(self):
        self.assertEqual(self.ledger.anthropic, {})
        self.assertEqual(self.ledger.anthropic_calls(), 0)
        self.assertEqual(self.ledger.anthropic_total_cost(), 0.0)

    def test_calls_accumulate_within_one_site(self):
        self.ledger.charge_anthropic("ghost-email", "claude-sonnet-4-6", 1000, 200)
        self.ledger.charge_anthropic("ghost-email", "claude-sonnet-4-6", 500, 100)
        bucket = self.ledger.anthropic["ghost-email"]
        self.assertEqual(bucket["calls"], 2)
        self.assertEqual(bucket["input_tokens"], 1500)
        self.assertEqual(bucket["output_tokens"], 300)

    def test_sites_are_tracked_separately(self):
        self.ledger.charge_anthropic("research", "claude-sonnet-4-6", 100, 10, web_searches=2)
        self.ledger.charge_anthropic("likely-priorities", "claude-sonnet-4-6", 2000, 400)
        self.assertEqual(sorted(self.ledger.anthropic), ["likely-priorities", "research"])
        self.assertEqual(self.ledger.anthropic["research"]["web_searches"], 2)
        self.assertEqual(self.ledger.anthropic["likely-priorities"]["web_searches"], 0)

    def test_total_cost_sums_every_site(self):
        self.ledger.charge_anthropic("research", "claude-sonnet-4-6", 1_000_000, 0)
        self.ledger.charge_anthropic("extract", "claude-sonnet-4-6", 1_000_000, 0)
        self.assertAlmostEqual(self.ledger.anthropic_total_cost(), 6.00, places=6)

    def test_an_untagged_call_lands_in_an_unknown_bucket_rather_than_vanishing(self):
        self.ledger.charge_anthropic("", "claude-sonnet-4-6", 10, 1)
        self.assertIn("unknown", self.ledger.anthropic)


class TestSummaryAndBreakdown(unittest.TestCase):
    def setUp(self):
        self.ledger = prov.CreditLedger()

    def test_summary_omits_anthropic_when_there_were_no_calls(self):
        self.assertNotIn("Anthropic", self.ledger.summary())

    def test_summary_carries_the_anthropic_total(self):
        self.ledger.charge_anthropic("ghost-email", "claude-sonnet-4-6", 1_000_000, 0)
        summary = self.ledger.summary()
        self.assertIn("Anthropic $3.0000 est", summary)
        self.assertIn("1 call(s)", summary)

    def test_breakdown_names_every_call_site_with_its_tokens(self):
        self.ledger.charge_anthropic("research", "claude-sonnet-4-6", 400, 18, web_searches=1)
        self.ledger.charge_anthropic("ghost-email", "claude-sonnet-4-6", 5000, 800)
        lines = self.ledger.anthropic_breakdown_lines()
        joined = "\n".join(lines)
        self.assertIn("research", joined)
        self.assertIn("ghost-email", joined)
        self.assertIn("400 in / 18 out tokens", joined)
        self.assertIn("1 web search(es)", joined)

    def test_breakdown_says_so_when_there_were_no_calls(self):
        self.assertIn("no calls this run", "\n".join(self.ledger.anthropic_breakdown_lines()))

    def test_breakdown_omits_the_search_clause_when_there_were_none(self):
        self.ledger.charge_anthropic("extract", "claude-sonnet-4-6", 100, 10)
        self.assertNotIn("web search", "\n".join(self.ledger.anthropic_breakdown_lines()))


class TestRecordAnthropicCall(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "nested" / "spend_20261002.log"
        self.ledger = prov.CreditLedger()

    def _lines(self):
        return [l for l in self.log.read_text().splitlines() if l.strip()]

    def test_a_line_is_appended_per_call_with_the_call_site(self):
        prov.record_anthropic_call(self.ledger, "ghost-email", "claude-sonnet-4-6",
                                   _Response(_Usage(1000, 200)), log_path=self.log)
        prov.record_anthropic_call(self.ledger, "research", "claude-sonnet-4-6",
                                   _Response(_Usage(400, 18, web_search_requests=1)),
                                   log_path=self.log)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("ghost-email", lines[0])
        self.assertIn("in=1000", lines[0])
        self.assertIn("out=200", lines[0])
        self.assertIn("research", lines[1])
        self.assertIn("searches=1", lines[1])

    def test_the_line_is_timestamped_and_tagged_anthropic(self):
        prov.record_anthropic_call(self.ledger, "preflight", "claude-sonnet-4-6",
                                   _Response(_Usage(10, 4)), log_path=self.log)
        line = self._lines()[0]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\tanthropic\t")

    def test_the_ledger_is_updated_alongside_the_log(self):
        prov.record_anthropic_call(self.ledger, "extract", "claude-sonnet-4-6",
                                   _Response(_Usage(2000, 50)), log_path=self.log)
        self.assertEqual(self.ledger.anthropic["extract"]["input_tokens"], 2000)

    def test_a_missing_parent_directory_is_created(self):
        self.assertFalse(self.log.parent.exists())
        prov.record_anthropic_call(self.ledger, "extract", "m", _Response(_Usage(1, 1)),
                                   log_path=self.log)
        self.assertTrue(self.log.exists())

    def test_it_works_without_a_ledger(self):
        # preflight_anthropic runs standalone; the log line must still be written.
        prov.record_anthropic_call(None, "preflight", "claude-sonnet-4-6",
                                   _Response(_Usage(10, 4)), log_path=self.log)
        self.assertIn("preflight", self._lines()[0])

    def test_an_unwritable_path_does_not_raise(self):
        try:
            prov.record_anthropic_call(self.ledger, "extract", "m", _Response(_Usage(1, 1)),
                                       log_path=Path("/dev/null/nope/spend.log"))
        except Exception as exc:  # noqa: BLE001 -- the assertion IS "nothing escapes"
            self.fail("cost tracking raised instead of degrading: {!r}".format(exc))

    def test_a_garbage_response_does_not_raise(self):
        # Observability must never be the reason a working run fails.
        for bad in (None, object(), "not a response", 42, {"usage": "nonsense"}):
            with self.subTest(bad=bad):
                try:
                    prov.record_anthropic_call(self.ledger, "extract", "m", bad,
                                               log_path=self.log)
                except Exception as exc:  # noqa: BLE001
                    self.fail("raised on {!r}: {!r}".format(bad, exc))


class TestNoSpendingCeiling(unittest.TestCase):
    def test_there_is_no_anthropic_budget_or_exhausted_check(self):
        # The brief is explicit: tracking only, no ceiling. A budget attribute or an
        # *_exhausted() predicate would be a behavior change.
        ledger = prov.CreditLedger()
        for name in ("anthropic_budget", "anthropic_budget_exhausted", "anthropic_cap"):
            self.assertFalse(hasattr(ledger, name),
                             "{} exists -- step 7 must not add a spending ceiling".format(name))

    def test_recording_a_huge_cost_changes_nothing_about_the_ledger_s_verdicts(self):
        ledger = prov.CreditLedger(qe_budget=25)
        ledger.charge_anthropic("ghost-email", "claude-opus-5", 50_000_000, 10_000_000)
        self.assertFalse(ledger.qe_budget_exhausted())
        self.assertFalse(ledger.apollo_budget_exhausted())


class TestCallSiteWiring(unittest.TestCase):
    """The tags actually reach the ledger from each real call path."""

    def _resp(self):
        return _Response(_Usage(400, 18))

    def test_llm_extract_tags_its_call_site(self):
        ledger = prov.CreditLedger()
        fake_client = MagicMock()
        fake_client.messages.create.return_value = self._resp()
        with patch.object(prov, "_get_anthropic_client", return_value=fake_client), \
             patch.object(prov, "spend_log_path",
                          return_value=Path(tempfile.mkdtemp()) / "spend.log"):
            prov._llm_extract("p", ledger=ledger, call_site="likely-priorities")
        self.assertIn("likely-priorities", ledger.anthropic)

    def test_llm_web_search_tags_its_call_site(self):
        ledger = prov.CreditLedger()
        fake_client = MagicMock()
        fake_client.messages.create.return_value = self._resp()
        with patch.object(prov, "_get_anthropic_client", return_value=fake_client), \
             patch.object(prov, "spend_log_path",
                          return_value=Path(tempfile.mkdtemp()) / "spend.log"):
            prov._llm_web_search("p", ledger=ledger, call_site="research")
        self.assertIn("research", ledger.anthropic)

    def test_a_failed_call_records_no_usage(self):
        ledger = prov.CreditLedger()
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = Exception("boom")
        with patch.object(prov, "_get_anthropic_client", return_value=fake_client):
            prov._llm_extract("p", ledger=ledger, call_site="extract")
        self.assertEqual(ledger.anthropic, {})


if __name__ == "__main__":
    unittest.main()
