"""Regression suite for Phase 20 step 8: failed-enrollment diagnostics and the deliverability guard.

Both halves come straight out of the 2026-10-02 run, where four enrollments failed
(avery@example.io, casey@example.net, dmercer@acme.com, blake.morrow@example.org) and nobody could
say why. Two separate problems:

  (a) Apollo returned HTTP 200 and then silently declined to add the contact. The response body was
      discarded, so the only available explanation was a guess. The body now travels out with the
      failure, into both the result detail and outputs/enroll_failures_YYYYMMDD.log.

  (b) All four carried email_status "verified" while Apollo's own email_true_status disagreed. Two
      were "Potentially Invalid" (avery, blake) and two "User Managed" (casey, dmercer). Only
      "Potentially Invalid" is skipped: "User Managed" means a human set the address deliberately,
      which is a reason to trust it rather than doubt it, and both were real, working addresses (fictionalized here).

Run with: python -m unittest tests.test_enroll_failures -v
No network, no API keys; Apollo responses are stubs and logs go to temp files.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae

TODAY = "2026-10-02"


# --- the four real records, as /contacts/search returned them on 2026-10-02 ----------------------

AVERY = {"id": "000000000000000000000a01", "email": "avery@example.io",
        "email_status": "verified", "email_true_status": "Potentially Invalid",
        "source": "api", "original_source": "api", "email_source": None,
        "source_display_name": "Created from API", "last_activity_date": None,
        "emailer_campaign_ids": [], "contact_campaign_statuses": [],
        "email_domain_catchall": False, "email_domain_catchall_verdict": "allow"}

BLAKE = dict(AVERY, id="000000000000000000000a02", email="blake.morrow@example.org",
              email_domain_catchall=True, email_domain_catchall_verdict="warn")

CASEY = {"id": "000000000000000000000a03", "email": "casey@example.net",
         "email_status": "verified", "email_true_status": "User Managed",
         "source": "csv_import", "original_source": "csv_import", "email_source": None,
         "source_display_name": "Uploaded from CSV",
         # Activity is well outside the 60-day window, so only the true-status guard is in play.
         "last_activity_date": "2026-02-24T16:39:34.000+00:00",
         "emailer_campaign_ids": ["6994a3270fea01001122e539"], "contact_campaign_statuses": []}

DMERCER = dict(CASEY, id="000000000000000000000a04", email="dmercer@acme.com",
             last_activity_date="2025-11-26T20:25:53.000+00:00")


def _contact(email="avery@example.io"):
    return {"id": "fib-1", "name": "Avery Quinn", "title": "CFO", "email": email,
            "linkedin": "", "persona": "CFO/Finance", "likely_priorities": "x",
            "company_id": "co-1", "crm_contact_id": ""}


# --- (b) the deliverability guard ----------------------------------------------------------------

class TestPotentiallyInvalidIsSkipped(unittest.TestCase):
    def test_viet_is_skipped(self):
        reason = ae.apollo_precheck_skip_reason(AVERY, TODAY)
        self.assertIsNotNone(reason)
        self.assertIn("Potentially Invalid", reason)

    def test_the_reason_names_the_disagreement_with_email_status(self):
        # The whole point: email_status says verified, which is why nothing caught this before.
        reason = ae.apollo_precheck_skip_reason(AVERY, TODAY)
        self.assertIn("email_true_status", reason)
        self.assertIn("verified", reason)

    def test_no_earlier_guard_would_have_caught_him(self):
        # Establishes the gap this guard fills: no recent activity, and a prospected source.
        stripped = dict(AVERY)
        del stripped["email_true_status"]
        self.assertIsNone(ae.apollo_precheck_skip_reason(stripped, TODAY))

    def test_blake_is_skipped_too(self):
        self.assertIsNotNone(ae.apollo_precheck_skip_reason(BLAKE, TODAY))

    def test_the_match_is_case_insensitive(self):
        for value in ("potentially invalid", "POTENTIALLY INVALID", "Potentially Invalid"):
            with self.subTest(value=value):
                self.assertIsNotNone(
                    ae.apollo_precheck_skip_reason(dict(AVERY, email_true_status=value), TODAY))


class TestUserManagedIsNotSkipped(unittest.TestCase):
    def test_colin_is_not_skipped(self):
        # Explicitly required: "User Managed" means a human set the address on purpose.
        self.assertIsNone(ae.apollo_precheck_skip_reason(CASEY, TODAY))

    def test_dmercer_is_not_skipped(self):
        self.assertIsNone(ae.apollo_precheck_skip_reason(DMERCER, TODAY))

    def test_user_managed_is_absent_from_the_skip_list(self):
        self.assertNotIn("user managed", ae._SKIP_EMAIL_TRUE_STATUSES)

    def test_a_missing_true_status_is_not_a_reason_to_skip(self):
        record = dict(AVERY)
        del record["email_true_status"]
        self.assertIsNone(ae.apollo_precheck_skip_reason(record, TODAY))

    def test_a_verified_true_status_passes(self):
        self.assertIsNone(ae.apollo_precheck_skip_reason(
            dict(AVERY, email_true_status="Verified"), TODAY))


# --- (a) the response body ----------------------------------------------------------------------

class TestBodyExcerpt(unittest.TestCase):
    def test_a_dict_body_is_serialised(self):
        out = ae._apollo_body_excerpt({"error": "contact not eligible"})
        self.assertIn("contact not eligible", out)

    def test_it_is_truncated_to_500_chars(self):
        out = ae._apollo_body_excerpt({"error": "x" * 5000})
        self.assertEqual(len(out), 500)

    def test_a_raw_text_body_has_its_real_whitespace_collapsed(self):
        # A non-JSON body -- an HTML error page, or a bare string -- carries real newlines, and
        # this is where collapsing actually does its work.
        out = ae._apollo_body_excerpt(None, raw_text="line one\n\n   line two")
        self.assertEqual(out, "line one line two")

    def test_no_literal_newline_survives_so_one_failure_is_one_log_line(self):
        # A JSON body escapes its newlines (they become the two characters backslash-n), so the
        # guarantee worth asserting is that no REAL newline reaches the log line and that runs of
        # spaces collapse -- not that the escape sequence gets unwrapped.
        out = ae._apollo_body_excerpt({"error": "line one\n\n   line two"})
        self.assertNotIn("\n", out)
        self.assertNotIn("   ", out)

    def test_a_none_body_falls_back_to_the_raw_text(self):
        self.assertIn("Gateway", ae._apollo_body_excerpt(None, raw_text="502 Bad Gateway"))

    def test_an_unserialisable_body_degrades_to_str_rather_than_raising(self):
        try:
            out = ae._apollo_body_excerpt({"obj": object()})
        except Exception as exc:  # noqa: BLE001
            self.fail("body excerpting raised: {!r}".format(exc))
        self.assertTrue(out)


# Session is mocked; this only satisfies _apollo_headers() without a real .env.
@patch.dict(os.environ, {"APOLLO_API_KEY": "test-dummy-key"})
class TestAddToSequenceCarriesTheBody(unittest.TestCase):
    def _post(self, status_code, payload):
        def fake_post(url, headers=None, json=None, timeout=None):
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = payload
            resp.text = str(payload)
            return resp
        return fake_post

    def test_an_http_error_body_reaches_the_detail(self):
        with patch.object(ae, "_apollo_default_send_account_id", return_value="acct-1"), \
             patch.object(ae.prov, "_session") as session:
            session.post.side_effect = self._post(422, {"error": "contact is not eligible"})
            ok, detail = ae._apollo_add_to_sequence("c-1", "seq-1", "a@b.com")
        self.assertFalse(ok)
        self.assertIn("422", detail)
        self.assertIn("contact is not eligible", detail)

    def test_the_http_200_but_not_added_body_reaches_the_detail(self):
        # The exact 2026-10-02 failure mode.
        with patch.object(ae, "_apollo_default_send_account_id", return_value="acct-1"), \
             patch.object(ae, "_apollo_find_contact_by_email",
                          return_value={"contact_campaign_statuses": []}), \
             patch.object(ae.prov, "_session") as session:
            session.post.side_effect = self._post(200, {"contacts": [], "note": "declined"})
            ok, detail = ae._apollo_add_to_sequence("c-1", "seq-1", "a@b.com")
        self.assertFalse(ok)
        self.assertIn("not actually added", detail)
        self.assertIn("declined", detail)

    def test_success_returns_no_detail(self):
        with patch.object(ae, "_apollo_default_send_account_id", return_value="acct-1"), \
             patch.object(ae, "_apollo_find_contact_by_email",
                          return_value={"contact_campaign_statuses": [
                              {"emailer_campaign_id": "seq-1"}]}), \
             patch.object(ae.prov, "_session") as session:
            session.post.side_effect = self._post(200, {})
            ok, detail = ae._apollo_add_to_sequence("c-1", "seq-1", "a@b.com")
        self.assertTrue(ok)
        self.assertIsNone(detail)


class TestEnrollFailureLog(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "nested" / "enroll_failures_20261002.log"

    def test_a_failure_is_recorded_with_company_email_and_detail(self):
        ae._log_enroll_failure("avery@example.io", "Acme", "body: {\"note\": \"declined\"}",
                               log_path=self.log)
        written = self.log.read_text()
        self.assertIn("avery@example.io", written)
        self.assertIn("Acme", written)
        self.assertIn("declined", written)

    def test_it_appends_across_failures(self):
        ae._log_enroll_failure("a@x.com", "X", "r1", log_path=self.log)
        ae._log_enroll_failure("b@y.com", "Y", "r2", log_path=self.log)
        self.assertEqual(len([l for l in self.log.read_text().splitlines() if l.strip()]), 2)

    def test_a_missing_parent_directory_is_created(self):
        self.assertFalse(self.log.parent.exists())
        ae._log_enroll_failure("a@x.com", "X", "r", log_path=self.log)
        self.assertTrue(self.log.exists())

    def test_an_unwritable_path_does_not_raise(self):
        try:
            ae._log_enroll_failure("a@x.com", "X", "r",
                                   log_path=Path("/dev/null/nope/fail.log"))
        except OSError as exc:
            self.fail("failure logging escaped: {!r}".format(exc))

    def test_the_default_path_is_a_dated_file_under_outputs(self):
        path = ae.enroll_failure_log_path()
        self.assertEqual(path.parent, Path("outputs"))
        self.assertRegex(path.name, r"^enroll_failures_\d{8}\.log$")


class TestEnrollContactSurfacesTheBody(unittest.TestCase):
    def test_the_failure_detail_and_the_log_both_carry_the_body(self):
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        log = Path(tempfile.mkdtemp()) / "enroll_failures.log"
        body_detail = ('Apollo accepted the request (HTTP 200) but the contact was not actually '
                       'added... Apollo response body: {"note": "declined"}')
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=None), \
             patch.object(ae, "_apollo_create_contact", return_value="apollo-1"), \
             patch.object(ae, "_apollo_update_ghost_fields", return_value=True), \
             patch.object(ae, "_apollo_add_to_sequence", return_value=(False, body_detail)), \
             patch.object(ae, "enroll_failure_log_path", return_value=log):
            result = ae.enroll_contact(_contact(), "Acme", None, None, False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("declined", result["detail"])
        self.assertIn("declined", log.read_text())

    def test_a_successful_enrollment_writes_no_failure_log(self):
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        log = Path(tempfile.mkdtemp()) / "enroll_failures.log"
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=None), \
             patch.object(ae, "_apollo_create_contact", return_value="apollo-1"), \
             patch.object(ae, "_apollo_update_ghost_fields", return_value=True), \
             patch.object(ae, "_apollo_add_to_sequence", return_value=(True, None)), \
             patch.object(ae, "enroll_failure_log_path", return_value=log):
            result = ae.enroll_contact(_contact(), "Acme", None, None, False)
        self.assertEqual(result["status"], "enrolled")
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
