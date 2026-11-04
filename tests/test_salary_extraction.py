"""Regression suite for the extract_salary() label-preference fix (found 2026-09-16 during the
Phase 5 build, fixed 2026-09-17 -- see CLAUDE.md Known Issues).

Locks in: extract_salary() now prefers a dollar figure immediately preceded by a salary label
("Salary range:", "Compensation:", ...) over an earlier, unlabeled, in-window figure that happens
to come first in the text -- a company's own boilerplate ("...generating roughly $500K ARR per
employee...") sits inside the believable-salary window just as easily as a real posted range does.

The AssemblyAI fixture below is a trimmed excerpt of the real Commercial Counsel posting text that
surfaced the bug (pulled from hypersignal/data/signals.db, 2026-09-16) -- not synthetic, the exact
shape that broke the old first-match-wins logic.

Run with: python -m unittest tests.test_salary_extraction -v
No network, no API keys required -- pure logic tests, nothing here spends real credits.

extract_salary() returns a 3-tuple, (amount, raw_string, floor_amount) -- the third element was
added 2026-09-19 for the Ramp Calculator fix (it cites the range floor, not the ceiling). These
tests are about which figure wins the *amount* slot, so they unpack and ignore the floor; the
floor's own behavior is exercised through compute_ramp_calc's own coverage.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.company_research import extract_salary

# Trimmed, real excerpt (HTML intact, exactly as HyperSignal stores it) -- boilerplate ARR figure
# early in the body, real labeled salary range much later.
ASSEMBLYAI_COMMERCIAL_COUNSEL = """
<div class="content-intro"><h3><strong>Why AssemblyAI</strong></h3>
<p>AssemblyAI builds the best-in-class Voice AI models powering the next generation of voice
applications.</p>
<p>We are one of the most capital-efficient AI companies on the planet - with under 100 people
generating roughly $500K ARR per employee, we sit among the top 5 most revenue-dense teams within
the fastest-growing AI companies today.</p></div>
<h2><strong>About the role:</strong></h2>
<p>We are looking for a seasoned Commercial Counsel to join as the second hire on our legal
team.</p>
<p>We will consider candidates who may be more or less experienced than outlined in the job
description.</p>
<p>Salary range: $190,000 - $225,000</p>
"""


class TestSalaryLabelPreference(unittest.TestCase):
    def test_real_assemblyai_posting_prefers_labeled_range_over_earlier_arr_figure(self):
        salary, raw, _floor = extract_salary(ASSEMBLYAI_COMMERCIAL_COUNSEL)
        self.assertEqual(salary, 225000, "should take the top of the labeled $190K-$225K range")
        self.assertIn("190,000", raw)
        self.assertIn("225,000", raw)

    def test_unlabeled_arr_figure_alone_is_still_in_window_but_loses_to_a_later_label(self):
        # Sanity check on the fixture itself: $500K is inside the believable-salary window
        # (50k-700k) on its own, so this test would have failed before the fix.
        text = "generating roughly $500K ARR per employee, with no labeled salary anywhere."
        salary, raw, _floor = extract_salary(text)
        self.assertEqual(salary, 500000, "with no label anywhere, old first-match behavior holds")

    def test_labeled_range_wins_even_when_it_comes_after_a_bigger_unlabeled_number(self):
        text = "Our last round raised $600,000,000 in funding. Compensation: $120,000 - $150,000."
        salary, raw, _floor = extract_salary(text)
        self.assertEqual(salary, 150000, "labeled range should win over an earlier unlabeled figure")

    def test_various_label_phrasings_are_recognized(self):
        cases = [
            "Base salary: $140,000 - $160,000", "Pay range: $90,000-$110,000",
            "Base pay for this role is $200,000", "Comp: $175,000 - $195,000",
        ]
        for text in cases:
            with self.subTest(text=text):
                salary, _raw, _floor = extract_salary(text)
                self.assertIsNotNone(salary, f"expected a salary to be found in: {text!r}")

    def test_multi_range_posting_with_no_label_still_takes_the_first_ranges_top(self):
        # Locks in the pre-existing Hightouch-style behavior (see extract_salary's docstring) --
        # this fix must not regress it. Taking the max across zones would read $350K where the
        # NYC band actually tops out at $300K.
        text = "NYC: $200,000 - $300,000. Remote (other US): $250,000 - $350,000."
        salary, raw, _floor = extract_salary(text)
        self.assertEqual(salary, 300000, "first range's top should win, not the max across zones")

    def test_no_plausible_salary_returns_none(self):
        salary, raw, _floor = extract_salary("We are a fast-growing startup building great products.")
        self.assertIsNone(salary)
        self.assertEqual(raw, "")

    def test_empty_text_returns_none(self):
        salary, raw, _floor = extract_salary("")
        self.assertIsNone(salary)
        self.assertEqual(raw, "")


if __name__ == "__main__":
    unittest.main()
