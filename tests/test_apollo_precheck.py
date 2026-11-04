"""Regression suite for Phase 18 step 2: the Apollo pre-check and CRM-link guards.

These three guards exist because every Fibery-side guard passed a contact who should never have
been cold-sequenced. The case that motivated them is Leah Brandt:

  - she had an existing Fibery Contact row,
  - with NO Last Sequenced Date, so the Phase 8 60-day Fibery gate cleared her,
  - her Sequence Status was legitimately "Not Sequenced" (nothing ever sequenced her),
  - but her Apollo record was source `email_import` -- synced out of Rachel's own mailbox -- with
    activity on 2026-09-21, eleven days before the run.

Nothing in Fibery could see that. Only her live Apollo record could, which is why the lookup now
happens before any copy is generated rather than after.

The guards are asymmetric on purpose: a contact Apollo has never heard of is the normal case for a
genuinely new prospect and must never be skipped, so "no Apollo record" is always a pass.

Run with: python -m unittest tests.test_apollo_precheck -v
No network, no API keys -- Apollo records are passed in as plain dicts.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae

TODAY = "2026-10-02"


def _apollo(**over):
    """A prospected Apollo record that passes every guard, as a baseline to vary from. Field
    values are the real ones observed on live records (source "api" / "Created from API")."""
    base = {"id": "apollo-1", "email": "new@acme.com", "email_status": "verified",
            "source": "api", "original_source": "api", "email_source": None,
            "source_display_name": "Created from API", "last_activity_date": None,
            "contact_campaign_statuses": []}
    base.update(over)
    return base


def _contact(**over):
    base = {"id": "fib-1", "name": "New Person", "title": "CFO", "email": "new@acme.com",
            "linkedin": "", "persona": "CFO/Finance", "likely_priorities": "x",
            "company_id": "co-1", "crm_contact_id": ""}
    base.update(over)
    return base


COMPANY = {"name": "Acme", "domain": "acme.com"}


class TestLeahBrandt(unittest.TestCase):
    """The real case's shape (name fictionalized): Fibery clean, Apollo not."""

    LEAH_FIBERY = {"id": "fib-leah", "name": "Leah Brandt", "title": "Head of People",
                    "email": "leah@acme.com", "linkedin": "", "persona": "People Ops",
                    "likely_priorities": "x", "company_id": "co-1", "crm_contact_id": ""}
    # No Last Sequenced Date anywhere, Sequence Status "Not Sequenced" -- a row
    # load_enrollable_contacts() correctly considers eligible.
    LEAH_FIBERY_SIBLINGS = [{"id": "fib-leah", "name": "Leah Brandt",
                              "email": "leah@acme.com", "linkedin": "",
                              "sequence_status": "Not Sequenced",
                              "outreach_status": "Cleared to proceed", "last_sequenced": ""}]
    LEAH_APOLLO = {"id": "apollo-leah", "email": "leah@acme.com", "email_status": "verified",
                    "source": "email_import", "original_source": "email_import",
                    "email_source": None, "source_display_name": "Synced from mailbox",
                    "last_activity_date": "2026-09-21T16:39:34.000+00:00",
                    "contact_campaign_statuses": []}

    def test_every_fibery_side_guard_passes_her(self):
        # Establishes the gap: this is why the Apollo lookup had to be added.
        self.assertIsNone(ae.contact_skip_reason(
            self.LEAH_FIBERY, COMPANY, self.LEAH_FIBERY_SIBLINGS))

    def test_the_apollo_precheck_catches_her(self):
        reason = ae.apollo_precheck_skip_reason(self.LEAH_APOLLO, TODAY)
        self.assertIsNotNone(reason)

    def test_recent_activity_is_reported_with_the_real_date_and_age(self):
        reason = ae.apollo_precheck_skip_reason(self.LEAH_APOLLO, TODAY)
        self.assertIn("11 day(s) ago", reason)
        self.assertIn("2026-09-21", reason)

    def test_she_is_still_caught_on_source_alone_if_activity_were_old(self):
        # Either condition alone must be sufficient; her record trips both.
        old = dict(self.LEAH_APOLLO, last_activity_date="2025-01-01T00:00:00.000+00:00")
        reason = ae.apollo_precheck_skip_reason(old, TODAY)
        self.assertIsNotNone(reason)
        self.assertIn("mailbox/email import", reason)


class TestRecentActivityGuard(unittest.TestCase):
    def test_activity_inside_the_window_is_skipped(self):
        for date in ("2026-10-02", "2026-09-21", "2026-08-04"):
            with self.subTest(date=date):
                self.assertIsNotNone(ae.apollo_precheck_skip_reason(
                    _apollo(last_activity_date=date + "T12:00:00.000+00:00"), TODAY))

    def test_activity_outside_the_window_is_allowed(self):
        # 2026-08-03 is 60 days before 2026-10-02 inclusive; 2026-08-02 is 61.
        self.assertIsNone(ae.apollo_precheck_skip_reason(
            _apollo(last_activity_date="2026-08-02T12:00:00.000+00:00"), TODAY))

    def test_the_boundary_day_is_treated_as_recent(self):
        self.assertIsNotNone(ae.apollo_precheck_skip_reason(
            _apollo(last_activity_date="2026-08-03T12:00:00.000+00:00"), TODAY))

    def test_no_activity_date_is_not_a_reason_to_skip(self):
        self.assertIsNone(ae.apollo_precheck_skip_reason(_apollo(last_activity_date=None), TODAY))

    def test_an_unparseable_date_is_ignored_rather_than_guessed(self):
        self.assertIsNone(ae.apollo_precheck_skip_reason(
            _apollo(last_activity_date="not a date"), TODAY))


