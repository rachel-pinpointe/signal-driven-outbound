"""Regression suite for Phase 12's Apollo enrollment build (src/apollo_enroll.py -- see
claude/nycbound-gtm-phase12-apollo-hypersignal-scope.md, Sales & Marketing claude.ai Project).

Locks in the same "skip rather than ship under the bar" discipline as
test_likely_priorities.py: a Ghost Email response that leaks the company name, echoes a
play's placeholder/worked-example text, or has an over-long subject is rejected and never
written to Apollo. Also locks in that dry-run never touches Apollo or Fibery.

No per-run contact cap is asserted here any more: MAX_CONTACTS_PER_RUN was removed 2026-09-19
(it was a testing-only value that leaked into the real default -- see apollo_enroll.py's module
docstring). main.py's --daily-cap is the one real throttle, and it lives upstream of this script.

Run with: python -m unittest tests.test_apollo_enroll -v
No network, no API keys required -- the LLM call and all Apollo REST calls are mocked
throughout.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae
from src.providers import MissingKey

SAMPLE_PLAYS_MD = """# Email Plays

## Universal Cold Email Rules (Apply to Every Play)
Never name the company. Keep it short. Always hedge.

## CFO / Finance Leader
Angle: total people cost of relocation friction.
Worked example: "Hi Rowan, gotta get that Sr. Data Eng started..."

## Ramp Risk (People Ops / HR Leader)
Angle: ramp risk from relocation friction.

## Real Support vs. Lump Sum (Recruiter / Talent Partner)
Angle: real support beats a lump sum stipend.

