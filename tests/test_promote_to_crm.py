"""
tests/test_promote_to_crm.py -- Phase 6 regression suite for promote_to_crm.py's check-then-create
and workflow/state logic. No network/token needed -- fc.create/fc.update are monkeypatched with
fakes that record calls, same spirit as this repo's other test files
(run: python3 -m unittest tests.test_promote_to_crm, matching tests/test_classifiers.py's own
documented run command; this Mac's .venv python3 symlink is stale, use system python3).

Covers, per claude/nycbound-gtm-phase6-crm-promotion-scope.md:
  - Company already Promoted to CRM -> reuse its existing CRM Account, no Accounts lookup/create.
  - Domain match on 1 - Pinpointe CRM/Accounts -> link, don't create.
  - No domain match -> create, with Source=Outbound/Type=Corporate Client.
  - workflow/state: Prospect -> Engaged (new or matched-existing); anything past Engaged, or
    Dormant/Lost, left untouched.
  - Contact side: email match -> link, don't create; no match -> create with Source/Type as above.
  - The same-run dedupe: two contacts at the same not-yet-promoted company in one run only create
    one Account (Rachel's explicit concern, 2026-09-17).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fibery_client as fc  # noqa: E402
import promote_to_crm as ptc  # noqa: E402


class FakeFibery:
    """Records every create/update call promote_to_crm.py makes and returns canned ids, so the
    dedupe/state logic can be tested without a real Fibery token or network access."""

    def __init__(self):
        self.creates = []  # (entity_type, entity) tuples
        self.updates = []  # (entity_type, entity_id, fields) tuples
        self._next_id = 1

    def create(self, token, entity_type, entity):
        self.creates.append((entity_type, entity))
        new_id = f"new-{entity_type.split('/')[-1].lower()}-{self._next_id}"
        self._next_id += 1
        return {"fibery/id": new_id}

    def update(self, token, entity_type, entity_id, fields):
        self.updates.append((entity_type, entity_id, fields))
        return {"fibery/id": entity_id}


def _company(promotion_status="Not Promoted", crm_account_id=None, domain="acme.com",
             name="Acme Inc", website="https://acme.com", company_id="company-1"):
    return {
        "id": company_id, "name": name, "domain": domain, "website": website,
        "promotion_status": promotion_status, "crm_account_id": crm_account_id,
    }


class TestResolveCrmAccount(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFibery()
        self._orig_create, self._orig_update = fc.create, fc.update
        fc.create, fc.update = self.fake.create, self.fake.update

    def tearDown(self):
        fc.create, fc.update = self._orig_create, self._orig_update

    def test_already_promoted_company_reuses_existing_account_no_lookup(self):
        company = _company(promotion_status="Promoted to CRM", crm_account_id="acct-existing")
        # A decoy domain match that must NOT be used -- the fast path skips the domain map entirely.
        crm_accounts_by_domain = {"acme.com": {"id": "acct-WRONG", "name": "Acme", "state": "Prospect"}}

        account_id, created, state_changed = ptc.resolve_crm_account(
            "tok", company, crm_accounts_by_domain, dry_run=False)

        self.assertEqual(account_id, "acct-existing")
        self.assertFalse(created)
        self.assertEqual(len(self.fake.creates), 0)
        # No Companies update either -- nothing changed on the Company side.
        self.assertFalse(any(e == "GTM Pipeline/Companies" for e, _, _ in self.fake.updates))

    def test_domain_match_links_existing_account_does_not_create(self):
        company = _company(domain="acme.com")
        crm_accounts_by_domain = {
            "acme.com": {"id": "acct-1", "name": "Acme", "state": "Contract Sent"},
        }

        account_id, created, state_changed = ptc.resolve_crm_account(
            "tok", company, crm_accounts_by_domain, dry_run=False)

        self.assertEqual(account_id, "acct-1")
        self.assertFalse(created)
        self.assertEqual(len(self.fake.creates), 0)
        self.assertTrue(any(e == "GTM Pipeline/Companies" and i == "company-1"
                             for e, i, _ in self.fake.updates))
        self.assertEqual(company["promotion_status"], "Promoted to CRM")
        self.assertEqual(company["crm_account_id"], "acct-1")

    def test_no_domain_match_creates_new_account_with_source_and_type(self):
        company = _company(domain="newco.com", name="Newco")
        account_id, created, state_changed = ptc.resolve_crm_account(
            "tok", company, {}, dry_run=False)

        self.assertTrue(created)
        self.assertEqual(len(self.fake.creates), 1)
        entity_type, entity = self.fake.creates[0]
        self.assertEqual(entity_type, "1 - Pinpointe CRM/Accounts")
        self.assertEqual(entity["1 - Pinpointe CRM/Source"], {"fibery/id": fc.CRM_ACCOUNT_SOURCE["Outbound"]})
        self.assertEqual(entity["1 - Pinpointe CRM/Type"], {"fibery/id": fc.CRM_ACCOUNT_TYPE["Corporate Client"]})
        self.assertEqual(entity["1 - Pinpointe CRM/Name"], "Newco")

    def test_new_account_always_flips_prospect_to_engaged(self):
        company = _company(domain="newco.com")
        _, created, state_changed = ptc.resolve_crm_account("tok", company, {}, dry_run=False)

        self.assertTrue(created)
        self.assertTrue(state_changed)
        state_updates = [f for e, i, f in self.fake.updates if e == "1 - Pinpointe CRM/Accounts"]
        self.assertEqual(len(state_updates), 1)
        self.assertEqual(state_updates[0]["workflow/state"], {"fibery/id": fc.CRM_ACCOUNT_STATE["Engaged"]})

    def test_matched_prospect_account_flips_to_engaged(self):
        company = _company(domain="acme.com")
        crm_accounts_by_domain = {"acme.com": {"id": "acct-1", "name": "Acme", "state": "Prospect"}}

        _, created, state_changed = ptc.resolve_crm_account(
            "tok", company, crm_accounts_by_domain, dry_run=False)

        self.assertTrue(state_changed)
        state_updates = [f for e, i, f in self.fake.updates
                          if e == "1 - Pinpointe CRM/Accounts" and i == "acct-1"]
        self.assertEqual(len(state_updates), 1)

    def test_matched_past_engaged_account_left_untouched(self):
        for past_state in ("Engaged", "Discovery Scheduled", "Proposal/Pilot Sent",
                           "Discovery Completed", "Contract Sent", "Customer/Partner"):
            with self.subTest(state=past_state):
                self.fake = FakeFibery()
                fc.create, fc.update = self.fake.create, self.fake.update
                company = _company(domain="acme.com")
                crm_accounts_by_domain = {"acme.com": {"id": "acct-1", "name": "Acme", "state": past_state}}

                _, _, state_changed = ptc.resolve_crm_account(
                    "tok", company, crm_accounts_by_domain, dry_run=False)

                self.assertFalse(state_changed)
                account_updates = [f for e, i, f in self.fake.updates if e == "1 - Pinpointe CRM/Accounts"]
                self.assertEqual(len(account_updates), 0)

    def test_matched_dormant_or_lost_account_left_untouched(self):
        for state in ("Dormant", "Lost"):
            with self.subTest(state=state):
                self.fake = FakeFibery()
                fc.create, fc.update = self.fake.create, self.fake.update
                company = _company(domain="acme.com")
                crm_accounts_by_domain = {"acme.com": {"id": "acct-1", "name": "Acme", "state": state}}

                _, _, state_changed = ptc.resolve_crm_account(
                    "tok", company, crm_accounts_by_domain, dry_run=False)

                self.assertFalse(state_changed)

    def test_two_contacts_same_new_company_in_one_run_create_only_one_account(self):
        """Rachel's explicit concern, 2026-09-17: two contacts at the same not-yet-promoted company,
        both queued in the same run, must not create two CRM Accounts."""
        company = _company(domain="sharedco.com", name="SharedCo")
        crm_accounts_by_domain = {}

        account_id_1, created_1, _ = ptc.resolve_crm_account(
            "tok", company, crm_accounts_by_domain, dry_run=False)
        # Second contact, same run, same (now-mutated) company dict -- exactly what the real loop does.
        account_id_2, created_2, _ = ptc.resolve_crm_account(
            "tok", company, crm_accounts_by_domain, dry_run=False)

        self.assertTrue(created_1)
        self.assertFalse(created_2)
        self.assertEqual(account_id_1, account_id_2)
        self.assertEqual(len(self.fake.creates), 1)


class TestResolveCrmContact(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFibery()
        self._orig_create, self._orig_update = fc.create, fc.update
        fc.create, fc.update = self.fake.create, self.fake.update

    def tearDown(self):
        fc.create, fc.update = self._orig_create, self._orig_update

    def test_email_match_links_existing_contact_does_not_create(self):
        contact = {"id": "gtm-c1", "Name": "Jane Doe", "Email": "jane@acme.com"}
        crm_contacts_by_email = {"jane@acme.com": {"id": "crm-c-1", "name": "Jane Doe"}}

        contact_id, created = ptc.resolve_crm_contact(
            "tok", contact, "acct-1", crm_contacts_by_email, dry_run=False)

        self.assertEqual(contact_id, "crm-c-1")
        self.assertFalse(created)
        self.assertEqual(len(self.fake.creates), 0)

    def test_no_email_match_creates_new_contact_with_source_and_type(self):
        contact = {"id": "gtm-c1", "Name": "Jane Doe", "Email": "jane@newco.com",
                   "Title": "VP Eng", "LinkedIn": "https://linkedin.com/in/jane"}

        contact_id, created = ptc.resolve_crm_contact(
            "tok", contact, "acct-1", {}, dry_run=False)

        self.assertTrue(created)
        self.assertEqual(len(self.fake.creates), 1)
        entity_type, entity = self.fake.creates[0]
        self.assertEqual(entity_type, "1 - Pinpointe CRM/Contacts")
        self.assertEqual(entity["1 - Pinpointe CRM/Source"], {"fibery/id": fc.CRM_CONTACT_SOURCE["Outbound"]})
        self.assertEqual(entity["1 - Pinpointe CRM/Type"], {"fibery/id": fc.CRM_CONTACT_TYPE["Corporate Client"]})
        self.assertEqual(entity["1 - Pinpointe CRM/Account"], {"fibery/id": "acct-1"})
        self.assertNotIn("workflow/state", entity)  # never written, per Rachel's call


if __name__ == "__main__":
    unittest.main()
