"""Regression suite for Phase 17 step 5: the Fibery-create write guards in main.py.

Covers main._contact_write_skip_reason's two new guards (domain mismatch, and an email already
present on another Fibery contact) alongside the step 4 duplicate-person guard they sit with,
plus main._log_manual_review.

The governing rule for all of these: a guard may decline to CREATE, but it may never silently
drop. Every skip is still written to the run's CSV upstream and must also land in the
manual-review log with its reason -- a wrong-domain email is sometimes a real person whose
company record has the wrong Domain set, and nobody can tell which from an absence. So these
tests assert the reason text names the specific values a human would need to adjudicate, not
just that a skip happened.

Run with: python -m unittest tests.test_contact_write_guards -v
No network, no API keys, no Fibery; the log is redirected to a temp file.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as m


def _contact(name="Jane Doe", email="jane@acme.com", linkedin=""):
    return {"contact_name": name, "email": email, "linkedin": linkedin}


def _seen(name, persona="cfo", email="seen@acme.com"):
    return {"name": name, "persona": persona, "email": email, "linkedin": ""}


def _fibery_contact(name="Someone Else", cid="contact-9", company_id="company-9"):
    return {"id": cid, "name": name, "company_id": company_id}


class TestDomainGuard(unittest.TestCase):
    def test_matching_domain_is_allowed(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="jane@acme.com"), [], company_domain="acme.com"))

    def test_subdomain_is_allowed(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="jane@mail.acme.com"), [], company_domain="acme.com"))

    def test_corporate_suffix_difference_is_allowed(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="jane@acme-inc.com"), [], company_domain="acme.com"))

    def test_curated_alias_is_allowed(self):
        # The small alias dict: getdbt.com serves dbt Labs' careers pages (CLAUDE.md).
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="jane@getdbt.com"), [], company_domain="dbtlabs.com"))

    def test_unrelated_domain_is_skipped_and_names_both_domains(self):
        reason = m._contact_write_skip_reason(
            _contact(email="okafor@contractco.io"), [], company_domain="initech.com")
        self.assertIsNotNone(reason)
        self.assertIn("contractco.io", reason)
        self.assertIn("initech.com", reason)

    def test_blank_company_domain_never_blocks(self):
        # Missing data is not evidence of a mismatch, and must not cost a real contact.
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="jane@whatever.com"), [], company_domain=""))


class TestCrossRecordEmailGuard(unittest.TestCase):
    def test_email_on_another_contact_is_skipped(self):
        reason = m._contact_write_skip_reason(
            _contact(email="shared@acme.com"), [], company_domain="acme.com",
            existing_emails={"shared@acme.com": _fibery_contact("Prior Record", "c-1", "co-2")})
        self.assertIsNotNone(reason)
        self.assertIn("already on Fibery contact", reason)
        self.assertIn("Prior Record", reason)

    def test_reason_names_the_other_company_so_the_collision_is_adjudicable(self):
        reason = m._contact_write_skip_reason(
            _contact(email="shared@acme.com"), [], company_domain="acme.com",
            existing_emails={"shared@acme.com": _fibery_contact(company_id="other-company")})
        self.assertIn("other-company", reason)

    def test_match_is_case_insensitive(self):
        reason = m._contact_write_skip_reason(
            _contact(email="Shared@Acme.com"), [], company_domain="acme.com",
            existing_emails={"shared@acme.com": _fibery_contact()})
        self.assertIsNotNone(reason)

    def test_an_unseen_email_is_allowed(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(email="fresh@acme.com"), [], company_domain="acme.com",
            existing_emails={"someone.else@acme.com": _fibery_contact()}))

    def test_empty_index_is_allowed(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(), [], company_domain="acme.com", existing_emails={}))


class TestGuardOrdering(unittest.TestCase):
    def test_duplicate_person_is_reported_before_a_domain_problem(self):
        reason = m._contact_write_skip_reason(
            _contact(name="Sam Hollis", email="sam@contractco.io"),
            [_seen("Samantha Hollis")], company_domain="acme.com")
        self.assertIn("same person already written", reason)

    def test_domain_problem_is_reported_before_an_email_collision(self):
        reason = m._contact_write_skip_reason(
            _contact(email="x@contractco.io"), [], company_domain="acme.com",
            existing_emails={"x@contractco.io": _fibery_contact()})
        self.assertIn("does not match company Domain", reason)

    def test_a_clean_contact_passes_every_guard(self):
        self.assertIsNone(m._contact_write_skip_reason(
            _contact(), [_seen("Totally Different Human")], company_domain="acme.com",
            existing_emails={"nobody@acme.com": _fibery_contact()}))


class TestManualReviewLog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "nested" / "manual_review_skips.log"

    def test_a_skip_is_recorded_with_every_field_needed_to_adjudicate_it(self):
        m._log_manual_review("Initech", "cfo",
                             _contact(name="Sam Okafor", email="sam@contractco.io"),
                             "email domain 'contractco.io' does not match company Domain "
                             "'initech.com'", log_path=self.tmp)
        written = self.tmp.read_text()
        for expected in ["Initech", "cfo", "Sam Okafor", "sam@contractco.io", "contractco.io"]:
            self.assertIn(expected, written)

    def test_the_log_appends_rather_than_overwriting(self):
        m._log_manual_review("A", "cfo", _contact(email="one@a.com"), "r1", log_path=self.tmp)
        m._log_manual_review("B", "hr_people_ops", _contact(email="two@b.com"), "r2",
                             log_path=self.tmp)
        lines = [l for l in self.tmp.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("one@a.com", lines[0])
        self.assertIn("two@b.com", lines[1])

    def test_missing_parent_directory_is_created(self):
        self.assertFalse(self.tmp.parent.exists())
        m._log_manual_review("A", "cfo", _contact(), "reason", log_path=self.tmp)
        self.assertTrue(self.tmp.exists())

    def test_an_unwritable_path_does_not_raise(self):
        # A logging failure must never take down an otherwise fine run.
        unwritable = Path("/dev/null/cannot/exist/manual_review.log")
        try:
            m._log_manual_review("A", "cfo", _contact(), "reason", log_path=unwritable)
        except OSError as exc:
            self.fail("logging failure escaped: {!r}".format(exc))

    def test_an_unnamed_contact_still_logs(self):
        m._log_manual_review("A", "cfo", {"contact_name": "", "email": ""}, "reason",
                             log_path=self.tmp)
        self.assertIn("(unnamed)", self.tmp.read_text())


if __name__ == "__main__":
    unittest.main()
