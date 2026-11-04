"""apollo_enroll.py --dry-run and --redact.

--dry-run: every read-only stage still runs (Fibery loads, guards, Apollo lookups, copy generation),
but no Apollo write, no Fibery write and no enroll-cap record happens, and each skipped write is
printed as a "would:" line.

--redact: contact names and emails are masked in everything printed to stdout, including lines
printed from inside providers.py, while company names stay readable.

No network: every Fibery/Apollo/LLM entry point is patched.
"""
import io
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae
from src import redact
from src import providers as prov

GHOST = {"subject": "ramp delay", "email_1": "Gotta move fast, Priya. Acme is hiring.",
         "email_2": "Hey Priya,\nfollow-up", "email_3": "Hey Priya,\nlast one"}

CONTACT = {"id": "fib-contact-1", "name": "Priya Lal", "title": "Head of People",
           "email": "priya.lal@acme.io", "linkedin": "", "persona": "People Ops",
           "likely_priorities": "keep ramp risk low", "company_id": "co-1",
           "crm_contact_id": None, "sequence_status": "Not Sequenced",
           "outreach_status": "Cleared to proceed"}

COMPANIES = {"co-1": {"name": "Acme Labs", "domain": "acme.io", "stated_goal": None}}


class _Harness:
    """Patches every external entry point main() touches and records the write calls."""

    def __init__(self, apollo_contact=None, ghost=GHOST, provider_noise=False):
        self.apollo_contact = apollo_contact
        self.ghost = ghost
        self.provider_noise = provider_noise

    def run(self, argv):
        out = io.StringIO()
        with ExitStack() as st:
            p = lambda *a, **k: st.enter_context(patch.object(*a, **k))
            p(ae.fc, "get_token", return_value="tok")
            p(ae.fc, "load_enrollable_contacts", return_value=[dict(CONTACT)])
            p(ae.fc, "load_company_context_map", return_value=COMPANIES)
            p(ae.fc, "load_all_contacts", return_value=[dict(CONTACT)])
            p(ae, "contact_skip_reason", return_value=None)
            p(ae, "apollo_precheck_skip_reason", return_value=None)
            p(ae, "company_cap_reached", return_value=False)
            p(ae, "generate_ghost_emails", return_value=self.ghost)
            p(ae, "_apollo_default_send_account_id", return_value="acct-9")

            def find(email):
                if self.provider_noise:
                    prov._provider_error(None, "apollo/contacts-search", email, "HTTP 500 boom")
                return self.apollo_contact
            p(ae, "_apollo_find_contact_by_email", side_effect=find)

            self.create = p(ae, "_apollo_create_contact", return_value="apollo-new")
            self.ghost_write = p(ae, "_apollo_update_ghost_fields", return_value=True)
            self.add = p(ae, "_apollo_add_to_sequence", return_value=(True, None))
            self.fibery_update = p(ae.fc, "update")
            self.fibery_create = p(ae.fc, "create")
            self.record = p(ae.cache, "record_enrolled_company")
            p(ae.cache, "get_enrolled_company_count", return_value=0)
            st.enter_context(redirect_stdout(out))
            self.rc = ae.main(argv)
        self.out = out.getvalue()
        return self


