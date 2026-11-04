"""Regression suite for the Phase 17 person-dedupe and domain guards.

Covers the two shared helpers in src/providers.py -- same_person() (nickname- and
initial-aware person identity) and email_domain_matches_company() (a hard domain gate,
as opposed to _agency_domain_flag's advisory CSV annotation) -- plus the
pre-enrollment guard in src/apollo_enroll.py built on top of them.

The two directions of error are NOT symmetric, and these tests are weighted
accordingly. A false merge silently drops a real person from outreach entirely, so the
negative cases (two different people who must never be treated as one) matter at least
as much as the positive ones. Peter Lindqvist and Peter Oduya are both genuinely at
Northwind; nothing here may ever collapse them.

Run with: python -m unittest tests.test_contact_dedupe -v
No network, no API keys, no Fibery or Apollo access -- pure logic throughout.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae
from src import providers as prov


class TestNicknameGroups(unittest.TestCase):
    def test_groups_are_disjoint(self):
        # The whole reason the groups are curated by hand: a name in two groups chains
        # them together transitively and quietly merges people who share neither name.
        seen = {}
        for group in prov._NICKNAME_GROUPS:
            for name in group:
                self.assertNotIn(
                    name, seen,
                    "'{}' appears in two nickname groups ({} and {}) -- they would merge "
                    "transitively".format(name, seen.get(name), group))
                seen[name] = group

    def test_variants_share_a_canonical_form(self):
        for a, b in [("Sam", "Samantha"), ("Will", "William"), ("Mike", "Michael"),
                     ("Rob", "Robert"), ("Bob", "Robert"), ("Bob", "Rob"),
                     ("Liz", "Elizabeth"), ("Tom", "Thomas")]:
            with self.subTest(pair=(a, b)):
                self.assertEqual(prov.normalize_first_name(a), prov.normalize_first_name(b))

    def test_unrelated_names_do_not_share_a_canonical_form(self):
        self.assertNotEqual(prov.normalize_first_name("Sam"), prov.normalize_first_name("Will"))
        self.assertNotEqual(prov.normalize_first_name("Peter"), prov.normalize_first_name("Paul"))

    def test_unknown_name_normalizes_to_itself(self):
        self.assertEqual(prov.normalize_first_name("Dana"), "dana")
        self.assertEqual(prov.normalize_first_name("  Delia. "), "delia")


class TestSamePerson(unittest.TestCase):
    def test_nickname_and_formal_first_name_match(self):
        # Hollis: QuickEnrich held "Samantha", Apollo held "Sam".
        self.assertTrue(prov.same_person(
            {"name": "Sam Hollis"}, {"name": "Samantha Hollis"}))

    def test_last_initial_matches_full_last_name(self):
        # Gorski: one record is "Robert G", the other "Robert Gorski".
        self.assertTrue(prov.same_person(
            {"name": "Robert G"}, {"name": "Robert Gorski"}))

    def test_apollo_masked_last_name_matches(self):
        # Apollo's people-search masks the surname; only the first letter is real.
        self.assertTrue(prov.same_person(
            {"first_name": "Robert", "last_name": "Go***i"},
            {"first_name": "Robert", "last_name": "Gorski"}))

    def test_same_first_name_different_last_names_never_match(self):
        # The case that must never merge: two real, different people at one company.
        self.assertFalse(prov.same_person(
            {"name": "Peter Lindqvist"}, {"name": "Peter Oduya"}))

    def test_different_first_names_with_same_last_name_never_match(self):
        self.assertFalse(prov.same_person(
            {"name": "Dana Whitlock"}, {"name": "Delia Whitlock"}))

    def test_missing_last_name_never_matches_on_first_name_alone(self):
        self.assertFalse(prov.same_person(
            {"first_name": "Peter", "last_name": ""}, {"name": "Peter Lindqvist"}))
        self.assertFalse(prov.same_person({"name": "Peter"}, {"name": "Peter Lindqvist"}))

    def test_identical_linkedin_matches_despite_different_names(self):
        self.assertTrue(prov.same_person(
            {"name": "Sam Hollis", "linkedin": "https://www.linkedin.com/in/shollis/"},
            {"name": "Samantha J Hollis", "linkedin": "http://linkedin.com/in/shollis"}))

    def test_differing_linkedin_does_not_veto_a_name_match(self):
        # A stale or wrong profile URL on one record must not defeat the name evidence.
        self.assertTrue(prov.same_person(
            {"name": "Sam Hollis", "linkedin": "https://linkedin.com/in/shollis"},
            {"name": "Samantha Hollis", "linkedin": "https://linkedin.com/in/samantha-hollis"}))

    def test_case_and_punctuation_are_ignored(self):
        self.assertTrue(prov.same_person(
            {"name": "ROB  O'BRIEN"}, {"name": "robert obrien"}))


class TestEmailDomainMatchesCompany(unittest.TestCase):
    def test_exact_and_subdomain_match(self):
        self.assertTrue(prov.email_domain_matches_company("a@acme.com", "acme.com"))
        self.assertTrue(prov.email_domain_matches_company("a@mail.acme.com", "acme.com"))
        self.assertTrue(prov.email_domain_matches_company("a@acme.com", "https://www.acme.com/"))

    def test_corporate_suffix_and_tld_differences_still_match(self):
        self.assertTrue(prov.email_domain_matches_company("a@acme-inc.com", "acme.com"))

    def test_curated_alias_group_matches(self):
        self.assertTrue(prov.email_domain_matches_company("a@getdbt.com", "dbtlabs.com"))

    def test_unrelated_domains_do_not_match(self):
        # Marisol: a priorco.io address on a globex.co record.
        self.assertFalse(prov.email_domain_matches_company("a@priorco.io", "globex.co"))
        # Okafor: a contractco.io address on an Initech record.
        self.assertFalse(prov.email_domain_matches_company("a@contractco.io", "initech.com"))

    def test_missing_data_never_counts_as_a_mismatch(self):
        self.assertTrue(prov.email_domain_matches_company("a@acme.com", ""))
        self.assertTrue(prov.email_domain_matches_company("", "acme.com"))
        self.assertTrue(prov.email_domain_matches_company("not-an-email", "acme.com"))


def _contact(**over):
    base = {"id": "c-self", "name": "Jane Doe", "title": "CFO", "email": "jane@acme.com",
            "linkedin": "", "persona": "CFO/Finance", "likely_priorities": "x",
            "company_id": "co-1"}
    base.update(over)
    return base


def _existing(**over):
    base = {"id": "c-other", "name": "Jane Doe", "title": "CFO", "email": "jane@acme.com",
            "linkedin": "", "company_id": "co-1", "persona": "CFO/Finance",
            "sequence_status": "Not Sequenced", "outreach_status": "Cleared to proceed",
            "last_sequenced": ""}
    base.update(over)
    return base


COMPANY = {"name": "Acme", "domain": "acme.com"}


class TestContactSkipReason(unittest.TestCase):
    def test_clean_contact_is_not_skipped(self):
        self.assertIsNone(ae.contact_skip_reason(_contact(), COMPANY, []))

    def test_sibling_already_sequenced_blocks_the_duplicate(self):
        existing = [_existing(name="Samantha Hollis", sequence_status="Sequenced")]
        reason = ae.contact_skip_reason(
            _contact(name="Sam Hollis", email="sam@acme.com"), COMPANY, existing)
        self.assertIsNotNone(reason)
        self.assertIn("already in flight", reason)
        self.assertIn("Sequenced", reason)

    def test_sibling_recently_contacted_blocks_the_duplicate(self):
        existing = [_existing(name="Robert Gorski", outreach_status="Recently Contacted")]
        reason = ae.contact_skip_reason(
            _contact(name="Robert G", email="rob@acme.com"), COMPANY, existing)
        self.assertIsNotNone(reason)
        self.assertIn("Recently Contacted", reason)

    def test_different_person_at_the_same_company_is_not_blocked(self):
        # Two real finance contacts at one company is normal, not a duplicate.
        existing = [_existing(name="Peter Oduya", sequence_status="Sequenced")]
        self.assertIsNone(ae.contact_skip_reason(
            _contact(name="Peter Lindqvist", email="plindqvist@acme.com"), COMPANY, existing))

    def test_sibling_not_in_flight_is_not_a_blocker(self):
        existing = [_existing(name="Samantha Hollis", sequence_status="Not Sequenced",
                              outreach_status="Cleared to proceed")]
        self.assertIsNone(ae.contact_skip_reason(
            _contact(name="Sam Hollis", email="sam@acme.com"), COMPANY, existing))

    def test_the_contacts_own_row_is_never_its_own_duplicate(self):
        existing = [_existing(id="c-self", name="Jane Doe", sequence_status="Sequenced")]
        self.assertIsNone(ae.contact_skip_reason(
            _contact(id="c-self"), COMPANY, existing))

    def test_domain_mismatch_is_skipped_with_both_domains_named(self):
        reason = ae.contact_skip_reason(
            _contact(email="okafor@contractco.io"), {"name": "Initech",
                                                    "domain": "initech.com"}, [])
        self.assertIsNotNone(reason)
        self.assertIn("contractco.io", reason)
        self.assertIn("initech.com", reason)

    def test_blank_company_domain_does_not_block(self):
        self.assertIsNone(ae.contact_skip_reason(
            _contact(email="jane@whatever.com"), {"name": "Acme", "domain": ""}, []))

    def test_duplicate_check_runs_before_the_domain_check(self):
        # Both guards would fire; the person-level reason is the more useful one to report.
        existing = [_existing(name="Samantha Hollis", sequence_status="Sequenced")]
        reason = ae.contact_skip_reason(
            _contact(name="Sam Hollis", email="sam@contractco.io"), COMPANY, existing)
        self.assertIn("already in flight", reason)


if __name__ == "__main__":
    unittest.main()
