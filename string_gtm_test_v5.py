"""
String API test v5 — GTM relocation-signal scan
------------------------------------------------
Architecture change from v4: stop guessing paths and ATS slugs. Fetch the
homepage once, parse it for the company's actual careers/about links (the
ones already in their nav or footer), and follow those. Only fall back to
guessed paths if no real link is found, and label that clearly as lower
confidence. Blind ATS-slug guessing is removed entirely.

Also: signal is now classified as "real signal" (relocat/visa/must-be-based
language) vs "geography mention only" (bare "new york"/"nyc", which was
inflating the v4 count by catching office-address footers).

companies.csv format:
    company,domain
    Tennr,https://www.tennr.com

Setup:
    pip install requests
    export STRING_API_KEY="sk_live_..."
    python string_gtm_test_v5.py companies.csv
"""

import csv
import json
import os
import re
import sys
from datetime import date
from urllib.parse import urljoin

import requests

API_KEY = os.environ.get("STRING_API_KEY", "sk_live_REPLACE_ME")
FETCH_URL = "https://request.usestring.ai/v1/fetch"

CAREER_LINK_KEYWORDS = ["career", "job", "join us", "join our team", "we're hiring", "open roles", "open positions"]
TEAM_LINK_KEYWORDS = ["about", "team", "who we are", "leadership", "our story"]

# Degraded-confidence fallback only, used if nothing career/team-shaped is in the nav at all
CAREER_PATH_FALLBACK = ["/careers", "/jobs"]
TEAM_PATH_FALLBACK = ["/about", "/team"]

ATS_LINK_PATTERN = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)"
    r"|jobs\.lever\.co/([a-zA-Z0-9_-]+)"
    r"|jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"
)
ATS_PROVIDER_BY_GROUP = ["greenhouse", "lever", "ashby"]

MD_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

STRONG_SIGNAL = re.compile(r"relocat|visa spons|must be based in ny|onsite in ny")
GEO_ONLY = re.compile(r"\bnew york\b|\bnyc\b")


def slugify(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_companies(csv_path):
    companies = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            name = row["company"].strip()
            companies.append({"name": name, "domain": row["domain"].strip(), "slug_guess": slugify(name)})
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


def find_ats_link(text_or_url):
    if not text_or_url:
        return None, None
    m = ATS_LINK_PATTERN.search(text_or_url)
    if not m:
        return None, None
    for i, group in enumerate(m.groups()):
        if group:
            return ATS_PROVIDER_BY_GROUP[i], group
    return None, None


def find_nav_links(markdown_text, base_url, keywords):
    """Pull real [text](url) links out of the page whose text or URL matches
    one of the keywords, resolved to absolute URLs, in page order, deduped."""
    if not markdown_text:
        return []
    candidates, seen = [], set()
    for text, url in MD_LINK_PATTERN.findall(markdown_text):
        haystack = f"{text} {url}".lower()
        if any(kw in haystack for kw in keywords):
            resolved = urljoin(base_url, url.strip())
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)
    return candidates


def fetch_ats_jobs_direct(provider, slug):
    try:
        if provider == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}, timeout=20)
        elif provider == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=20)
        elif provider == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}, timeout=20)
        else:
            return None
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def name_matches(job_data, company_name):
    if not job_data:
        return False
    blob = json.dumps(job_data).lower()
    tokens = [t for t in re.sub(r"[^a-z0-9 ]", "", company_name.lower()).split() if len(t) > 2]
    return any(tok in blob for tok in tokens)


def classify_signal(text):
    if not text:
        return "none"
    lowered = text.lower()
    if STRONG_SIGNAL.search(lowered):
        return "real signal"
    if GEO_ONLY.search(lowered):
        return "geography mention only"
    return "none"


def discover_page(home_text, domain, keywords, path_fallback):
    """Follow a real nav link if one exists; else try a couple of guessed
    paths, flagged as lower confidence."""
    for url in find_nav_links(home_text, domain, keywords):
        provider, slug = find_ats_link(url)
        if provider:
            return url, None, provider, slug, "nav link -> ATS directly"
        content = string_fetch(url)
        if content:
            provider, slug = find_ats_link(content)
            method = "nav link -> page -> ATS link" if provider else "nav link -> page"
            return url, content, provider, slug, method

    for path in path_fallback:
        url = domain.rstrip("/") + path
        content = string_fetch(url)
        if content:
            provider, slug = find_ats_link(content)
            method = "guessed path -> ATS link (low confidence)" if provider else "guessed path (low confidence)"
            return url, content, provider, slug, method

    return None, None, None, None, "not found"


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
        home_text = string_fetch(co["domain"])

        careers_url, careers_text, provider, slug, method = discover_page(
            home_text, co["domain"], CAREER_LINK_KEYWORDS, CAREER_PATH_FALLBACK
        )
        verified = "n/a"
        if provider and "nav link" not in method:
            # only came from a guessed path, not a real nav link -> sanity check it
            job_data = fetch_ats_jobs_direct(provider, slug)
            verified = "name matched" if name_matches(job_data, co["name"]) else "UNVERIFIED"
        else:
            job_data = fetch_ats_jobs_direct(provider, slug) if provider else None

        job_text_blob = json.dumps(job_data) if job_data else (careers_text or "")
        signal = classify_signal(job_text_blob)

        if provider:
            print(f"  ATS: {provider}/{slug} ({method}, verified: {verified})")

        team_url, team_text, _, _, team_method = discover_page(
            home_text, co["domain"], TEAM_LINK_KEYWORDS, TEAM_PATH_FALLBACK
        )

        results.append({
            "company": co["name"],
            "ats_provider": provider or "NOT FOUND",
            "ats_slug": slug or "",
            "discovery_method": method,
            "verified": verified,
            "relocation_signal": signal,
            "team_page_found": team_url or "NOT FOUND",
            "team_page_method": team_method,
            "date_scraped": str(date.today()),
        })

    out_path = "string_test_results_v5.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    total = len(results)
    real_signal = sum(1 for r in results if r["relocation_signal"] == "real signal")
    geo_only = sum(1 for r in results if r["relocation_signal"] == "geography mention only")
    unverified = sum(1 for r in results if r["verified"] == "UNVERIFIED")
    not_found = sum(1 for r in results if r["discovery_method"] == "not found")

    print(f"\n--- Summary ({total} companies) ---")
    print(f"Real relocation signal:     {real_signal}")
    print(f"Geography mention only:     {geo_only}")
    print(f"Unverified ATS matches:     {unverified}")
    print(f"ATS/careers not found:      {not_found}")
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
