"""Phase 21 (2026-10-05) regression tests.

1. Combined-company read-side guard. Nine Fibery Companies were created as several companies joined by
   commas ("Baselayer,Confido,Corridor,Cyera,Spott" with Domain "baselayer.com,confidotech.com,...").
   main.py researched the joined Domain as one domain and attached contacts from several companies to one
   record. Every reader must refuse them; "Parallel Worlds, Inc." is a real name and must survive.
2. requalify_quarantined: lists by default, deletes only company_research rows only with --apply.

No network, no real Fibery token, no real cache file.

Run with: python -m unittest tests.test_phase21_guards -v
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import apollo_enroll as ae
from src import cache
from src import fibery_client as fc
from src import main as m
from src import requalify_quarantined as rq


class TestCombinedCompanyReason(unittest.TestCase):
    def test_comma_joined_names_are_combined(self):
        self.assertIsNotNone(fc.combined_company_reason("Cyera,Chamelio", None))
        self.assertIsNotNone(fc.combined_company_reason("Baselayer,Confido,Corridor,Cyera,Spott", ""))

    def test_comma_joined_domain_is_combined_even_with_a_clean_name(self):
        self.assertIsNotNone(fc.combined_company_reason("EliseAI", "eliseai.com,ortet.ai,rivetdating.com"))

    def test_allow_listed_legal_name_is_not_combined(self):
        self.assertIsNone(fc.combined_company_reason("Parallel Worlds, Inc.", "parallelworlds.example.com"))
        self.assertIsNone(fc.combined_company_reason("  parallel worlds, inc.  ", "parallelworlds.example.com"))

    def test_an_allow_listed_name_does_not_excuse_a_comma_domain(self):
        self.assertIsNotNone(fc.combined_company_reason("Parallel Worlds, Inc.", "parallelworlds.example.com,other.com"))

    def test_ordinary_company_is_not_combined(self):
        self.assertIsNone(fc.combined_company_reason("Clasp", "clasp.com"))
        self.assertIsNone(fc.combined_company_reason(None, None))


class TestLoadCompanyMapSkipsCombined(unittest.TestCase):
    def test_combined_rows_are_dropped_and_the_real_comma_name_is_kept(self):
        rows = [
            {"id": "1569", "Name": "Baselayer,Confido,Corridor,Cyera,Spott",
             "Domain": "baselayer.com,confidotech.com,corridoradvisors.com,cyera.com,spott.io"},
            {"id": "1575", "Name": "EliseAI,Ortet,Rivet,Zero Infinity Partners,Instinct",
             "Domain": "eliseai.com,ortet.ai,rivetdating.com,zeroinfinitypartners.com,instinct.com"},
            {"id": "717", "Name": "Parallel Worlds, Inc.", "Domain": "parallelworlds.example.com"},
            {"id": "545", "Name": "Elise AI", "Domain": "eliseai.com"},
        ]
        with patch.object(fc, "query", return_value=rows):
            out = fc.load_company_map("tok")
        self.assertEqual(sorted(out), ["eliseai.com", "parallelworlds.example.com"])
        self.assertEqual(out["eliseai.com"]["id"], "545")  # the real Elise AI, not the combined record


class TestCsvIntakeSkipsCombined(unittest.TestCase):
    def test_combined_csv_rows_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("Company,URL\n")
            f.write('"Cyera,Chamelio",cyera.com\n')
            f.write("Snorkel AI,\"snorkel.ai,chamelio.ai\"\n")
            f.write('"Parallel Worlds, Inc.",parallelworlds.example.com\n')
            f.write("Clasp,clasp.com\n")
        rows = m.read_company_list(f.name)
        self.assertEqual([r["company"] for r in rows], ["Parallel Worlds, Inc.", "Clasp"])


class TestEnrollmentSkipsCombined(unittest.TestCase):
    contact = {"id": "c1", "name": "Jordan Pike", "email": "jordan.pike@example.io", "linkedin": "",
               "persona": "CFO/Finance", "crm_contact_id": ""}

    def test_contact_on_a_combined_company_is_skipped(self):
        company = {"name": "Baselayer,Confido,Corridor,Cyera,Spott",
                   "domain": "baselayer.com,confidotech.com,corridoradvisors.com,cyera.com,spott.io"}
        reason = ae.contact_skip_reason(self.contact, company, [])
        self.assertIsNotNone(reason)
        self.assertIn("combined", reason)

    def test_contact_on_the_allow_listed_name_is_not_skipped_for_it(self):
        contact = dict(self.contact, email="ceo@parallelworlds.example.com")
        company = {"name": "Parallel Worlds, Inc.", "domain": "parallelworlds.example.com"}
        self.assertIsNone(ae.contact_skip_reason(contact, company, []))


class TestRequalifyQuarantined(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gtm_cache.db"
        self.patch = patch.object(cache, "CACHE_PATH", self.db)
        self.patch.start()
        conn = cache._connect()
        conn.execute("CREATE TABLE IF NOT EXISTS company_research (domain TEXT PRIMARY KEY, company_name TEXT, payload TEXT NOT NULL, fetched_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS stated_goal (domain TEXT PRIMARY KEY, company_name TEXT, payload TEXT NOT NULL, fetched_at REAL NOT NULL)")
        for d, q in (("suno.tech", "PENDING - not yet scanned by HyperSignal"), ("clasp.com", "QUALIFIED"), ("ramp.com", "QUALIFIED")):
            conn.execute("INSERT INTO company_research VALUES (?, ?, ?, 0)", (d, d, json.dumps({"qualification": q})))
        conn.execute("INSERT INTO stated_goal VALUES ('clasp.com', 'Clasp', '{}', 0)")
        conn.commit(); conn.close()
        self.seed = Path(self.tmp.name) / "seed.json"
        self.seed.write_text(json.dumps({
            "quarantine": [{"domain": "suno.tech", "vendor": "ashby", "slug": "suno"},
                           {"domain": "clasp.com", "vendor": "ashby", "slug": "clasp"},
                           {"domain": "method.com", "vendor": "ashby", "slug": "method"}],
            "overrides": [{"domain": "hightouch.com", "vendor": "ashby", "slug": "hightouch"}],
            "left_alone_unsure": [{"domain": "buena.ai", "vendor": "ashby", "slug": "buena"}]}))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _rows(self, table="company_research"):
        conn = sqlite3.connect(str(self.db))
        try:
            return sorted(r[0] for r in conn.execute("SELECT domain FROM {}".format(table)))
        finally:
            conn.close()

    def test_seed_domains_exclude_overrides_and_unsure(self):
        self.assertEqual(rq.domains_from_seed(self.seed), ["suno.tech", "clasp.com", "method.com"])

    def test_default_is_a_dry_run(self):
        report = rq.requalify(rq.domains_from_seed(self.seed), apply=False)
        self.assertEqual([e["domain"] for e in report if e["cached"]], ["suno.tech", "clasp.com"])
        self.assertFalse(any(e["deleted"] for e in report))
        self.assertEqual(self._rows(), ["clasp.com", "ramp.com", "suno.tech"])

    def test_apply_deletes_only_the_affected_company_research_rows(self):
        rq.requalify(rq.domains_from_seed(self.seed), apply=True)
        self.assertEqual(self._rows(), ["ramp.com"])
        self.assertEqual(self._rows("stated_goal"), ["clasp.com"])  # other cache tables untouched

    def test_cli_without_apply_writes_nothing(self):
        rq.main(["--seed", str(self.seed)])
        self.assertEqual(self._rows(), ["clasp.com", "ramp.com", "suno.tech"])


if __name__ == "__main__":
    unittest.main()