class TestMailboxSourceGuard(unittest.TestCase):
    def test_email_import_is_skipped(self):
        reason = ae.apollo_precheck_skip_reason(
            _apollo(source="email_import", original_source="email_import"), TODAY)
        self.assertIsNotNone(reason)
        self.assertIn("mailbox/email import", reason)

    def test_other_mailbox_style_sources_are_skipped(self):
        for value in ("mailbox_sync", "email_sync", "gmail", "outlook"):
            with self.subTest(value=value):
                self.assertIsNotNone(ae.apollo_precheck_skip_reason(
                    _apollo(source=value, original_source=value), TODAY))

    def test_the_display_name_is_matched_as_a_substring(self):
        # Covers a mailbox source whose underlying enum this code hasn't seen.
        self.assertIsNotNone(ae.apollo_precheck_skip_reason(
            _apollo(source="some_future_enum", original_source="some_future_enum",
                    source_display_name="Synced from mailbox"), TODAY))

    def test_email_source_field_is_also_checked(self):
        self.assertIsNotNone(ae.apollo_precheck_skip_reason(
            _apollo(email_source="email_import"), TODAY))

    def test_real_prospected_sources_are_allowed(self):
        # The values confirmed on live prospected records -- these must keep flowing.
        self.assertIsNone(ae.apollo_precheck_skip_reason(
            _apollo(source="api", original_source="api",
                    source_display_name="Created from API"), TODAY))
        self.assertIsNone(ae.apollo_precheck_skip_reason(
            _apollo(source="csv_import", original_source="csv_import",
                    source_display_name="Uploaded from CSV"), TODAY))

    def test_case_is_ignored(self):
        self.assertIsNotNone(ae.apollo_precheck_skip_reason(
            _apollo(source="EMAIL_IMPORT", original_source="EMAIL_IMPORT"), TODAY))


class TestNoApolloRecord(unittest.TestCase):
    def test_a_contact_apollo_has_never_seen_is_allowed(self):
        # The normal case for a genuinely new prospect. Never a reason to skip.
        self.assertIsNone(ae.apollo_precheck_skip_reason(None, TODAY))

    def test_an_empty_record_is_allowed(self):
        self.assertIsNone(ae.apollo_precheck_skip_reason({}, TODAY))


class TestCrmLinkGuard(unittest.TestCase):
    def test_a_contact_linked_to_a_crm_contact_is_skipped(self):
        reason = ae.contact_skip_reason(_contact(crm_contact_id="crm-123"), COMPANY, [])
        self.assertIsNotNone(reason)
        self.assertIn("CRM Contact", reason)
        self.assertIn("crm-123", reason)

    def test_an_unpromoted_contact_is_allowed(self):
        self.assertIsNone(ae.contact_skip_reason(_contact(crm_contact_id=""), COMPANY, []))

    def test_a_missing_key_is_treated_as_unpromoted(self):
        contact = _contact()
        del contact["crm_contact_id"]
        self.assertIsNone(ae.contact_skip_reason(contact, COMPANY, []))

    def test_whitespace_only_link_is_treated_as_unpromoted(self):
        self.assertIsNone(ae.contact_skip_reason(_contact(crm_contact_id="   "), COMPANY, []))


class TestEnrollContactReusesTheFetchedRecord(unittest.TestCase):
    def test_a_supplied_apollo_record_prevents_a_second_lookup(self):
        from unittest.mock import patch
        already_enrolled = {"id": "apollo-1", "contact_campaign_statuses": [
            {"emailer_campaign_id": ae.SALES_OUTREACH_SEQUENCE_ID, "status": "active"}]}
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email") as mock_find:
            result = ae.enroll_contact(_contact(), "Acme", None, "tok", False,
                                       apollo_contact=already_enrolled)
        self.assertEqual(result["status"], "skipped")
        mock_find.assert_not_called()

    def test_without_a_supplied_record_it_still_fetches(self):
        from unittest.mock import patch
        ghost = {"subject": "s", "email_1": "1", "email_2": "2", "email_3": "3"}
        with patch.object(ae, "generate_ghost_emails", return_value=ghost), \
             patch.object(ae, "_apollo_find_contact_by_email", return_value=None) as mock_find, \
             patch.object(ae, "_apollo_create_contact", return_value=None):
            ae.enroll_contact(_contact(), "Acme", None, "tok", False)
        mock_find.assert_called_once()


if __name__ == "__main__":
    unittest.main()
