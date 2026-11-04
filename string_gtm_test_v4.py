"""
String API test v4 — GTM relocation-signal scan
------------------------------------------------
Change from v3: company list now comes from companies.csv instead of being
hardcoded, so scaling to 20-30+ companies doesn't mean hand-editing the
script. Slug guesses are derived automatically from the company name.

companies.csv format (see companies_template.csv):
    company,domain
    Tennr,https://www.tennr.com
    Clay,https://www.clay.com

Setup:
    pip install requests
    export STRING_API_KEY="sk_live_..."
    python string_gtm_test_v4.py companies.csv
"""

import csv
import json
import os
import re
import sys
from datetime import date

import requests

API_KEY = os.environ.get("STRING_API_KEY", "sk_live_REPLACE_ME")
FETCH_URL = "https://request.usestring.ai/v1/fetch"

CAREER_PATHS = ["/careers", "/jobs", "/careers/", "/join-us"]
TEAM_PATHS = ["/about", "/team", "/company", "/about-us"]

ATS_LINK_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
}

RELOCATION_KEYWORDS = [
    "relocat", "visa sponsor", "new york", "nyc",
    "must be based in ny", "onsite in ny", "willing to relocate",
]


def slugify(name):
    """Best-guess ATS slug from a company name: lowercase, strip anything
    that isn't a letter or digit. 'Poly AI' -> 'polyai'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_companies(csv_path):
    companies = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            name = row["company"].strip()
            companies.append({
                "name": name,
                "domain": row["domain"].strip(),
                "slug_guess": slugify(name),
            })
    return companies


def string_fetch(url):
    try:
        r = requests.post(
            FETCH_URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"url": url, "format": "markdown"},
            timeout=30,
        )
        origin_status = int(r.headers.get("X-Status-Code", r.status_code))
        if origin_status < 400 and r.text:
            return r.text
        print(f"  x {url} -> origin status {origin_status}")
        return None
    except requests.RequestException as e:
        print(f"  x {url} -> error: {e}")
        return None


def best_of(base_url, paths):
    best_url, best_content = None, ""
    for path in paths:
        url = base_url.rstrip("/") + path
        content = string_fetch(url)
        if content and len(content) > len(best_content):
            best_url, best_content = url, content
    if best_url:
        print(f"  ok {best_url} ({len(best_content)} chars)")
    return best_url, best_content or None


def find_ats_link(markdown_text):
    if not markdown_text:
        return None, None
    for provider, pattern in ATS_LINK_PATTERNS.items():
        m = pattern.search(markdown_text)
        if m:
            return provider, m.group(1)
    return None, None


def fetch_ats_jobs_direct(provider, slug):
    try:
        if provider == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                              params={"content": "true"}, timeout=20)
        elif provider == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}",
                              params={"mode": "json"}, timeout=20)
        elif provider == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                              params={"includeCompensation": "true"}, timeout=20)
        else:
            return None
        if r.status_code == 200:
            return r.json()
        return None
    except requests.RequestException:
        return None


def scan_for_signal(text):
    if not text:
        return []
    lowered = text.lower()
    return [kw for kw in RELOCATION_KEYWORDS if kw in lowered]


def main():
    if API_KEY == "sk_live_REPLACE_ME":
        print("Set STRING_API_KEY before running.")
        return

    csv_path = sys.argv[1] if len(sys.argv) > 1 else "companies.csv"
    companies = load_companies(csv_path)
    print(f"Loaded {len(companies)} companies from {csv_path}")

    results = []
    for co in companies:
        print(f"\n== {co['name']} ==")

        careers_url, careers_text = best_of(co["domain"], CAREER_PATHS)

        provider, slug = find_ats_link(careers_text)
        discovery_method = "extracted from page" if provider else None

        if not provider:
            for guess_provider in ("greenhouse", "lever", "ashby"):
                data = fetch_ats_jobs_direct(guess_provider, co["slug_guess"])
                if data:
                    provider, slug, discovery_method = guess_provider, co["slug_guess"], "slug guess"
                    break

        job_data = fetch_ats_jobs_direct(provider, slug) if provider else None
        job_text_blob = json.dumps(job_data) if job_data else (careers_text or "")
        signal_hits = scan_for_signal(job_text_blob)

        if provider:
            print(f"  ATS: {provider}/{slug} ({discovery_method})")

        team_url, team_text = best_of(co["domain"], TEAM_PATHS)

        results.append({
            "company": co["name"],
            "ats_provider": provider or "NOT FOUND",
            "ats_slug": slug or "",
            "discovery_method": discovery_method or "none",
            "relocation_signal": ", ".join(signal_hits) if signal_hits else "none",
            "team_page_found": team_url or "NOT FOUND",
            "team_page_chars": len(team_text) if team_text else 0,
            "date_scraped": str(date.today()),
        })

    out_path = "string_test_results_v4.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    # Summary
    total = len(results)
    extracted = sum(1 for r in results if r["discovery_method"] == "extracted from page")
    guessed = sum(1 for r in results if r["discovery_method"] == "slug guess")
    not_found = sum(1 for r in results if r["discovery_method"] == "none")
    with_signal = sum(1 for r in results if r["relocation_signal"] != "none")
    with_team = sum(1 for r in results if r["team_page_found"] != "NOT FOUND")

    print(f"\n--- Summary ({total} companies) ---")
    print(f"ATS found via link on page: {extracted}")
    print(f"ATS found via slug guess:   {guessed}")
    print(f"ATS not found:              {not_found}")
    print(f"Relocation signal found:    {with_signal}")
    print(f"Team page found:            {with_team}")
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
