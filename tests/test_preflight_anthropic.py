"""Regression suite for the Phase 17 Anthropic preflight (src/preflight_anthropic.py).

The point of the preflight is to turn a silent billing failure into a loud one, so what
matters here is that each failure mode maps to its own exit code -- run-cron.sh aborts the
whole run on any nonzero, and reports which kind it was. A credit failure misclassified as
a generic one still aborts, but tells Rachel to go debug the network instead of the bill.

Run with: python -m unittest tests.test_preflight_anthropic -v
No network, no API keys -- the Anthropic client is mocked throughout.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import preflight_anthropic as pf
from src.providers import MissingKey


def _text_block(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _client_returning(blocks):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=blocks)
    return client


def _client_raising(exc):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


class TestIsCreditFailure(unittest.TestCase):
    def test_real_anthropic_credit_wording_is_recognized(self):
        # The current live wording, which is what this actually has to catch.
        self.assertTrue(pf.is_credit_failure(
            "BadRequestError: Error code: 400 - {'error': {'message': 'Your credit balance is "
            "too low to access the Anthropic API. Please go to Plans & Billing to upgrade or "
            "purchase credits.'}}"))

    def test_other_billing_phrasings_are_recognized(self):
        for detail in ["insufficient credit", "RateLimitError: quota exceeded",
                       "payment required", "monthly spending limit reached",
                       "organization usage limit"]:
            with self.subTest(detail=detail):
                self.assertTrue(pf.is_credit_failure(detail))

    def test_transient_and_config_failures_are_not_credit_failures(self):
        for detail in ["APIConnectionError: Connection error.",
                       "NotFoundError: model: claude-does-not-exist",
                       "AuthenticationError: invalid x-api-key",
                       "InternalServerError: Overloaded"]:
            with self.subTest(detail=detail):
                self.assertFalse(pf.is_credit_failure(detail))

    def test_empty_detail_is_not_a_credit_failure(self):
        self.assertFalse(pf.is_credit_failure(""))
        self.assertFalse(pf.is_credit_failure(None))


class TestCheck(unittest.TestCase):
    def setUp(self):
        # check() records its Anthropic usage via providers.record_anthropic_call (Phase 19), which
        # appends to outputs/spend_YYYYMMDD.log by default. Redirected to a temp file here: without
        # this, these tests wrote six lines of MagicMock-derived garbage (in=1 out=1 searches=1,
        # since int(MagicMock()) is 1) into the REAL spend log, inventing ~$0.06 of cost on a day
        # whose log is meant to be a record of actual spend. Caught 2026-10-02.
        self._tmpdir = tempfile.mkdtemp()
        self._patcher = patch.object(
            pf.prov, "spend_log_path",
            return_value=Path(self._tmpdir) / "spend_test.log")
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_successful_call_returns_zero(self):
        with patch.object(pf.prov, "_get_anthropic_client",
                          return_value=_client_returning([_text_block("ok")])):
            code, detail = pf.check()
        self.assertEqual(code, 0)
        self.assertIn("ok", detail)

    def test_call_is_cheap(self):
        # A preflight that burned real tokens every weekday would be its own problem.
        client = _client_returning([_text_block("ok")])
        with patch.object(pf.prov, "_get_anthropic_client", return_value=client):
            pf.check()
        _, kwargs = client.messages.create.call_args
        self.assertEqual(kwargs["max_tokens"], pf.PREFLIGHT_MAX_TOKENS)
        self.assertLessEqual(kwargs["max_tokens"], 10)

    def test_missing_key_returns_two(self):
        with patch.object(pf.prov, "_get_anthropic_client",
                          side_effect=MissingKey("ANTHROPIC_API_KEY missing from .env")):
            code, detail = pf.check()
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", detail)

    def test_credit_failure_returns_three(self):
        exc = Exception("Error code: 400 - Your credit balance is too low to access the "
                        "Anthropic API.")
        with patch.object(pf.prov, "_get_anthropic_client", return_value=_client_raising(exc)):
            code, detail = pf.check()
        self.assertEqual(code, 3)
        self.assertIn("credit balance", detail)

    def test_other_api_failure_returns_four(self):
        with patch.object(pf.prov, "_get_anthropic_client",
                          return_value=_client_raising(Exception("Connection error."))):
            code, _ = pf.check()
        self.assertEqual(code, 4)

    def test_response_with_no_text_content_returns_four(self):
        with patch.object(pf.prov, "_get_anthropic_client", return_value=_client_returning([])):
            code, detail = pf.check()
        self.assertEqual(code, 4)
        self.assertIn("no text content", detail)

    def test_unexpected_sdk_exception_type_still_maps_to_four(self):
        # The SDK's exception hierarchy varies by version, so check() catches Exception
        # broadly rather than naming classes. An exception type this code has never heard
        # of must still come back as a code, not escape and crash the preflight itself.
        # (Deliberately only Exception, not BaseException -- a Ctrl-C must still interrupt.)
        class SomeFutureSDKError(Exception):
            pass

        with patch.object(pf.prov, "_get_anthropic_client",
                          return_value=_client_raising(SomeFutureSDKError("unrecognized"))):
            code, detail = pf.check()
        self.assertEqual(code, 4)
        self.assertIn("SomeFutureSDKError", detail)


class TestMainExitCodes(unittest.TestCase):
    def test_main_returns_the_check_code(self):
        for code in (0, 2, 3, 4):
            with self.subTest(code=code):
                with patch.object(pf, "check", return_value=(code, "detail")):
                    self.assertEqual(pf.main(), code)


if __name__ == "__main__":
    unittest.main()
