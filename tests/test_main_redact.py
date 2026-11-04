"""main.py --redact.

Same masking rules as apollo_enroll.py --redact (src/redact.py): contact names, emails and LinkedIn
slugs are masked in terminal output; company names stay visible. main.py learns contact names
mid-run (the waterfall discovers them), so these tests also check that names known only from
Fibery at startup -- e.g. another contact named in a duplicate-email skip reason -- are masked, and
that --redact changes nothing about what is written or how --dry-run behaves.

No network: every Fibery/provider/LLM/cache entry point is patched. Nothing is written to disk.
"""
import io
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as m
from src import providers as prov

COMPANY = {"company": "Acme Labs", "website": "https://acme.io"}
FCOMPANY = {"id": "co-1", "name": "Acme Labs", "domain": "acme.io"}

CONTACT = {"contact_name": "Priya Lal", "email": "priya.lal@acme.io", "status": "resolved",
           "title": "CFO", "linkedin": "https://www.linkedin.com/in/priya-lal-4b2",
           "source": "QuickEnrich"}

JOBS = {"qualification": "QUALIFIED - 4 NYC roles", "signal_tier": "Tier 1",
        "nyc_roles": ["Sr. Data Eng ($180K)"], "_cache": "miss"}

# Another person already in Fibery, at another company, holding a colliding email. The skip reason
# printed for the collision names this person -- known only from the startup Fibery load.
OTHER_IN_FIBERY = {"id": "contact-9", "name": "Morgan Reyes", "email": "dup@acme.io",
                   "company_id": "co-9"}


class _Harness:
    def __init__(self, contacts=None, provider_noise=False, fibery_error=None):
        self.contacts = contacts if contacts is not None else [dict(CONTACT)]
        self.provider_noise = provider_noise
        self.fibery_error = fibery_error

    def _find_contacts(self, company, website, ledger, dry_run=False, no_cache=False):
        if self.provider_noise:
            # Printed from inside the waterfall, BEFORE main.py has seen any contact name.
            prov._provider_error(None, "quickenrich/email-search", CONTACT["linkedin"], "HTTP 500")
            prov._provider_error(None, "scrubby", CONTACT["email"], "request failed")
        return {"company": company, "persona": "cfo", "status": "found",
                "contacts": [dict(c) for c in self.contacts], "_cache": "miss"}

    def run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        persona = SimpleNamespace(key="cfo", label="CFO / Finance", status="live",
                                  find_contacts=self._find_contacts)
        with ExitStack() as st:
            p = lambda *a, **k: st.enter_context(patch.object(*a, **k))
            p(m, "PERSONAS", {"cfo": persona})
            p(m, "require_keys", return_value=None)
            p(m.fc, "get_token", return_value="tok")
            p(m.fc, "load_company_map", return_value={"acme.io": FCOMPANY})
            p(m.fc, "load_contacts_by_company", return_value={})
            if self.fibery_error:
                p(m.fc, "load_all_contacts", side_effect=self.fibery_error)
            else:
                p(m.fc, "load_all_contacts", return_value=[OTHER_IN_FIBERY])
            p(m.fc, "is_active_company", return_value=True)
            p(m, "read_company_list", return_value=[dict(COMPANY)])
            p(m, "research_company", return_value=dict(JOBS))
            p(m, "research_stated_goal", return_value={"found": False})
            p(m, "research_likely_priorities", return_value="keep the forecast intact")
            p(m, "_reconcile_contact", return_value={
                "outreach_status": "Cleared to proceed", "existing_fibery_id": None,
                "last_sequenced_hint": None})
            p(m, "_build_row", side_effect=lambda co, label, contact, *a: {
                "Qualification": JOBS["qualification"], "Outreach Status": "",
                "Email": contact.get("email") or "", "Contact Name": contact.get("contact_name")})
            p(m.cache, "is_fresh_cached", return_value=False)
            p(m.cache, "get_daily_count", return_value=0)
            p(m.cache, "increment_daily_count")
            p(m, "_install_sigterm_handler")
            p(m, "_append_spend_line")
            p(m, "_log_manual_review")
            p(m, "scrub_needs_verification")
            self.write_outputs = p(m, "write_outputs", return_value=(
                ([], "ready.csv"), ([], "needs.csv"), ([], "rest.csv")))
            self.fibery_create = p(m.fc, "create", return_value={"fibery/id": "new-1"})
            self.fibery_update = p(m.fc, "update")
            st.enter_context(redirect_stdout(out))
            st.enter_context(redirect_stderr(err))
            self.rc = m.main(["--input", "companies.csv"] + argv)
        self.out, self.err = out.getvalue(), err.getvalue()
        return self