## CFO / Finance Leader (archived, replaced Sept 2026)
Old stuff, should never be loaded.
"""

WELL_FORMED_TEXT = (
    "SUBJECT: a quick question on ramp time\n"
    "EMAIL 1:\nHi there, curious how you're thinking about onboarding friction this quarter.\n"
    "EMAIL 2:\nFollowing up in case this got buried -- happy to share what we're seeing.\n"
    "EMAIL 3:\nLast note from me -- let me know if this isn't relevant right now."
)

COMPANY_LEAK_TEXT = WELL_FORMED_TEXT.replace("this quarter", "at Acme Corp this quarter")

PLACEHOLDER_TEXT = (
    "SUBJECT: a quick question\n"
    "EMAIL 1:\nHi {{first_name}}, following up.\n"
    "EMAIL 2:\nStill there?\n"
    "EMAIL 3:\nLast note."
)

LONG_SUBJECT_TEXT = WELL_FORMED_TEXT.replace(
    "SUBJECT: a quick question on ramp time",
    "SUBJECT: a much longer subject line than nine words should ever be allowed to run",
)

MISSING_SECTION_TEXT = "SUBJECT: hello\nEMAIL 1:\nJust email 1, nothing else."

SAMPLE_CONTACT = {
    "id": "contact-1", "name": "Jane Doe", "title": "Head of People",
    "email": "jane@acme.com", "linkedin": "", "persona": "People Ops",
    "likely_priorities": "This contact likely wants to keep ramp risk low.",
    "company_id": "company-1",
}


class TestLoadMarkdownSection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        self.tmp.write(SAMPLE_PLAYS_MD)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_extracts_exact_heading_section(self):
        section = ae._load_markdown_section(self.path, "Ramp Risk (People Ops / HR Leader)")
        self.assertIn("ramp risk from relocation friction", section)
        self.assertNotIn("Real Support vs. Lump Sum", section)

    def test_stops_at_next_heading_and_does_not_bleed_into_archived_duplicate(self):
        section = ae._load_markdown_section(self.path, "CFO / Finance Leader")
        self.assertIn("total people cost", section)
        self.assertNotIn("Old stuff", section)

    def test_unknown_heading_returns_none(self):
        self.assertIsNone(ae._load_markdown_section(self.path, "Not A Real Heading"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(ae._load_markdown_section(Path("/no/such/file.md"), "Anything"))


class TestParseGhostEmailResponse(unittest.TestCase):
    def test_well_formed_response_parses_all_four_parts(self):
        parsed = ae._parse_ghost_email_response(WELL_FORMED_TEXT)
        self.assertEqual(parsed["subject"], "a quick question on ramp time")
        self.assertIn("onboarding friction", parsed["email_1"])
        self.assertIn("Following up", parsed["email_2"])
        self.assertIn("Last note", parsed["email_3"])

    def test_missing_section_returns_none(self):
        self.assertIsNone(ae._parse_ghost_email_response(MISSING_SECTION_TEXT))

    def test_empty_text_returns_none(self):
        self.assertIsNone(ae._parse_ghost_email_response(""))
        self.assertIsNone(ae._parse_ghost_email_response(None))


class TestValidateGhostEmail(unittest.TestCase):
    def test_well_formed_passes(self):
        parsed = ae._parse_ghost_email_response(WELL_FORMED_TEXT)
        self.assertTrue(ae._validate_ghost_email(parsed, "Acme Corp"))

    def test_company_name_leak_fails(self):
        parsed = ae._parse_ghost_email_response(COMPANY_LEAK_TEXT)
        self.assertFalse(ae._validate_ghost_email(parsed, "Acme Corp"))

    def test_placeholder_token_fails(self):
        parsed = ae._parse_ghost_email_response(PLACEHOLDER_TEXT)
        self.assertFalse(ae._validate_ghost_email(parsed, "Acme Corp"))

    def test_overlong_subject_fails(self):
        parsed = ae._parse_ghost_email_response(LONG_SUBJECT_TEXT)
        self.assertFalse(ae._validate_ghost_email(parsed, "Acme Corp"))

    def test_blank_company_name_never_false_positives(self):
        parsed = ae._parse_ghost_email_response(WELL_FORMED_TEXT)
        self.assertTrue(ae._validate_ghost_email(parsed, ""))


class TestGenerateGhostEmails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        self.tmp.write(SAMPLE_PLAYS_MD)
        self.tmp.close()
        self.patcher = patch.object(ae, "EMAIL_PLAYS_PATH", Path(self.tmp.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_unknown_persona_returns_none_without_calling_llm(self):
        contact = dict(SAMPLE_CONTACT, persona="Not A Real Persona")
        with patch("src.providers._llm_extract") as mock_extract:
            result = ae.generate_ghost_emails(contact, "Acme", None)
        self.assertIsNone(result)
        mock_extract.assert_not_called()

    def test_well_formed_llm_response_is_returned(self):
        with patch("src.providers._llm_extract", return_value=WELL_FORMED_TEXT):
            result = ae.generate_ghost_emails(SAMPLE_CONTACT, "Acme", None)
        self.assertEqual(result["subject"], "a quick question on ramp time")

    def test_guardrail_rejects_company_leak_and_returns_none(self):
        with patch("src.providers._llm_extract", return_value=COMPANY_LEAK_TEXT):
            result = ae.generate_ghost_emails(SAMPLE_CONTACT, "Acme Corp", None)
        self.assertIsNone(result)

    def test_missing_api_key_returns_none_not_an_exception(self):
        with patch("src.providers._llm_extract", side_effect=MissingKey("no key")):
            result = ae.generate_ghost_emails(SAMPLE_CONTACT, "Acme", None)
        self.assertIsNone(result)

    def test_prompt_includes_likely_priorities_and_play_section(self):
        captured = {}

        def fake_extract(prompt, ledger=None, max_tokens=900, call_site=None):
            captured["prompt"] = prompt
            return WELL_FORMED_TEXT

        with patch("src.providers._llm_extract", side_effect=fake_extract):
            ae.generate_ghost_emails(SAMPLE_CONTACT, "Acme", "Hiring 5 NYC roles by Q1")
        self.assertIn("ramp risk low", captured["prompt"])
        self.assertIn("ramp risk from relocation friction", captured["prompt"])
        self.assertIn("Hiring 5 NYC roles by Q1", captured["prompt"])
        self.assertIn("Never name the company", captured["prompt"])


class TestEnrollContactDryRun(unittest.TestCase):
    def test_dry_run_never_writes_to_apollo_or_fibery(self):
        # Reads (the contact lookup, the send-account lookup) are allowed on a dry run; writes not.
        with patch.object(ae, "generate_ghost_emails", return_value={
                "subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=None), \
             patch.object(ae, "_apollo_default_send_account_id", return_value="acct-1"), \
             patch.object(ae, "_apollo_create_contact") as mock_create, \
             patch.object(ae, "_apollo_update_ghost_fields") as mock_update, \
             patch.object(ae, "_apollo_add_to_sequence") as mock_add, \
             patch("src.fibery_client.update") as mock_fibery_update:
            result = ae.enroll_contact(SAMPLE_CONTACT, "Acme", None, "fake-token", dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        mock_create.assert_not_called()
        mock_update.assert_not_called()
        mock_add.assert_not_called()
        mock_fibery_update.assert_not_called()

    def test_failed_generation_is_skipped_before_any_apollo_call(self):
        with patch.object(ae, "generate_ghost_emails", return_value=None), \
             patch.object(ae, "_apollo_find_contact_by_email") as mock_find:
            result = ae.enroll_contact(SAMPLE_CONTACT, "Acme", None, "fake-token", dry_run=False)
        self.assertEqual(result["status"], "skipped")
        mock_find.assert_not_called()

    def test_real_run_writes_fibery_sequence_status_on_success(self):
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=None), \
             patch.object(ae, "_apollo_create_contact", return_value="apollo-123"), \
             patch.object(ae, "_apollo_update_ghost_fields", return_value=True), \
             patch.object(ae, "_apollo_add_to_sequence", return_value=(True, None)), \
             patch("src.fibery_client.update") as mock_fibery_update:
            result = ae.enroll_contact(SAMPLE_CONTACT, "Acme", None, "fake-token", dry_run=False)
        self.assertEqual(result["status"], "enrolled")
        mock_fibery_update.assert_called_once()
        args, kwargs = mock_fibery_update.call_args
        fields = args[3]
        self.assertEqual(fields["GTM Pipeline/Sequence Status"],
                         {"fibery/id": ae.fc.SEQUENCE_STATUS["Sequenced"]})

    def test_already_enrolled_is_skipped_without_creating_duplicate(self):
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        existing = {"id": "apollo-existing", "contact_campaign_statuses": [
            {"emailer_campaign_id": ae.SALES_OUTREACH_SEQUENCE_ID, "status": "active"}]}
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=existing), \
             patch.object(ae, "_apollo_create_contact") as mock_create:
            result = ae.enroll_contact(SAMPLE_CONTACT, "Acme", None, "fake-token", dry_run=False)
        self.assertEqual(result["status"], "skipped")
        mock_create.assert_not_called()


# Session is mocked; this only satisfies _apollo_headers() without a real .env.
@patch.dict(os.environ, {"APOLLO_API_KEY": "test-dummy-key"})
class TestApolloDefaultSendAccount(unittest.TestCase):
    def _mock_response(self, status_code, payload):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    def test_picks_the_account_marked_default(self):
        payload = {"email_accounts": [
            {"id": "acct-1", "default": False},
            {"id": "acct-2", "default": True},
        ]}
        with patch.object(ae.prov, "_session") as mock_session:
            mock_session.get.return_value = self._mock_response(200, payload)
            result = ae._apollo_default_send_account_id()
        self.assertEqual(result, "acct-2")

    def test_falls_back_to_first_account_when_none_marked_default(self):
        payload = {"email_accounts": [{"id": "acct-1", "default": False}]}
        with patch.object(ae.prov, "_session") as mock_session:
            mock_session.get.return_value = self._mock_response(200, payload)
            result = ae._apollo_default_send_account_id()
        self.assertEqual(result, "acct-1")

    def test_no_accounts_returns_none(self):
        with patch.object(ae.prov, "_session") as mock_session:
            mock_session.get.return_value = self._mock_response(200, {"email_accounts": []})
            result = ae._apollo_default_send_account_id()
        self.assertIsNone(result)

    def test_http_error_returns_none(self):
        with patch.object(ae.prov, "_session") as mock_session:
            mock_session.get.return_value = self._mock_response(500, {"error": "boom"})
            result = ae._apollo_default_send_account_id()
        self.assertIsNone(result)


# Session is mocked; this only satisfies _apollo_headers() without a real .env.
@patch.dict(os.environ, {"APOLLO_API_KEY": "test-dummy-key"})
class TestApolloAddToSequence(unittest.TestCase):
    def test_payload_includes_emailer_campaign_id_and_send_account(self):
        # Regression test for the 2026-09-18 Stage 1 live-send failures: Apollo's
        # /add_contact_ids endpoint started requiring both send_email_from_email_account_id
        # and emailer_campaign_id in the JSON body (not just the sequence id in the URL path),
        # which the original build didn't send. First HTTP 422 named the missing send-account
        # param; fixing that surfaced a second 422 naming emailer_campaign_id too.
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        confirmed_contact = {"contact_campaign_statuses": [{"emailer_campaign_id": "sequence-1"}]}
        with patch.object(ae, "_apollo_default_send_account_id", return_value="acct-42"), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=confirmed_contact), \
             patch.object(ae.prov, "_session") as mock_session:
            mock_session.post.side_effect = fake_post
            ok, detail = ae._apollo_add_to_sequence("contact-1", "sequence-1", "a@b.com")
        self.assertTrue(ok)
        self.assertIsNone(detail)
        self.assertEqual(captured["json"]["emailer_campaign_id"], "sequence-1")
        self.assertEqual(captured["json"]["send_email_from_email_account_id"], "acct-42")
        self.assertEqual(captured["json"]["contact_ids"], ["contact-1"])

    def test_no_send_account_available_fails_without_calling_apollo(self):
        with patch.object(ae, "_apollo_default_send_account_id", return_value=None), \
             patch.object(ae.prov, "_session") as mock_session:
            ok, detail = ae._apollo_add_to_sequence("contact-1", "sequence-1", "a@b.com")
        self.assertFalse(ok)
        self.assertIn("no linked Apollo email account", detail)
        mock_session.post.assert_not_called()

    def test_http_200_but_contact_not_actually_added_fails(self):
        # Regression test for the 2026-09-18 Stage 2 finding: Apollo returned a clean HTTP 200
        # for Owen Pratt's add-to-sequence call, but his contact record's
        # contact_campaign_statuses stayed empty -- Apollo had silently declined to add him
        # (almost certainly the same-day job-change safety block). The old code trusted the
        # HTTP status alone and reported this as a successful enrollment, which then wrote an
        # incorrect Sequence Status into Fibery. The fix re-fetches the contact after a
        # seemingly-successful call and only confirms success if the sequence actually shows up.
        def fake_post(url, headers=None, json=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        not_actually_added = {"contact_campaign_statuses": []}
        with patch.object(ae, "_apollo_default_send_account_id", return_value="acct-42"), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=not_actually_added), \
             patch.object(ae.prov, "_session") as mock_session:
            mock_session.post.side_effect = fake_post
            ok, detail = ae._apollo_add_to_sequence("contact-1", "sequence-1", "a@b.com")
        self.assertFalse(ok)
        self.assertIn("not actually added", detail)


if __name__ == "__main__":
    unittest.main()