class TestDryRun(unittest.TestCase):
    def test_no_apollo_or_fibery_writes(self):
        h = _Harness().run(["--dry-run"])
        self.assertEqual(h.rc, 0)
        for write in (h.create, h.ghost_write, h.add, h.fibery_update, h.fibery_create, h.record):
            write.assert_not_called()

    def test_prints_each_skipped_write(self):
        h = _Harness().run(["--dry-run"])
        self.assertIn("DRY RUN", h.out)
        self.assertIn("-> dry-run", h.out)
        self.assertIn("would: create Apollo contact for Priya Lal", h.out)
        self.assertIn("would: write Ghost Email", h.out)
        self.assertIn("would: add to Sales Outreach sequence {} sending from account acct-9"
                      .format(ae.SALES_OUTREACH_SEQUENCE_ID), h.out)
        self.assertIn("would: update Fibery contact fib-contact-1: Sequence Status = Sequenced",
                      h.out)
        # The generated copy is still shown.
        self.assertIn("subject: ramp delay", h.out)

    def test_existing_apollo_contact_is_reused_not_created(self):
        h = _Harness(apollo_contact={"id": "apollo-77", "contact_campaign_statuses": []}
                     ).run(["--dry-run"])
        self.assertIn("would: reuse Apollo contact apollo-77", h.out)
        self.assertNotIn("would: create Apollo contact", h.out)

    def test_already_in_sequence_is_still_detected(self):
        # A read-only stage, so a dry run must report the same skip a real run would.
        existing = {"id": "apollo-77", "contact_campaign_statuses": [
            {"emailer_campaign_id": ae.SALES_OUTREACH_SEQUENCE_ID, "status": "active"}]}
        h = _Harness(apollo_contact=existing).run(["--dry-run"])
        self.assertIn("-> skipped (already enrolled", h.out)
        self.assertNotIn("would:", h.out)

    def test_missing_send_account_is_predicted(self):
        with patch.object(ae, "_apollo_default_send_account_id", return_value=None):
            plan = ae._dry_run_plan(CONTACT, None)
        self.assertTrue(any("WOULD FAIL" in step for step in plan))

    def test_real_run_still_writes(self):
        h = _Harness().run([])
        h.create.assert_called_once()
        h.add.assert_called_once()
        h.fibery_update.assert_called_once()
        self.assertNotIn("would:", h.out)


class TestMasking(unittest.TestCase):
    def test_mask_name(self):
        self.assertEqual(redact.mask_name("Priya Lal"), "P**** L**")
        self.assertEqual(redact.mask_name(""), "")

    def test_mask_email(self):
        self.assertEqual(redact.mask_email("priya.lal@acme.io"), "p***@acme.io")
        self.assertEqual(redact.mask_email("not an email"), "not an email")

    def test_redact_is_idempotent(self):
        r = redact.Redactor(names=["Priya Lal"])
        once = r.redact("Priya Lal <priya.lal@acme.io>")
        self.assertEqual(r.redact(once), once)

    def test_full_name_masked_as_a_unit_then_first_name_alone(self):
        r = redact.Redactor(names=["Priya Lal"])
        self.assertEqual(r.redact("Priya Lal said hi. Hey Priya,"), "P**** L** said hi. Hey P****,")

    def test_company_word_shared_with_a_first_name_stays_visible(self):
        r = redact.Redactor(names=["Morgan Reyes"], protected=["Morgan Stanley"])
        self.assertEqual(r.redact("Morgan Reyes at Morgan Stanley"), "M***** R**** at Morgan Stanley")

    def test_unrelated_words_untouched(self):
        r = redact.Redactor(names=["Priya Lal"])
        self.assertEqual(r.redact("Acme Labs hiring 5 NYC roles"), "Acme Labs hiring 5 NYC roles")


class TestRedactFlag(unittest.TestCase):
    def test_names_and_emails_masked_company_visible(self):
        h = _Harness().run(["--dry-run", "--redact"])
        self.assertNotIn("Priya", h.out)
        self.assertNotIn("priya.lal@acme.io", h.out)
        self.assertIn("p***@acme.io", h.out)
        self.assertIn("P**** L**", h.out)
        self.assertIn("Hey P****,", h.out)          # inside the generated copy
        self.assertIn("@ Acme Labs", h.out)          # company name stays visible
        self.assertIn("Acme is hiring", h.out)

    def test_provider_level_prints_are_masked_too(self):
        h = _Harness(provider_noise=True).run(["--dry-run", "--redact"])
        self.assertIn("apollo/contacts-search", h.out)
        self.assertNotIn("priya.lal@acme.io", h.out)

    def test_without_flag_nothing_is_masked(self):
        h = _Harness().run(["--dry-run"])
        self.assertIn("priya.lal@acme.io", h.out)
        self.assertIn("Hey Priya,", h.out)

    def test_stdout_restored_after_run(self):
        before = sys.stdout
        _Harness().run(["--dry-run", "--redact"])
        self.assertIs(sys.stdout, before)

    def test_redact_does_not_change_what_is_written(self):
        # Masking is terminal-only: a real run still sends the real email to Apollo.
        h = _Harness().run(["--redact"])
        args, _ = h.add.call_args
        self.assertEqual(args[2], "priya.lal@acme.io")


if __name__ == "__main__":
    unittest.main()