class TestMainRedact(unittest.TestCase):
    def test_names_emails_and_linkedin_masked_company_visible(self):
        h = _Harness().run(["--redact"])
        self.assertEqual(h.rc, 0)
        self.assertNotIn("Priya", h.out)
        self.assertNotIn("priya.lal@acme.io", h.out)
        self.assertIn("P**** L** | resolved | p***@acme.io", h.out)
        self.assertIn("[1/1] Acme Labs", h.out)

    def test_provider_lines_printed_before_the_name_is_known_are_masked(self):
        h = _Harness(provider_noise=True).run(["--redact"])
        self.assertIn("quickenrich/email-search error for https://www.linkedin.com/in/***", h.out)
        self.assertIn("scrubby error for p***@acme.io", h.out)
        self.assertNotIn("priya-lal", h.out)
        self.assertNotIn("priya.lal@acme.io", h.out)

    def test_name_known_only_from_fibery_is_masked_in_a_skip_reason(self):
        dup = dict(CONTACT, contact_name="Dev Patel", email="dup@acme.io", linkedin="")
        h = _Harness(contacts=[dup]).run(["--redact"])
        self.assertIn("contact NOT created", h.out)
        self.assertNotIn("Morgan Reyes", h.out)
        self.assertIn("M***** R****", h.out)

    def test_without_flag_nothing_is_masked(self):
        h = _Harness(provider_noise=True).run([])
        self.assertIn("Priya Lal | resolved | priya.lal@acme.io", h.out)
        self.assertIn("linkedin.com/in/priya-lal-4b2", h.out)

    def test_redact_does_not_change_what_is_written(self):
        h = _Harness().run(["--redact"])
        entity = h.fibery_create.call_args[0][2]
        self.assertEqual(entity["GTM Pipeline/Name"], "Priya Lal")
        self.assertEqual(entity["GTM Pipeline/Email"], "priya.lal@acme.io")
        rows = h.write_outputs.call_args[0][0]
        self.assertEqual(rows[0]["Email"], "priya.lal@acme.io")

    def test_redact_leaves_dry_run_behaviour_alone(self):
        h = _Harness().run(["--dry-run", "--redact"])
        h.fibery_create.assert_not_called()
        h.fibery_update.assert_not_called()
        self.assertIn("fibery : dry-run -- write-back skipped", h.out)
        self.assertNotIn("Priya", h.out)

    def test_stderr_is_masked_too(self):
        # The Fibery-load WARNING goes to stderr; --company Nope then stops the run early.
        h = _Harness(fibery_error=RuntimeError("row priya.lal@acme.io bad")).run(
            ["--redact", "--company", "Nope"])
        self.assertEqual(h.rc, 2)
        self.assertIn("p***@acme.io", h.err)
        self.assertNotIn("priya.lal@acme.io", h.err)

    def test_streams_restored_after_run(self):
        before_out, before_err = sys.stdout, sys.stderr
        _Harness().run(["--redact"])
        self.assertIs(sys.stdout, before_out)
        self.assertIs(sys.stderr, before_err)


if __name__ == "__main__":
    unittest.main()
