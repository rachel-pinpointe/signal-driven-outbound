"""One-off evidence probe: do the 10 known-Ashby companies expose "ashby"
anywhere the current detection code can see it?

Rationale (2026-09-10): tonight's batch reported 0 NYC roles for a set of
companies whose Ashby boards are live and whose slug is simply the company
name. Before hardening ats_belongs_to and allowing bounded slug guessing, we
want documented proof - not assumption - that page inspection genuinely cannot
reach these boards. Every rung the real pipeline uses is exercised here:
plain markdown, JS-rendered markdown, raw HTML, and JS-rendered raw HTML.

Not part of the pipeline. Safe to delete once the fix lands.
"""

import json
import re

from src.providers import FETCH_COUNTS, string_fetch, string_fetch_raw
from src.company_research import CAREER_LINK_KEYWORDS, find_ats_link, find_nav_links

ASHBY = re.compile(r"ashby", re.I)

# slug as confirmed manually by Rachel, 2026-09-10
COMPANIES = [
    ("Valon",            "https://valon.com",             "Valon"),
    ("Sardine",          "https://www.sardine.ai/",       "sardine"),
    ("Graphite",         "https://graphite.com",          "graphite"),
    ("Stedi",            "https://www.stedi.com",         "Stedi"),
    ("TigerData",        "https://www.tigerdata.com/",    "TigerData"),
    ("Venn",             "https://www.venn.com/",         "Venn"),
    ("Truemed",          "https://www.truemed.com/",      "Truemed"),
    ("Spade",            "https://spade.com",             "Spade"),
    ("Normal Computing", "https://normalcomputing.com/",  "NormalComputing"),
    ("Mintlify",         "https://www.mintlify.com/",     "Mintlify"),
]

results = []
for company, homepage, slug in COMPANIES:
    row = {"company": company, "homepage": homepage, "confirmed_slug": slug}

    home = string_fetch(homepage)
    row["homepage_chars"] = len(home or "")
    navs = find_nav_links(home or "", homepage, CAREER_LINK_KEYWORDS)
    row["nav_links"] = navs[:4]
    # Same candidate order the pipeline uses: real nav links first, then guess.
    careers = navs[0] if navs else homepage.rstrip("/") + "/careers"
    row["careers_url"] = careers

    plain = string_fetch(careers)
    rendered = string_fetch(careers, execute_js=True)
    raw = string_fetch_raw(careers)
    raw_js = string_fetch_raw(careers, execute_js=True)

    for label, body in (("plain_md", plain), ("rendered_md", rendered),
                        ("raw_html", raw), ("raw_html_js", raw_js)):
        row[label + "_chars"] = len(body or "")
        row[label + "_has_ashby"] = bool(ASHBY.search(body or ""))

    row["find_ats_link_plain"] = find_ats_link(plain)[0]
    row["find_ats_link_rendered"] = find_ats_link(rendered)[0]
    row["homepage_has_ashby"] = bool(ASHBY.search(home or ""))
    row["ANY_ashby_anywhere"] = any(
        row[k + "_has_ashby"] for k in ("plain_md", "rendered_md", "raw_html", "raw_html_js")
    ) or row["homepage_has_ashby"]

    results.append(row)
    print("%-18s careers=%-52s ashby_anywhere=%s  (md %d / rendered %d / raw %d / raw+js %d)" % (
        company, careers[:52], row["ANY_ashby_anywhere"],
        row["plain_md_chars"], row["rendered_md_chars"],
        row["raw_html_chars"], row["raw_html_js_chars"]), flush=True)

json.dump(results, open("/tmp/detection_gap_evidence.json", "w"), indent=1)
found = [r["company"] for r in results if r["ANY_ashby_anywhere"]]
print("\n=== VERDICT ===")
print("companies where 'ashby' appears somewhere reachable: %s" % (found or "NONE"))
print("companies genuinely undiscoverable by page inspection: %d of %d" % (
    len(results) - len(found), len(results)))
print("fetches used: %s" % dict(FETCH_COUNTS))
