"""Company research (Layer 1): careers-page discovery, ATS resolution,
NYC role detection, and salary banding.

Goal: find a company's real open NYC roles and salary so downstream
messaging cites an actual number instead of a generic default. This module
is persona-agnostic -- it's used by every play that needs to know whether a
company has NYC hiring signal, not just the CFO play. Persona-specific
contact-finding lives one layer up (e.g. cfo_enrich.py for the CFO play).

Discovery follows the company's own site, per the Company Discovery
Workflow in CLAUDE.md: fetch the homepage, read the nav for a real careers
link, follow it. The nav-link parsing and the ATS provider APIs are carried
over from string_gtm_test_v5.py.

Slug guessing is the LAST rung, not the first (see guess_ats_slug). It was
forbidden entirely until 2026-09-10, when ats_belongs_to was rebuilt to stop
failing open on namesake slugs. Page inspection is still always tried first.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from . import cache
from .providers import (
    MissingKey, _llm_extract, _llm_web_search, _parse_json_response, _provider_error,
    exa_search, jina_fetch, string_fetch, string_fetch_escalating, string_fetch_raw,
    verify_excerpt_on_page,
)

CAREER_LINK_KEYWORDS = [
    "career", "job", "join us", "join our team", "we're hiring",
    "open roles", "open positions", "work with us", "hiring",
]

# Only used when the homepage exposes no careers link at all. Flagged as
# lower confidence in the output.
CAREER_PATH_FALLBACK = ["/careers", "/jobs", "/join-us", "/company/careers"]

# Phase 5 (2026-09-16): HyperSignal's own daily scanner (see the Phase 4 scope
# doc) already holds fresh, structured job data for the watchlist companies in
# hypersignal/data/signals.db. When it has fresh data for a domain, use it
# directly instead of re-fetching and re-parsing the careers page -- real
# structured rows beat a markdown scrape of the same information. Real limit:
# only covers the ~20 companies on HyperSignal's watchlist today, not the
# full company list (see the tracker's Section 5 company-intake item for
# widening this in a later phase). Schema confirmed against the live db
# 2026-09-16 via python3's sqlite3 module (no sqlite3 CLI on this Mac):
# raw_jobs(id, source, external_id, title, company, location, state, remote,
# url, posted_at, seen_at, raw, origin_key, domain, status, last_seen_at,
# expires_at, closed_at, dedup_key, description, comp_max, currency, starred,
# country).
HYPERSIGNAL_DB_PATH = Path(__file__).resolve().parent.parent / "hypersignal" / "data" / "signals.db"
# HyperSignal's scanner runs on a weekday 1pm ET schedule (Phase 4), so a
# domain that's been quiet for a few days is still worth trusting; a week of
# silence means either the domain fell off the watchlist or the scanner has
# been down, and it's safer to fall through to a live fetch than serve
# week-old data as current.
HYPERSIGNAL_FRESHNESS_DAYS = 7


def _hypersignal_row_is_fresh(last_seen_at: str) -> bool:
    if not last_seen_at:
        return False
    try:
        seen = datetime.strptime(last_seen_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    # last_seen_at is stored in the scanner's local (ET) time, not UTC, based
    # on the sample timestamps lining up with the 1pm ET cron. Not corrected
    # for here: the freshness window only needs day-level precision, and a
    # few hours of offset is well inside it.
    return (datetime.utcnow() - seen).days < HYPERSIGNAL_FRESHNESS_DAYS


def _jobs_from_hypersignal(domain: str) -> Optional[List[Dict[str, Any]]]:
    """Check HyperSignal's local signals.db for this domain before falling
    through to jina/String. Returns None (fall through to live discovery)
    when the domain isn't on the watchlist, the db file isn't present on
    this machine, or its data has gone stale; returns a list -- possibly
    empty, a confirmed zero rather than a parse failure -- when fresh data
    exists. Shaped like _normalise_jobs()'s output so it drops straight into
    the same downstream NYC-matching/salary-extraction code; NOT pushed
    through _jobs_from_page_text (that parser's contract concatenates every
    line after the title into `location`, which a full job description
    would corrupt).
    """
    if not HYPERSIGNAL_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(HYPERSIGNAL_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT title, location, state, remote, description, last_seen_at "
            "FROM raw_jobs WHERE domain = ? AND status = 'open'",
            (domain,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    freshest = max((r.get("last_seen_at") or "" for r in rows), default="")
    if not _hypersignal_row_is_fresh(freshest):
        return None
    jobs = []
    for r in rows:
        loc = (r.get("location") or "").strip()
        if r.get("state"):
            loc = (loc + ", " + r["state"]).strip(", ")
        jobs.append({
            "title": (r.get("title") or "").strip(),
            "location": loc,
            "body": r.get("description") or "",
            "is_remote": bool(r.get("remote")),
        })
    return jobs


MD_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# Image alt text routinely contains words like "jobs" or "hiring", so CDN asset
# URLs match the careers keywords and crowd out the real careers link. Clay's
# homepage yielded four .avif/.png links before /careers.
ASSET_EXT = re.compile(
    r"\.(?:png|jpe?g|avif|webp|svg|gif|ico|pdf|mp4|webm|zip|css|js|woff2?|ttf)(?:\?|$)", re.I)

# Markers proving the company uses an ATS even when no board URL is present -
# a client-side embed div/script. Provider is established from the company's
# own page; the slug is then resolved and verified, never blind-probed.
ATS_EMBED_MARKERS = [
    ("ashby", re.compile(r"ashby_embed|ashbyhq", re.I)),
    ("greenhouse", re.compile(r"grnhse|greenhouse\.io|greenhouse_?embed", re.I)),
    ("lever", re.compile(r"lever\.co|leverjobs", re.I)),
]

# Exact slug extraction from raw HTML, in confidence order.
ATS_SLUG_IN_HTML = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"posting-api/job-board/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I)),
]

ATS_LINK_PATTERN = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)"
    r"|jobs\.lever\.co/([a-zA-Z0-9_-]+)"
    r"|jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"
)
ATS_PROVIDER_BY_GROUP = ["greenhouse", "lever", "ashby"]

# NYC match. Audited and widened 2026-09-10 after Cityblock's "Bronx MIC
# Touchdown (NY)" role was missed: the old pattern had Manhattan and Brooklyn
# but not the other three boroughs.
#
# Covered: "New York" (which also covers "New York City" and "New York, NY"),
# NYC and N.Y.C., all five boroughs, Long Island City, and "New York metro".
#
# Still deliberately EXCLUDED, per the original reasoning: a bare or
# comma-prefixed state code. ", NY" looks tempting but matches "Albany, NY" and
# "Buffalo, NY", which are New York State and not this market. "New York, NY"
# already matches on the city half, so the state code adds nothing but risk.
NYC_PATTERN = re.compile(
    r"\bnew york\b"
    r"|\bnyc\b|\bn\.y\.c\.?\b"
    r"|\bmanhattan\b|\bbrooklyn\b|\bbronx\b|\bqueens\b|\bstaten island\b"
    r"|\blong island city\b"
    r"|\bnew york metro\b",
    re.I)

# A role only counts as remote-alternative if the posting ITSELF offers remote.
# A separate remote role elsewhere on the same board is irrelevant - that was
# the refinement Rachel specified on 2026-09-10.
ROLE_REMOTE_PATTERN = re.compile(
    r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b|\bdistributed\b", re.I)

# Qualification floor. A company is disqualified on comp only when EVERY open
# NYC role is priced and every one of them is under this number. An unpriced
# role cannot establish "all under floor", so it never triggers disqualification.
MIN_SALARY_FLOOR = 150_000

# Relocation signal tiers, per CLAUDE.md. Tier 1 beats Tier 2; record which.
TIER1_PATTERN = re.compile(
    r"relocat\w*|visa spons\w*|immigration support|work authorization spons\w*", re.I)
TIER2_PATTERN = re.compile(
    r"must be (?:based|located) in|based in new york|in[- ]person role|onsite \d+ days"
    r"|in[- ]office \d+ days|hybrid in new york|this is an in[- ]person", re.I)

# Entry-level disqualifier (CLAUDE.md): these don't carry a relocation budget,
# so they must not be the *driving* NYC signal for an account.
#
# "Associate" is deliberately NOT in this list. Unlike the others, it spans
# both genuinely entry-level roles (coordinator-tier, research associate)
# and senior ones -- "Associate Director", "Associate General Counsel" can
# mean $200K+, one rung below VP at plenty of firms. A posted salary is
# stronger evidence than that one word, so it gets its own check in
# is_entry_level() below, where a known salary above the ceiling overrides
# the keyword instead of the keyword short-circuiting past the salary.
# Real cases this fixes (CLAUDE.md Known Issues): "Associate Director" and
# "Associate General Counsel" at $260K were both being tagged entry-level
# on the word alone, regardless of posted comp.
ENTRY_LEVEL_PATTERN = re.compile(
    r"\b(sdr|bdr|sales development|business development rep\w*|intern|internship"
    r"|coordinator|junior|entry[- ]level|apprentice|fellow)\b", re.I)
ASSOCIATE_PATTERN = re.compile(r"\bassociate\b", re.I)
ENTRY_LEVEL_BASE_CEILING = 150_000

# On-target earnings bundle base plus commission. Quoting an OTE figure to a
# CFO as though it were a base salary is wrong on the facts and trivially
# dismissed, so these roles are only used as the driving number when nothing
# else is open.
OTE_PATTERN = re.compile(
    r"\bOTE\b|on[- ]target earnings|on target earnings|base \+ commission"
    r"|uncapped commission|variable compensation|commission plan", re.I)

# Research and Stats role bands - used only when no salary is posted.
ROLE_BANDS = [
    ("Software Engineer", 165_000, re.compile(
        r"engineer|developer|swe|programmer|architect|scientist|technical staff|infrastructure|devops|sre", re.I)),
    ("Finance Lead", 152_000, re.compile(
        r"finance|accounting|accountant|controller|fp&a|treasury|audit", re.I)),
    ("GTM Lead", 111_000, re.compile(r"\bgtm\b|\bsales\b|account executive", re.I)),
    ("Chief of Staff", 157_500, re.compile(
        r"chief of staff|business operations|bizops|strategy|operations manager|program manager|product manager", re.I)),
    ("GTM Lead", 111_000, re.compile(
        r"sales|account executive|account manager|marketing|growth|revenue|partnership"
        r"|customer success|business development|gtm|demand gen|recruit|people|talent", re.I)),
]
DEFAULT_BAND = ("Software Engineer", 165_000)

SALARY_PATTERN = re.compile(
    r"\$\s?(\d{2,3})(?:,(\d{3}))?(?:\s?[kK])?\b(?:\s?(?:-|–|—|to)\s?\$?\s?(\d{2,3})(?:,(\d{3}))?(?:\s?[kK])?)?")


def _norm_amount(whole: str, thousands: Optional[str]) -> Optional[int]:
    """Turn a regex salary capture into a plain integer of dollars."""
    if thousands:
        return int(whole + thousands)
    n = int(whole)
    # A bare 2-3 digit number in a salary context is thousands ($165K).
    return n * 1000 if n < 1000 else n


# Words that mark a dollar figure as an actual pay figure, not ARR/funding/equity mentioned
# nearby in the same posting. Checked in a window immediately before each in-window candidate
# (extract_salary's SALARY_CONTEXT_WINDOW) -- real postings label the number close by
# ("Salary range: $190,000 - $225,000", confirmed against AssemblyAI's real posting text,
# 2026-09-16), so a short window is enough and avoids matching a label that describes a
# different number entirely earlier in a long body.
SALARY_CONTEXT_PATTERN = re.compile(
    r"\b(?:salary|compensation|comp\.?|pay\s*range|pay\s*band|base\s*pay|base\s*salary)\b",
    re.IGNORECASE)
SALARY_CONTEXT_WINDOW = 100  # chars of text immediately before a candidate to check for a label


def extract_salary(text: str) -> Tuple[Optional[int], str, Optional[int]]:
    """Pull the posted base salary out of prose. Returns (amount, raw_string, floor_amount).

    floor_amount is the low end of the same matched range (equal to amount when the posting
    gives a single number, not a range) -- added 2026-09-19 for the Ramp Calculator fix, which
    cites the floor rather than the ceiling as the harder-to-dispute number in a CFO email.

    Prefers the FIRST in-window match that's immediately preceded by a salary label
    (SALARY_CONTEXT_PATTERN, within SALARY_CONTEXT_WINDOW chars) over an earlier unlabeled one.
    Falls back to the first in-window match with no label anywhere in the text, same as before
    this fix -- most postings never label their range at all, and that majority case must keep
    behaving exactly as it did.

    Values outside a believable base-salary window are ignored so equity grants, ARR figures and
    funding totals in the company blurb are not read as comp -- but that alone isn't enough: a
    boilerplate line like "...generating roughly $500K ARR per employee..." can still land inside
    the window and, without a label check, wins just by appearing first in the text (AssemblyAI,
    found 2026-09-16 during Phase 5 -- see CLAUDE.md Known Issues; its Commercial Counsel role's
    real range, "Salary range: $190,000 - $225,000", is labeled and now correctly wins instead).

    Also takes the top of the *first* plausible (labeled, or else in-window) range in the body,
    not the largest number in it. Postings routinely list several ranges - multiple location
    zones, or a second range for a more senior level - and taking the maximum quotes a number the
    role does not actually pay. Hightouch posts per-zone bands; the max read $350K where the NYC
    band tops out at $300K.
    """
    if not text:
        return None, "", None

    first_unlabeled = None
    for m in SALARY_PATTERN.finditer(text):
        lo = _norm_amount(m.group(1), m.group(2))
        hi = _norm_amount(m.group(3), m.group(4)) if m.group(3) else None
        candidate = hi or lo
        if candidate is None or not (50_000 <= candidate <= 700_000):
            continue

        if first_unlabeled is None:
            first_unlabeled = (candidate, m.group(0).strip(), lo)

        context_start = max(0, m.start() - SALARY_CONTEXT_WINDOW)
        if SALARY_CONTEXT_PATTERN.search(text[context_start:m.start()]):
            return candidate, m.group(0).strip(), lo

    return first_unlabeled or (None, "", None)


def map_role_band(title: str) -> Tuple[str, int]:
    for name, base, pattern in ROLE_BANDS:
        if pattern.search(title or ""):
            return name, base
    return DEFAULT_BAND


# Ramp Calculator (Pinpointe Group x YOC Ramp Calculator.xlsx, the Sales & Marketing claude.ai
# Project file the CFO play's Year-1 net-value figures come from) -- reproduced exactly here,
# cell-by-cell verified 2026-09-19 against model!D3:D20/F19/F20 and data!/citations! sheets, so
# the CFO play's "run it through the Ramp Calculator" instruction has a real number behind it for
# whatever salary was actually found, instead of the LLM reciting the one worked example already
# in the play text (found 2026-09-19: Email 2's net-value figures never changed with the real
# discovered salary, because no calculation existed anywhere in this codebase -- see
# claude/nycbound-gtm-phase12-apollo-hypersignal-scope.md). Each role's "expected value add"
# factor is the calculator's own fixed, cited multiple of that role's all-in cost (SWE:
# $892,000 revenue-per-engineer (getdx.com) / $165,167 baseline salary; GTM Lead and Chief of
# Staff/Finance Lead factors are cited/estimated directly in the calculator, not derived) --
# change it in the calculator first, then here, if Rachel updates it there.
RAMP_CALC_VALUE_ADD_FACTOR = {
    "Software Engineer": 5.400594549758729,
    "GTM Lead": 5.0,
    "Chief of Staff": 3.0,
    "Finance Lead": 3.0,
}
RAMP_CALC_RELO_BONUS = 10_000  # data!B3:E3, cited (levels.fyi), flat across all four roles
RAMP_CALC_BENEFITS_TAX_RATE = 0.25  # data!B4:E4
RAMP_CALC_DEFAULT_ROLE = "Software Engineer"
RAMP_CALC_DEFAULT_SALARY = 165_167  # citations!C2 -- the calculator's own default, matches the
                                     # $952,547/$835,647/$116,901 figures already cited in
                                     # fibery_docs/research_and_stats.md
RAMP_CALC_RELO_RAMP_MONTH12 = 0.9   # data! "Relo Ramp Month 12" -- same for all four roles
RAMP_CALC_RELO_FAILURE_MONTH12 = 0.4  # data! "Relo + Not Work Out1 Month 12" -- same for all four
RAMP_CALC_FAILURE_RATE = 0.40         # citations!C7 (Jobera) -- the "40% of DIY relocations fail"
                                       # figure Email 2 escalates with


def _shorthand_dollars(n: float) -> str:
    """Rounds a dollar figure to the shorthand style Rachel's copy always uses ($840K, $1.7M --
    never a raw exact figure like $835,647.19) -- 2026-09-19: handed the LLM exact computed
    integers and told it to "use them as fact," so naturally it did, verbatim, digit for digit,
    even though the play's own footnote already said never to write numbers out in full. Rounding
    here, in code, means there is no instruction for the model to half-follow -- it only ever
    sees the already-clean string."""
    n = abs(round(n))
    if n >= 1_000_000:
        tenths = round(n / 100_000) / 10
        return "${}M".format(int(tenths)) if tenths == int(tenths) else "${:.1f}M".format(tenths)
    return "${}K".format(int(round(n / 1000)))


def compute_ramp_calc(salary: Optional[int], role_name: Optional[str] = None) -> Dict[str, Any]:
    """Year-1 net value if a hire works out locally, if they DIY-relocate and ramp normally, and
    if that DIY relocation fails outright (~40% of them, per Jobera) -- the figures the CFO
    play's Email 2 (2026-09-19 rewrite) cites: local-hire value, DIY-relo value, the gap between
    them, and the failure-adjusted value. Falls back to the $165,167 SWE base case (the
    calculator's own default) when no real salary was found, exactly reproducing the existing
    default figures rather than a new/different set. Every dollar figure is returned twice: the
    exact int (for anything that needs the real number) and an "_fmt" shorthand string ($840K /
    $1.7M) that is what should actually go in front of the LLM -- see _shorthand_dollars."""
    used_default = not salary
    effective_salary = salary if salary else RAMP_CALC_DEFAULT_SALARY
    factor = RAMP_CALC_VALUE_ADD_FACTOR.get(role_name or "",
                                            RAMP_CALC_VALUE_ADD_FACTOR[RAMP_CALC_DEFAULT_ROLE])
    cost = effective_salary * (1 + RAMP_CALC_BENEFITS_TAX_RATE) + RAMP_CALC_RELO_BONUS
    value_add = cost * factor
    normal_ramp_year1 = value_add - cost
    relo_ramp_year1 = RAMP_CALC_RELO_RAMP_MONTH12 * value_add - cost
    relo_failure_year1 = RAMP_CALC_RELO_FAILURE_MONTH12 * value_add - cost
    gap = normal_ramp_year1 - relo_ramp_year1
    salary_used = int(round(effective_salary))
    return {
        "salary_used": salary_used,
        "salary_used_fmt": _shorthand_dollars(salary_used),
        "used_default": used_default,
        "role_name": role_name or RAMP_CALC_DEFAULT_ROLE,
        "normal_ramp_year1": int(round(normal_ramp_year1)),
        "normal_ramp_year1_fmt": _shorthand_dollars(normal_ramp_year1),
        "relo_ramp_year1": int(round(relo_ramp_year1)),
        "relo_ramp_year1_fmt": _shorthand_dollars(relo_ramp_year1),
        "gap": int(round(gap)),
        "gap_fmt": _shorthand_dollars(gap),
        "relo_failure_year1": int(round(relo_failure_year1)),
        "relo_failure_year1_fmt": _shorthand_dollars(relo_failure_year1),
        "failure_rate_pct": int(round(RAMP_CALC_FAILURE_RATE * 100)),
    }


def is_entry_level(title: str, salary: Optional[int]) -> bool:
    t = title or ""
    if ENTRY_LEVEL_PATTERN.search(t):
        return True
    if ASSOCIATE_PATTERN.search(t):
        # Salary ceiling outranks the keyword match (the 2026-09-10 bug):
        # a known salary at or above the floor means this "associate" is
        # the senior kind, whatever the word alone would suggest.
        if salary is not None and salary >= ENTRY_LEVEL_BASE_CEILING:
            return False
        return True
    return salary is not None and salary < ENTRY_LEVEL_BASE_CEILING


def _norm_url(u: str) -> str:
    """Compare URLs ignoring scheme, www, trailing slash and fragment."""
    p = urlparse((u or "").strip())
    host = (p.netloc or "").lower().replace("www.", "")
    path = (p.path or "/").rstrip("/") or "/"
    return host + path


def find_nav_links(markdown_text: Optional[str], base_url: str, keywords: List[str]) -> List[str]:
    """Real [text](url) links whose text or href matches a keyword, absolutised,
    in page order, deduped. Carried over from string_gtm_test_v5."""
    if not markdown_text:
        return []
    base_norm = _norm_url(base_url)
    candidates, seen = [], set()
    for text, url in MD_LINK_PATTERN.findall(markdown_text):
        href = url.strip()
        # A fragment-only or JS href resolves straight back to the current page.
        # Following it re-fetches the homepage and finds no jobs, which reads as
        # a false "no signal". Skip those.
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        haystack = (text + " " + href).lower()
        if not any(kw in haystack for kw in keywords):
            continue
        resolved = urljoin(base_url, href)
        norm = _norm_url(resolved)
        if not resolved.startswith("http") or norm == base_norm or norm in seen:
            continue
        if ASSET_EXT.search(urlparse(resolved).path or ""):
            continue
        seen.add(norm)
        candidates.append(resolved)
    # A keyword in the URL path is far stronger evidence than one in link text.
    def _score(u):
        path = (urlparse(u).path or "").lower()
        host = (urlparse(u).netloc or "").lower()
        if any(k in host for k in ("greenhouse", "lever", "ashby")):
            return 0
        segments = [seg for seg in path.split("/") if seg]
        last = segments[-1] if segments else ""
        # A careers page lives at a shallow path. "hiring" deep in a URL is
        # usually a blog post: Connecteam's
        # /hr-people-management/hiring-and-onboarding/ is content marketing,
        # not their job board.
        if last in ("careers", "career", "jobs", "job", "join-us", "join", "openings", "open-roles"):
            return 1
        if any(k in host for k in ("career", "job")):
            return 1
        if len(segments) <= 2 and any(
                k in path for k in ("career", "job", "join", "position", "opening")):
            return 2
        if any(k in path for k in ("career", "job", "join", "hiring", "position", "opening")):
            return 4
        return 3
    candidates.sort(key=_score)
    return candidates


def find_ats_link(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    m = ATS_LINK_PATTERN.search(text)
    if not m:
        return None, None
    for i, group in enumerate(m.groups()):
        if group:
            return ATS_PROVIDER_BY_GROUP[i], group
    return None, None


def fetch_ats_jobs(provider: str, slug: str, context: str = "",
                   ledger: Any = None, quiet: bool = False) -> Optional[Any]:
    """Hit the ATS provider's public API directly - free, and returns full job
    bodies rather than rendered page text.

    `quiet` suppresses the warning for speculative slug probes, where a 404 is
    the expected answer most of the time. A confirmed slug that 404s is a real
    fault and must be loud: GlossGenius's wrong Greenhouse slug returned
    `{"error":"Job not found"}` and the silent None made it indistinguishable
    from a company with an empty board.
    """
    try:
        if provider == "greenhouse":
            r = requests.get(
                "https://boards-api.greenhouse.io/v1/boards/{}/jobs".format(slug),
                params={"content": "true"}, timeout=25)
        elif provider == "lever":
            r = requests.get("https://api.lever.co/v0/postings/{}".format(slug),
                             params={"mode": "json"}, timeout=25)
        elif provider == "ashby":
            r = requests.get(
                "https://api.ashbyhq.com/posting-api/job-board/{}".format(slug),
                params={"includeCompensation": "true"}, timeout=25)
        else:
            if not quiet:
                _provider_error(ledger, "ats", context or slug,
                                "unknown provider {!r}".format(provider))
            return None
        if r.status_code == 200:
            return r.json()
        if not quiet:
            _provider_error(ledger, "ats/" + provider, context or slug,
                            "HTTP {} for slug {!r}: {}".format(
                                r.status_code, slug, r.text[:100]))
        return None
    except (requests.RequestException, ValueError) as exc:
        if not quiet:
            _provider_error(ledger, "ats/" + provider, context or slug,
                            "request failed for slug {!r}: {}".format(slug, exc))
        return None


# Stripped before tokenising a company name. "Truemed.com" tokenised to
# "truemedcom", which appears nowhere in Truemed's own board, so the correct
# board was rejected. See CLAUDE.md.
_TLD_SUFFIX = re.compile(
    r"\.(?:com|io|ai|co|net|org|dev|app|xyz|tech|inc|so|sh|gg|us|uk|health|security)$", re.I)

# Hosts that say nothing about board ownership, so their presence must not be
# read as "this board belongs to some other company". Matched as exact
# registrable names, NOT as substrings: an earlier regex version had "x" as an
# alternative, which matched inside "on(yx.)app" and let ashby/onyx through.
_GENERIC_HOST_NAMES = {
    "github.com", "gitlab.com", "x.com", "twitter.com", "linkedin.com",
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "medium.com",
    "notion.so", "google.com", "gle.com", "forms.gle", "bit.ly", "calendly.com",
    "loom.com", "bill.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "ashbyprd.com", "workday.com", "myworkdayjobs.com", "gem.com", "comeet.com",
}


def _is_generic_host(host: str) -> bool:
    h = (host or "").lower().strip(".").replace("www.", "")
    if h in _GENERIC_HOST_NAMES:
        return True
    parts = h.split(".")
    registrable = ".".join(parts[-2:]) if len(parts) >= 2 else h
    return registrable in _GENERIC_HOST_NAMES

_HOST_IN_URL = re.compile(r"https?://([a-z0-9\-\.]+)", re.I)


def company_name_tokens(company_name: str) -> List[str]:
    """Tokens for name matching, with any TLD-looking suffix removed first."""
    name = _TLD_SUFFIX.sub("", (company_name or "").strip().lower())
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", name).split() if len(t) > 2]


def ats_belongs_to(job_data: Any, company_name: str,
                   company_domain: Optional[str] = None) -> bool:
    """Guard against namesake slug collisions (ashby/onyx is a different Onyx).

    Rebuilt 2026-09-10. The old version accepted any company-name token longer
    than 2 chars appearing anywhere in the board JSON, which both failed OPEN
    (ashby/onyx verified True for "Onyx Security") and failed CLOSED
    ("Truemed.com" rejected its own board). Job descriptions are prose, so a
    single token match proves nothing.

    Two pieces of evidence, either sufficient:
      1. The board cites the company's own registrable domain.
      2. The company's FULL name appears as a contiguous phrase.

    Requiring the full phrase is what kills the namesake: "Onyx Security" never
    appears on ashby/onyx, which belongs to an open-source project simply called
    Onyx, even though both the tokens "onyx" and "security" occur somewhere in
    its job prose. Single-word companies reduce to a plain token match, which is
    the best available signal for them.

    A third-party-domain heuristic was tried first and rejected: Graphite's
    board cites cursor.com (a partner), which is not evidence of foreign
    ownership, and using it produced a false negative.
    """
    if not job_data:
        return False
    blob = json.dumps(job_data).lower()
    tokens = company_name_tokens(company_name)
    if not tokens:
        return True

    if company_domain:
        domain = _norm_url(company_domain).split("/")[0].lower().replace("www.", "")
        if domain and domain in blob:
            return True

    return " ".join(tokens) in blob


def _ashby_salary(job: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], str]:
    """Read Ashby's structured compensation instead of regexing its prose.

    Only annual Salary components count: an EquityPercentage component carries
    no dollar value, and an hourly rate read as a base salary is nonsense.
    """
    comp = job.get("compensation") or {}
    for component in (comp.get("summaryComponents") or []):
        # USD only: a GBP band read as dollars both misstates the number and
        # trips the entry-level test (Coder's GBP 96K-130K London role looked
        # like a sub-150K entry-level NYC role).
        if (component.get("compensationType") == "Salary"
                and (component.get("currencyCode") or "USD").upper() == "USD"
                and (component.get("interval") or "").upper() in ("1 YEAR", "YEAR", "ANNUAL")):
            lo, hi = component.get("minValue"), component.get("maxValue")
            if lo or hi:
                raw = comp.get("scrapeableCompensationSalarySummary") or ""
                return lo, hi, raw.strip()
    return None, None, ""


def _normalise_jobs(provider: str, data: Any) -> List[Dict[str, str]]:
    """Flatten each ATS's payload into {title, location, body}."""
    out = []
    if provider == "greenhouse":
        for j in (data or {}).get("jobs", []):
            out.append({
                "title": j.get("title") or "",
                "location": ((j.get("location") or {}).get("name") or ""),
                "body": j.get("content") or "",
            })
    elif provider == "lever":
        for j in (data or []):
            cats = j.get("categories") or {}
            out.append({
                "title": j.get("text") or "",
                "location": cats.get("location") or "",
                "body": (j.get("descriptionPlain") or j.get("description") or "")
                        + " " + json.dumps(j.get("lists") or []),
            })
    elif provider == "ashby":
        for j in (data or {}).get("jobs", []):
            lo, hi, raw = _ashby_salary(j)
            locations = [j.get("location") or ""] + list(j.get("secondaryLocations") or [])
            out.append({
                "title": j.get("title") or "",
                "location": ", ".join(x for x in (
                    l if isinstance(l, str) else (l or {}).get("location", "")
                    for l in locations) if x),
                "body": (j.get("descriptionPlain") or j.get("description") or ""),
                "salary_min": lo,
                "salary_max": hi,
                "salary_raw": raw or (("${:,}".format(int(hi))) if hi else ""),
                # workplaceType is the trustworthy field. Ashby sets
                # isRemote=True on Hybrid roles too: every GlossGenius NYC
                # posting is location 'NYC', workplaceType 'Hybrid',
                # isRemote True. Keying off isRemote disqualified exactly the
                # hybrid-NYC roles this play most wants.
                "is_remote": ((j.get("workplaceType") or "").strip().lower() == "remote"
                              if j.get("workplaceType") else bool(j.get("isRemote"))),
                "workplace_type": j.get("workplaceType") or "",
            })
    return out


# Filter-sidebar and nav furniture that the page-text fallback otherwise
# mistakes for job titles ("ALL DEPARTMENTS ... LOCATION London, UK New York").
_JUNK_ROLE = re.compile(
    r"all departments|all locations|^location$|^department$|filter|view all"
    r"|see all|search|sign in|log in|cookie|privacy|subscribe|newsletter"
    r"|^careers?$|^jobs?$|^open roles?$|^apply$|^home$", re.I)


def _looks_like_role(title: str) -> bool:
    """Coarse guard for the no-ATS fallback: a plausible job title, not UI chrome."""
    if not title or not (2 < len(title) < 110):
        return False
    if _JUNK_ROLE.search(title):
        return False
    # A title that is only a place name is a location label, not a role.
    if NYC_PATTERN.search(title) and len(title.split()) <= 3:
        return False
    return 0 < len(title.split()) <= 12


def _jobs_from_page_text(text: str) -> List[Dict[str, str]]:
    """Fallback when the careers page exposes no Greenhouse/Lever/Ashby board.

    Parses markdown links across the whole document rather than line by line:
    careers pages commonly render a posting as a multi-line link, with the title
    on one line and the location on the next, e.g.

        *   [
            Product Manager
            New York, US
            ](/careers/co/product/46.F6F/product-manager/all)

    A line-by-line scan never sees those as one link. Only links are considered
    - a bare line mentioning New York is usually an office address or a
    location filter control, not a job.

    KNOWN GAP (logged 2026-09-10, deliberately not fixed): a role only counts
    if NYC_PATTERN matches inside the link text or href. Boards that render the
    location in a sibling element - a separate table cell or div next to the
    title link - yield zero roles even when the page renders correctly and the
    roles really are in NYC. Fixing ATS-board detection sidesteps this for most
    companies, because roles then come from the provider API where location is
    a typed field. Do not widen this to bare non-link lines without a set of
    real failing examples: that was tried and it counted office addresses and
    location-filter controls as jobs.
    """
    jobs, seen = [], set()
    for raw_text, href in MD_LINK_PATTERN.findall(text or ""):
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        if not lines:
            continue
        if not NYC_PATTERN.search(raw_text + " " + href):
            continue
        title = re.sub(r"\s+", " ", lines[0]).strip(" |-#*")
        location = re.sub(r"\s+", " ", " ".join(lines[1:])).strip() or "New York (from page text)"
        if not _looks_like_role(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        jobs.append({"title": title, "location": location, "body": raw_text + " " + href})
    return jobs


def _slug_candidates(company: str, homepage: str) -> List[str]:
    """Plausible board slugs for a company we already know uses a given ATS."""
    name = re.sub(r"[^a-z0-9 ]", "", (company or "").lower()).strip()
    host = _norm_url(homepage).split("/")[0]
    root = host.split(".")[0] if host else ""
    compact = name.replace(" ", "")
    # Concatenated domain labels: genius.ai -> "geniusai", which is exactly
    # GlossGenius's real Ashby slug and is not derivable from its display name.
    host_joined = "".join(p for p in host.split(".") if p) if host else ""
    out = [compact, name.replace(" ", "-"), root, host_joined,
           compact + "labs", root + "labs"]
    if " " in name:
        out.append(name.split()[0])
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def resolve_ats(page_markdown: Optional[str], careers_url: str, company: str,
                homepage: str, quiet: bool = False) -> Dict[str, str]:
    """Establish provider and slug for a careers page with no board URL in its
    markdown.

    Order of confidence:
      1. A board URL in the markdown (handled by the caller).
      2. A board URL in the raw HTML - exact, and the common case for a
         client-side embed.
      3. An embed marker naming the provider, plus a derived slug that is then
         verified against that provider's API.

    Step 3 is not blind ATS probing: the provider comes from the company's own
    page, and CLAUDE.md requires confirming a slug belongs to the company
    before trusting it, so every derived slug is name-checked before use.

    Pre-hydration HTML is tried first because it is cheaper. A board rendered
    entirely client-side exposes nothing until after render, so the same two
    steps are retried against JS-executed HTML before giving up.

    This does NOT recover a company whose careers page never names its ATS at
    all - verified on ElevenLabs, whose 690KB of raw HTML contains no "ashby"
    string in any pass. That class needs slug guessing, which is unsafe until
    ats_belongs_to is strengthened. See CLAUDE.md Known Issues.
    """
    soft = None
    for js in (False, True):
        html = string_fetch_raw(careers_url, execute_js=js, context=company, quiet=quiet)
        if not html:
            continue
        suffix = " (post-render)" if js else ""

        for provider, pattern in ATS_SLUG_IN_HTML:
            m = pattern.search(html)
            if m and m.group(1).lower() not in ("embed", "job_board", "v1", "boards"):
                return {"provider": provider, "slug": m.group(1),
                        "how": "board URL in raw HTML of careers page" + suffix}

        blob = html + " " + (page_markdown or "")
        for provider, marker in ATS_EMBED_MARKERS:
            if not marker.search(blob):
                continue
            for slug in _slug_candidates(company, homepage):
                data = fetch_ats_jobs(provider, slug, quiet=True)
                if data and ats_belongs_to(data, company, homepage):
                    return {"provider": provider, "slug": slug,
                            "how": "{} embed detected on careers page{}; slug '{}' "
                                   "derived and name-verified".format(provider, suffix, slug)}
            # Provider known but no slug verified. Hold it rather than return:
            # the post-render pass may still carry the exact board URL.
            soft = {"provider": "", "slug": "",
                    "how": "{} embed detected but no slug could be verified".format(provider)}
            break

    return soft or {"provider": "", "slug": "", "how": ""}


def guess_ats_slug(company: str, homepage: str) -> Dict[str, str]:
    """Last resort when the company's own pages never name their ATS.

    Bounded and free: at most 3 providers x ~6 derived candidates against the
    providers' public APIs, no String fetches. This was forbidden outright
    until 2026-09-10 because `ats_belongs_to` failed open on namesake slugs
    (ashby/onyx verified as "Onyx Security"). It is only safe now that
    verification requires the company's own domain or full-name phrase - the
    probe on 2026-09-10 documented 8 companies whose live boards are
    unreachable by page inspection, so without this rung they stay invisible.
    """
    for provider in ("ashby", "greenhouse", "lever"):
        for slug in _slug_candidates(company, homepage):
            data = fetch_ats_jobs(provider, slug, quiet=True)
            if data and ats_belongs_to(data, company, homepage):
                return {"provider": provider, "slug": slug,
                        "how": "no board linked on site; slug '{}' derived and "
                               "name-verified against {}".format(slug, provider)}
    return {"provider": "", "slug": "", "how": ""}


def _fetch_careers_page(url: str, company: str = "", quiet: bool = False) -> Optional[str]:
    """Fetch a careers page, escalating to JS-rendering on usefulness.

    A careers page is the one place where a long response can still be an
    empty shell: nav, footer and cookie banner clear any byte threshold while
    the board itself renders client-side. `string_fetch_escalating` keys off
    length alone, so it accepts those shells and never renders them. Judge the
    page by whether it actually yielded something we can use, and only pay for
    a browser render when it did not.

    Scope note (2026-09-10): this was added expecting it to recover the batch's
    "no roles parsed" companies. It did not - those turned out to be
    server-rendered landing pages, not JS shells (see CLAUDE.md Known Issues).
    Kept because it is cheap and correct for genuinely client-rendered boards,
    but do not credit it with fixing that class.
    """
    # Phase 5: jina is a free fetch, tried ahead of String's paid rungs. Same
    # "judge by whether it yielded something usable" discipline as below --
    # jina's reader can also return a shell for a client-rendered page, so a
    # miss here just falls through to String rather than being trusted blind.
    jina_page = jina_fetch(url, context=company or url, quiet=quiet)
    if jina_page and (find_ats_link(jina_page)[0] or _jobs_from_page_text(jina_page)):
        return jina_page
    page = string_fetch(url, context=company or url, quiet=quiet)
    if page and (find_ats_link(page)[0] or _jobs_from_page_text(page)):
        return page
    rendered = string_fetch(url, execute_js=True, context=company or url, quiet=quiet)
    if rendered and (find_ats_link(rendered)[0] or _jobs_from_page_text(rendered)):
        return rendered
    # Nothing found anything usable. Prefer the rendered body anyway: it is
    # the better input for the raw-HTML slug hunt in resolve_ats, then the
    # plain String fetch, then jina's -- most-processed to least.
    return rendered or page or jina_page


def discover_careers_page(company: str, homepage: str) -> Dict[str, Any]:
    """Homepage -> real careers link -> ATS board, in that order.

    Tries every careers-shaped nav link before falling back to guessed paths,
    and prefers a candidate that actually yields something: an ATS board first,
    then a page with role-shaped links on it. Settling for the first page that
    merely returns HTTP 200 is how a company with real NYC roles ends up
    mislabelled "no signal".

    Real navigation-based discovery, not just guessing (2026-09-16): a
    nav-link candidate that itself yields no board and no parsed roles can
    still link one level deeper -- a "Careers" landing page whose own "See
    open roles" link is the real board. That second-hop link is followed
    too, since it's still a real link the site points at, not a guess.
    Guessed paths and guessed ATS slugs are tried only once every real
    link -- direct and one-hop-deep -- is exhausted.
    """
    result = {"careers_url": "", "method": "", "ats_provider": "", "ats_slug": "",
              "ats_verified": "", "page_text": None}

    home_text = string_fetch_escalating(homepage)
    if not home_text:
        result["method"] = "homepage unreachable"
        return result

    # An ATS board can be linked straight from the homepage nav or footer.
    nav_links = find_nav_links(home_text, homepage, CAREER_LINK_KEYWORDS)
    for url in nav_links:
        provider, slug = find_ats_link(url)
        if provider and slug:
            result.update(careers_url=url, ats_provider=provider, ats_slug=slug,
                          method="homepage nav -> ATS board")
            return result

    fallback = None
    tried = set()

    def _try_candidate(url, method, speculative):
        """Fetch one candidate URL. Returns (hit_dict_or_None, deeper_urls).
        deeper_urls is only ever non-empty for a real (non-speculative)
        candidate: a guess is never used as a launching point for more
        guessing."""
        page = _fetch_careers_page(url, company, quiet=speculative)
        if not page or len(page.strip()) < 200:
            # Empty markdown is not a dead end. careers.normalcomputing.com
            # returns 0 chars of markdown, plain and JS-rendered, but 27KB of
            # raw HTML carrying jobs.ashbyhq.com/NormalComputing. The old code
            # `continue`d here and never reached the raw rung at all.
            res = resolve_ats(page, url, company, homepage, quiet=speculative)
            if res["provider"] and res["slug"]:
                return {"careers_url": url, "page_text": page,
                        "ats_provider": res["provider"], "ats_slug": res["slug"],
                        "method": method + " -> " + res["how"]}, []
            return None, []

        provider, slug = find_ats_link(page)
        if provider and slug:
            return {"careers_url": url, "page_text": page, "ats_provider": provider,
                    "ats_slug": slug, "method": method + " -> ATS board"}, []

        # No board URL in the markdown. A client-side embed hides it in the raw
        # HTML, so look there before writing this page off.
        res = resolve_ats(page, url, company, homepage, quiet=speculative)
        if res["provider"] and res["slug"]:
            return {"careers_url": url, "page_text": page, "ats_provider": res["provider"],
                    "ats_slug": res["slug"], "method": method + " -> " + res["how"]}, []

        # No board here. Keep it only if it looks like it lists actual roles,
        # and keep looking either way.
        if _jobs_from_page_text(page):
            return {"careers_url": url, "page_text": page, "method": method}, []

        nonlocal fallback
        if fallback is None:
            fallback = (url, page, method + " (no roles parsed)")

        deeper = find_nav_links(page, url, CAREER_LINK_KEYWORDS) if not speculative else []
        return None, deeper

    # Phase 1: every real nav link, plus up to MAX_SECOND_HOPS links found
    # one level deeper on those pages. All still real, followed links --
    # nothing guessed yet. Bounded so a large careers microsite can't blow
    # up the fetch budget chasing links indefinitely.
    MAX_SECOND_HOPS = 3
    second_hops_used = 0
    queue = [(u, "homepage nav -> careers page") for u in nav_links[:4]]
    qi = 0
    while qi < len(queue):
        url, method = queue[qi]
        qi += 1
        norm = _norm_url(url)
        if norm in tried:
            continue
        tried.add(norm)
        hit, deeper_urls = _try_candidate(url, method, speculative=False)
        if hit:
            result.update(hit)
            return result
        if second_hops_used < MAX_SECOND_HOPS:
            for deeper_url in deeper_urls:
                if second_hops_used >= MAX_SECOND_HOPS:
                    break
                if _norm_url(deeper_url) not in tried:
                    queue.append((deeper_url, method + " -> second-hop nav link"))
                    second_hops_used += 1

    # Phase 2: only now, with every real link exhausted, try guessed paths.
    for path in CAREER_PATH_FALLBACK:
        url = homepage.rstrip("/") + path
        norm = _norm_url(url)
        if norm in tried:
            continue
        tried.add(norm)
        hit, _ = _try_candidate(url, "guessed path (low confidence)", speculative=True)
        if hit:
            result.update(hit)
            return result

    # Nothing on the company's own pages named a board. Try derived slugs
    # against the provider APIs before writing the company off - free, and the
    # only rung that reaches a board the site never mentions.
    guess = guess_ats_slug(company, homepage)
    if guess["provider"] and guess["slug"]:
        result.update(careers_url=(fallback[0] if fallback else ""),
                      page_text=(fallback[1] if fallback else None),
                      ats_provider=guess["provider"], ats_slug=guess["slug"],
                      method=(fallback[2] + " -> " if fallback else "") + guess["how"])
        return result

    if fallback:
        result.update(careers_url=fallback[0], page_text=fallback[1], method=fallback[2])
        return result

    result["method"] = "no careers page found"
    return result


def _research_company_live(company: str, homepage: str) -> Dict[str, Any]:
    """Full company-research pass: discovery, ATS resolution, NYC role
    matching, salary banding, and qualification tier. Layer 1 entry point,
    persona-agnostic. Always hits the network -- called by research_company()
    below only on a cache miss.

    Phase 5: HyperSignal's local db is checked first (_jobs_from_hypersignal).
    When it has fresh data for this domain, the whole fetch/nav-link/ATS-
    resolution chain below is skipped entirely -- HyperSignal's rows are
    already structured and trustworthy, so re-scraping the careers page would
    just be spending String/jina credits to re-derive what's already known.
    """
    domain = re.sub(r"^https?://(www\.)?", "", homepage or "").rstrip("/").split("/")[0].lower()
    hs_jobs = _jobs_from_hypersignal(domain)

    out = {
        "careers_url": "",
        "discovery_method": "",
        "ats": "",
        "nyc_roles": [],
        "role_band_used": "",
        "band_base": "",
        "salary_source": "",
        "signal_tier": "no signal",
        "signal_notes": "",
        "total_jobs": 0,
        # Persisted so NYC-required vs NYC-as-one-of-several is decidable from
        # the CSV alone. Previously the location string was matched on and then
        # thrown away, which made the remote-alternative rule unauditable.
        "nyc_role_locations": [],
        "qualification": "",
        "disqualify_reason": "",
    }

    # Phase 14 (2026-09-22): when HyperSignal has no fresh data for this domain, stop here
    # rather than falling through to discover_careers_page's jina/String live-discovery
    # fallback. On the 9/21 run, 601 of 850 companies processed (71%) hit exactly that
    # fallback and came back "no signal"/"NO BOARD DETECTED" -- almost all because
    # HyperSignal simply hadn't reached them yet under the throttled job-fetch rotation,
    # not because they have no real careers page. That fallback's real hit rate against
    # not-yet-scanned companies is close to zero, so it was spending real String/jina
    # credits (and, for companies that then looked "qualified" on bad data, downstream
    # Anthropic credits) to produce a false negative written to Fibery as Disqualified or
    # Needs Manual Review -- both of which read as "we looked and it's not a fit," when
    # the honest state is "we haven't looked yet." See
    # claude/nycbound-gtm-phase14-tiered-chunking-scope.md (Sales & Marketing claude.ai
    # Project). main.py's _map_research_status() maps this "PENDING" qualification to
    # Fibery's "Pending Not Yet Scanned" status and skips persona contact-enrichment and
    # the Anthropic research calls for it, the same way it already skips them for a real
    # disqualification -- no new gate needed downstream.
    if hs_jobs is None:
        out["qualification"] = "PENDING - not yet scanned by HyperSignal"
        out["disqualify_reason"] = ""
        out["discovery_method"] = "HyperSignal (no fresh data for this domain yet)"
        out["signal_notes"] = (
            "PENDING - HyperSignal has no fresh job data for this domain yet (not on "
            "its watchlist today, or its data is older than {}d). Not a confirmed "
            "absence of openings -- will be picked up automatically once HyperSignal's "
            "rotation reaches it."
        ).format(HYPERSIGNAL_FRESHNESS_DAYS)
        return out

    jobs: List[Dict[str, str]] = hs_jobs
    board_confirmed = True
    out.update(
        careers_url="",
        discovery_method="HyperSignal (local signals.db, fresh within {}d)".format(
            HYPERSIGNAL_FRESHNESS_DAYS),
        ats_verified="name matched" if jobs else "",
    )

    out["total_jobs"] = len(jobs)

    # Location first. Matching on the body as well pulls in roles based
    # elsewhere that merely mention a New York office or customer, which is how
    # a London posting ends up counted as an NYC relocation signal. The body is
    # only consulted when the posting carries no location at all.
    nyc_matched = [j for j in jobs
                   if (NYC_PATTERN.search(j["location"])
                       if (j.get("location") or "").strip()
                       else NYC_PATTERN.search(j.get("body") or ""))]

    # Per-role remote rule: an NYC-tagged posting only fails if THAT posting
    # offers remote as an alternative. A separate remote role elsewhere on the
    # same board is irrelevant. Stedi is the live example - all four of its
    # NYC-matching postings are themselves "Remote in the USA".
    nyc_jobs, nyc_remote = [], []
    for j in nyc_matched:
        loc = (j.get("location") or "")
        if j.get("is_remote") or ROLE_REMOTE_PATTERN.search(loc):
            nyc_remote.append(j)
        else:
            nyc_jobs.append(j)

    out["nyc_role_locations"] = [
        "{} @ {}{}".format((j.get("title") or "?"),
                           (j.get("location") or "location not stated"),
                           " [REMOTE-ALTERNATIVE]" if j in nyc_remote else "")
        for j in nyc_matched[:12]
    ]

    if not nyc_jobs:
        note = ("No NYC-based open roles found ({} roles scanned via {}). "
                "Not a fit for this play - calculator numbers not forced."
                ).format(len(jobs), out["discovery_method"] or "no source")
        # A parsing failure and a genuine absence of NYC roles both land here,
        # and they mean opposite things. Say which one this is: a false
        # "no signal" writes off a live prospect.
        # Roles read from a name-verified ATS board are trustworthy, so the
        # count means what it says. Do NOT substring-match the method string in
        # that case: a successfully guessed board carries the earlier
        # "(no roles parsed)" fallback text in its method, which made a board
        # with 13 real roles report NO BOARD DETECTED.
        ats_ok = bool(jobs) and out.get("ats_verified") == "name matched"
        weak = (not jobs) or (not ats_ok and (
            "low confidence" in out["discovery_method"]
            or "no roles parsed" in out["discovery_method"]
            or "not found" in out["discovery_method"]
            or "unreachable" in out["discovery_method"]))
        if weak:
            note += (" VERIFY MANUALLY: no roles could be parsed at all, so this is "
                     "an extraction failure as likely as a real absence of openings.")
            out["qualification"] = "NO BOARD DETECTED - MANUAL CHECK NEEDED"
        elif nyc_remote:
            out["qualification"] = "DISQUALIFIED"
            out["disqualify_reason"] = (
                "every NYC-tagged role offers remote as an alternative on the posting "
                "itself ({} of {} roles)".format(len(nyc_remote), len(jobs)))
            note += " " + out["disqualify_reason"].capitalize() + "."
        else:
            out["qualification"] = "DISQUALIFIED"
            out["disqualify_reason"] = (
                "{} roles read from the board, none NYC-based".format(len(jobs)))
        out["signal_notes"] = note
        return out

    scored = []
    for j in nyc_jobs:
        # Structured provider data first; regex the prose only when there is none.
        if j.get("salary_max") or j.get("salary_min"):
            salary = j.get("salary_max") or j.get("salary_min")
            raw = j.get("salary_raw") or ""
            salary_floor = j.get("salary_min") or salary
        else:
            salary, raw, salary_floor = extract_salary(j.get("body") or "")
        entry = is_entry_level(j.get("title") or "", salary)
        ote = bool(OTE_PATTERN.search((j.get("body") or "") + " " + (j.get("salary_raw") or "")))
        band_name, band_base = map_role_band(j.get("title") or "")
        scored.append({
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "salary": salary,
            "salary_raw": raw,
            "salary_floor": salary_floor,
            "entry_level": entry,
            "ote": ote,
            "band_name": band_name,
            "band_base": band_base,
            "body": j.get("body") or "",
        })

    # Comp floor. Disqualify only when EVERY NYC role is priced and all of them
    # sit under the floor - an unpriced role cannot establish "all under", so a
    # partly-priced board stays qualified with comp unproven. `scored` holds
    # every NYC role, not the truncated display list.
    priced = [s["salary"] for s in scored if s["salary"]]
    if priced and len(priced) == len(scored) and max(priced) < MIN_SALARY_FLOOR:
        out["qualification"] = "DISQUALIFIED"
        out["disqualify_reason"] = (
            "all {} NYC role(s) priced, every one under ${:,} (max ${:,})".format(
                len(scored), MIN_SALARY_FLOOR, max(priced)))
    else:
        out["qualification"] = "QUALIFIED"
        if not priced:
            out["disqualify_reason"] = ""  # qualified, comp unproven
            out["qualification"] = "QUALIFIED (comp unproven)"

    # The driving role must not be entry-level: those carry no relocation
    # budget (CLAUDE.md).
    senior = [s for s in scored if not s["entry_level"]]
    pool = senior or scored

    # Sales roles post on-target earnings, not base. Clay's top NYC listing is
    # a Sales Manager at "$290K - $375K" OTE; quoting that to a CFO as a base
    # salary is wrong on the facts and easy for them to dismiss. Prefer any
    # non-GTM senior role, and fall back to GTM only if that is all there is.
    non_gtm = [s for s in pool if not s["ote"] and s["band_name"] != "GTM Lead"]
    pool = non_gtm or pool

    pool.sort(key=lambda s: (s["salary"] is not None, s["salary"] or s["band_base"]), reverse=True)
    driver = pool[0]

    out["driving_role"] = driver["title"]
    # Kept separate from role_band_used (below), which becomes the string "posted salary" when a
    # real number was found -- compute_ramp_calc() needs the role NAME in both cases to pick the
    # right value-add factor, whether or not a real salary was posted.
    out["band_role_name"] = driver["band_name"]
    if driver["salary"]:
        out["role_band_used"] = "posted salary"
        out["band_base"] = driver["salary"]
        # The floor of the posted range, not the ceiling -- the CFO play (2026-09-19 rewrite)
        # cites the floor as the harder-to-dispute number. Equal to band_base when the posting
        # gave a single number, not a range.
        out["band_salary_floor"] = driver["salary_floor"]
        out["salary_source"] = "posted on '{}' ({})".format(
            driver["title"], driver["salary_raw"])
    else:
        out["role_band_used"] = driver["band_name"]
        out["band_base"] = driver["band_base"]
        out["band_salary_floor"] = driver["band_base"]
        out["salary_source"] = "no salary posted - mapped to Research and Stats band"

    blob = " ".join((s["title"] + " " + s["location"] + " " + s["body"]) for s in scored)
    if TIER1_PATTERN.search(blob):
        out["signal_tier"] = "Tier 1 - explicit relocation/visa language"
    elif TIER2_PATTERN.search(blob):
        out["signal_tier"] = "Tier 2 - NYC presence required, no relocation support (pitch as 'no existing solution')"
    else:
        out["signal_tier"] = "Tier 3 - NYC roles open, no relocation or onsite language found"

    out["nyc_roles"] = [
        "{}{} [{}]".format(
            s["title"],
            " (" + s["salary_raw"] + ")" if s["salary_raw"] else "",
            "entry-level" if s["entry_level"] else "senior",
        )
        for s in scored[:8]
    ]

    notes = ["{} NYC role(s) of {} total".format(len(nyc_jobs), len(jobs)),
             out["qualification"], out["signal_tier"]]
    if out["disqualify_reason"]:
        notes.append("DISQUALIFY: " + out["disqualify_reason"])
    if nyc_remote:
        notes.append("{} further NYC role(s) excluded as remote-alternative".format(len(nyc_remote)))
    if driver.get("ote") or (not non_gtm and senior):
        notes.append("CAUTION: driving figure comes from a sales/OTE posting - "
                     "may be on-target earnings, not base salary")
    if not senior:
        notes.append("WARNING: all NYC roles are entry-level - no relocation budget, "
                     "downgrade this account as a driving signal")
    if out.get("ats_verified", "").startswith("UNVERIFIED"):
        notes.append(out["ats_verified"])
    if "low confidence" in out["discovery_method"]:
        notes.append("careers page found by guessed path, not a real nav link")
    out["signal_notes"] = "; ".join(notes)
    return out


def research_company(company: str, homepage: str, no_cache: bool = False) -> Dict[str, Any]:
    """Cache-aware public entry point. Fresh cache hit for this domain
    returns the stored result with no network calls at all. Miss, stale, or
    no_cache=True runs the full research pass live and caches the result
    (see cache.py for the "MANUAL CHECK NEEDED" short-TTL rule)."""
    return cache.get_or_fetch(
        "company_research", company, homepage,
        fetch_fn=lambda: _research_company_live(company, homepage),
        no_cache=no_cache,
    )


# --------------------------------------------------------------------------
# Stated-goal research (Phase 5, 2026-09-16). Search-and-cite, not inference:
# the prompt requires a verbatim quote and returns found: false on anything
# ambiguous, so what comes back is a real citation of what the company
# itself said, not a guess dressed up as one -- see the Phase 5 scope doc
# for the full framing and the citation-verification design this
# implements. Meant to go straight into outreach copy ("saw you're
# expanding into X because [quote]"), not just sit as internal context, but
# only once the automated verification step below confirms the quote is
# real and calibration/human review (Phase 8) confirm it's the right one to
# lead with -- this function only guarantees the quote is genuine.
# --------------------------------------------------------------------------

_STATED_GOAL_PROMPT = (
    "You're researching why {company} ({domain}) is hiring for a role that "
    "requires being in NYC. Search for: (1) the actual job posting language "
    "-- does it say this is a new/net-new function, backfilling someone who "
    "left, or team expansion; (2) funding news in the last 12 months; (3) a "
    "recent product launch or market expansion that would explain new NYC "
    "headcount.\n\n"
    "Only report a finding if you can quote the exact sentence or phrase it "
    "came from. Do not summarize or infer beyond what the source actually "
    "says. If you search and find nothing that clearly supports a reason, "
    "say so -- do not guess.\n\n"
    '"goal" must be a full one-sentence paraphrase of what the excerpt '
    "actually says (who's doing what, and any concrete numbers/timeframe "
    "in the excerpt) -- never a single word or bare category label like "
    '"hiring" or "funding". If you cannot write a real sentence from the '
    "excerpt, treat it as not found rather than shipping a one-word answer.\n\n"
    'Respond as JSON: {{"found": bool, "goal": string|null, '
    '"source_type": "job_posting"|"funding_news"|"product_launch"|null, '
    '"excerpt": string|null, "url": string|null}}'
)

# Second-pass prompt for the exa escalation: exa has already found candidate
# pages, so this asks the model to read and extract from text already in
# hand rather than searching again -- a plain (no web-search-tool) call.
_STATED_GOAL_EXTRACT_PROMPT = (
    "Below is the text of a web page about {company} ({domain}). Does it "
    "explain why they're hiring for an NYC-based role -- a new/net-new "
    "function, backfilling someone, team expansion, funding news, or a "
    "product/market launch? Only answer yes if you can quote the exact "
    "sentence or phrase. Do not infer beyond what the text actually "
    "says.\n\nPAGE TEXT:\n{content}\n\n"
    '"goal" must be a full one-sentence paraphrase of what the excerpt '
    "actually says (who's doing what, and any concrete numbers/timeframe "
    "in the excerpt) -- never a single word or bare category label like "
    '"hiring" or "funding". If you cannot write a real sentence from the '
    "excerpt, answer found: false instead.\n\n"
    'Respond as JSON: {{"found": bool, "goal": string|null, '
    '"source_type": "job_posting"|"funding_news"|"product_launch"|null, '
    '"excerpt": string|null}}'
)


def _empty_stated_goal(status: str) -> Dict[str, Any]:
    return {"found": False, "goal": "", "source_type": "", "excerpt": "", "url": "",
            "status": status}


def _research_stated_goal_live(company: str, domain: str, ledger=None) -> Dict[str, Any]:
    prompt = _STATED_GOAL_PROMPT.format(company=company, domain=domain)
    try:
        text = _llm_web_search(prompt, ledger=ledger, call_site="research")
    except MissingKey as exc:
        return _empty_stated_goal("not attempted - {}".format(exc))
    data = _parse_json_response(text)

    if data and data.get("found") and data.get("excerpt") and data.get("url"):
        if verify_excerpt_on_page(data["url"], data["excerpt"], ledger=ledger):
            return {
                "found": True,
                "goal": (data.get("goal") or "").strip(),
                "source_type": (data.get("source_type") or "").strip(),
                "excerpt": data["excerpt"].strip(),
                "url": data["url"],
                "status": "resolved via web_research",
            }
        # Citation didn't verify against the live page -- treated exactly
        # like found: false per the scope doc. A plausible-looking but
        # unconfirmed quote never ships; fall through to exa below.

    # Escalation: exa, a second/different search index, only on a
    # web_research miss (including an unverified citation, handled above).
    if ledger is not None and ledger.exa_budget_exhausted():
        return _empty_stated_goal("not found - web_research miss, exa budget capped")

    try:
        results = exa_search(
            "{} {} hiring: new role, funding, expansion, or backfill".format(company, domain),
            ledger=ledger, num_results=5)
    except MissingKey:
        results = []

    for r in results:
        content = (r.get("text") or "").strip()
        url = r.get("url") or ""
        if not content or not url:
            continue
        extract_prompt = _STATED_GOAL_EXTRACT_PROMPT.format(
            company=company, domain=domain, content=content[:6000])
        try:
            extract_text = _llm_extract(extract_prompt, ledger=ledger, call_site="extract")
        except MissingKey:
            break
        extracted = _parse_json_response(extract_text)
        if extracted and extracted.get("found") and extracted.get("excerpt"):
            if verify_excerpt_on_page(url, extracted["excerpt"], ledger=ledger):
                return {
                    "found": True,
                    "goal": (extracted.get("goal") or "").strip(),
                    "source_type": (extracted.get("source_type") or "").strip(),
                    "excerpt": extracted["excerpt"].strip(),
                    "url": url,
                    "status": "resolved via exa",
                }

    return _empty_stated_goal("not found - no verifiable citation from web_research or exa")


# Bump this string any time _STATED_GOAL_PROMPT or _STATED_GOAL_EXTRACT_PROMPT
# changes in a way that changes what a cached "found: true" answer means.
# Found the hard way, 2026-09-18: the 2026-09-17 fix that stopped one-word
# "hiring" answers left the *old* text sitting in cache under the old
# prompt's blessing, and it silently re-shipped to Fibery (overwriting an
# already-hand-corrected record) the next time that company was processed.
# This constant, combined with cache.py's expected_version check, makes a
# prompt change self-invalidating -- no more remembering to manually
# cache.bust() every affected domain by hand.
_STATED_GOAL_PROMPT_VERSION = "2026-09-18"


def research_stated_goal(company: str, domain: str, ledger=None,
                         no_cache: bool = False) -> Dict[str, Any]:
    """Cache-aware public entry point. Same domain-keyed cache as
    research_company/find_finance_contact (cache.py's "stated_goal" table).
    A verified finding caches for STATED_GOAL_TTL_DAYS; a not-found result
    caches for the shorter SHORT_TTL_DAYS so it's retried sooner rather than
    treated as a durable absence of signal (cache.py). Also version-gated
    (see _STATED_GOAL_PROMPT_VERSION above) -- a cached row from before the
    prompt last changed is treated as a miss, not a stale-but-usable hit."""
    return cache.get_or_fetch(
        "stated_goal", company, domain,
        fetch_fn=lambda: _research_stated_goal_live(company, domain, ledger),
        no_cache=no_cache,
        expected_version=_STATED_GOAL_PROMPT_VERSION,
    )


# --------------------------------------------------------------------------
# "Likely Priorities" inference (2026-09-17). Deliberately the opposite
# design point from stated-goal research above: that function is
# search-and-cite and refuses to guess, this one is guardrailed inference
# and guessing (within limits) is the entire point. Rachel's own framing:
# "the llm should make an inference but the prompt has to be very tight and
# within guardrails" -- every sentence about the specific contact must be
# hedged ("likely," "may," "could," "appears to"), and every claim must
# trace back to either (a) this persona's own researched role
# responsibilities/pain points (Persona Profile docs, Sales & Marketing
# claude.ai Project), generalized to "someone in this role," or (b) a
# signal main.py actually gathered this run (open NYC roles, qualification
# tier, a found Stated Goal) -- never an invented specific fact about the
# person or company. No new search, no new provider spend: one plain
# (non-search) LLM call per resolved contact, logged the same "web_research,
# 0 cost" way _llm_extract already logs every plain completion call. See
# claude/nycbound-gtm-likely-priorities-scope.md (Sales & Marketing
# claude.ai Project) for the full design writeup.
# --------------------------------------------------------------------------

# Condensed from the real Persona Profile PDFs in the Sales & Marketing
# claude.ai Project -- responsibilities, pain points (with their own
# quantified impact framing), and what each role is actually measured on.
# Only the 3 live personas have an entry; a persona with no entry here
# (Global Mobility, VC Talent, CEO/Founder -- none live in main.py yet)
# simply doesn't get a Likely Priorities write-up.
_LIKELY_PRIORITIES_PERSONA_CONTEXT: Dict[str, str] = {
    "cfo": (
        "CFOs/senior finance leaders at high-growth companies recruiting into or expanding "
        "an NYC hub. They own total people/talent cost (salary, benefits, relocation, "
        "real-estate spend), safeguard productivity and time-to-ramp for revenue-critical "
        "hires (sales, execs, engineering), approve relocation policy/vendor spend against a "
        "clear ROI case, and reduce financial risk from failed relocations, delayed starts, "
        "or extended temp housing. Known pain points: hidden productivity loss when new hires "
        "lose ~3 weeks to housing search during onboarding; unplanned/unbudgeted costs from "
        "temp-housing extensions and delayed start dates; critical hires and leadership "
        "distracted by relocation logistics instead of ramping; international hires adding "
        "credit/guarantor/visa friction; finance having limited bandwidth to manage relocation "
        "vendors directly even though the cost lands on their P&L. What they're measured on: "
        "total people cost, ROI on relocation spend, time-to-ramp/productivity for critical "
        "hires, predictable and budgetable costs."
    ),
    "hr_people_ops": (
        "HR/People Ops leaders at high-growth, often venture-backed companies hiring into "
        "NYC. They own the end-to-end employee experience from offer acceptance through "
        "onboarding (including relocation), protect candidate close rates by reducing "
        "relocation/housing/onboarding friction, partner with Talent/Recruiting and Finance so "
        "high-value hires ramp quickly without wasted recruiting spend, and manage external "
        "vendors without adding headcount on a lean team. Known pain points: relocation risk "
        "killing offers (losing 2-3 senior candidates a year can mean $50K-$300K in wasted "
        "recruiting spend); onboarding derailed by housing chaos (new hires can lose 3-4 weeks "
        "to apartment hunting); poor relocation experience hurting employer brand and raising "
        "90-day churn risk; limited internal bandwidth or NYC-specific expertise for bespoke "
        "relocation support, especially for international hires. What they're measured on: "
        "candidate close rates, time-to-productivity, early retention."
    ),
    "recruiter_ta": (
        "Recruiters/Talent Partners at high-growth, often VC-backed companies hiring into "
        "NYC (often hybrid/in-office roles). They run full-cycle recruiting for critical "
        "roles, partner with hiring managers/HR to scope roles and keep candidates warm "
        "through long interview loops, coordinate offer packages and handle objections around "
        "cost of living/relocation/family, manage candidate experience end to end, and flag "
        "risks that could derail a close (competing offers, relocation anxiety, timing, family "
        "constraints). Known pain points: relocation anxiety stalling or killing offers (2-3 "
        "lost hires a year at $150K-$250K comp can mean $300K-$750K of lost recruiting ROI); "
        "time-to-fill blowing up when candidates self-navigate housing (a delayed start can add "
        "3-4 weeks); recruiters becoming default relocation support (5-10 extra hours per "
        "candidate on non-core work); international/complex cases feeling risky enough that a "
        "recruiter avoids pitching a strong candidate on an NYC role, or loses them to a more "
        "\"relocation-ready\" competitor. What they're measured on: time-to-fill, offer "
        "acceptance rate, quality of hire."
    ),
}

_LIKELY_PRIORITIES_PROMPT = (
    "You write short, hedged sales-prep notes for Pinpointe NYCbound, a company that helps "
    "employers relocate new NYC hires. You are NOT researching this specific person -- you "
    "have no information about them beyond what's given below. Your job is a reasonable, "
    "clearly hedged inference about what someone in their role, at a company with these "
    "signals, is likely dealing with right now.\n\n"
    "CONTACT: {contact_name}, {title}, at {company}.\n\n"
    "WHAT THIS ROLE TYPICALLY OWNS AND STRUGGLES WITH (from Pinpointe's own persona "
    "research -- your only source for role-level claims):\n{persona_profile}\n\n"
    "WHAT WE ACTUALLY KNOW ABOUT THIS COMPANY RIGHT NOW (your only source for "
    "company-specific claims):\n{signals}\n\n"
    "Hard rules:\n"
    "- Every sentence about this specific person's goals, priorities, or challenges must use "
    "hedged language -- \"likely,\" \"may,\" \"could,\" \"appears to,\" \"probably.\" Never "
    "state anything about them as settled fact.\n"
    "- Every claim must trace back to either the role-level research above (generalized to "
    "\"someone in this role\") or a signal actually listed above. Never invent a specific fact "
    "about this person or their company that isn't in the inputs.\n"
    "- If the company signals are thin, fall back to role-level generalization only -- that's "
    "still a valid, hedged answer, not a failure.\n"
    "- If a specific role title is given in the signals (e.g. \"Director, Data Analytics\"), "
    "name that exact title when referencing this company's hiring -- never soften it to a "
    "generic level description like \"Director-level roles\" or \"senior director level.\"\n"
    "- Keep it concise: 2-4 short sentences (or a short list for KPIs) per section, like a "
    "sharp internal sales note, not an essay.\n\n"
    "Respond with plain text in exactly this structure (section headers verbatim, in caps, "
    "each on its own line):\n\n"
    "GOALS & PRIORITIES\n<2-4 hedged sentences>\n\n"
    "CHALLENGES, PRIORITIES, AND RESPONSIBILITIES\n<2-4 hedged sentences>\n\n"
    "KPIS TO PRIORITIZE\n<short list of likely role KPIs -- these are well-known role "
    "metrics, not claims about the individual, so no hedging needed here>"
)

_LIKELY_PRIORITIES_SECTIONS = (
    "GOALS & PRIORITIES",
    "CHALLENGES, PRIORITIES, AND RESPONSIBILITIES",
    "KPIS TO PRIORITIZE",
)

_LIKELY_PRIORITIES_HEDGE_WORDS = ("likely", "may ", "could ", "appears to", "probably", "might ")


def _validate_likely_priorities(text: Optional[str]) -> bool:
    """Defense-in-depth guardrail check, mirroring the non-fabrication spirit of the
    stated-goal path above even though the bar here is structure + hedging rather than
    citation verification. A response that fails this is never written to Fibery --
    treated the same as a stated-goal miss, not shipped as a degraded answer."""
    if not text:
        return False
    upper = text.upper()
    if not all(section in upper for section in _LIKELY_PRIORITIES_SECTIONS):
        return False
    lower = text.lower()
    if not any(w in lower for w in _LIKELY_PRIORITIES_HEDGE_WORDS):
        return False
    return True


def _summarize_signals_for_likely_priorities(company: str, jobs: Dict[str, Any],
                                             stated_goal: Dict[str, Any]) -> str:
    """Only ever pulls from what main.py already gathered this run (Layer 1's jobs dict,
    and stated-goal research if it found something) -- never triggers new research of its
    own. Thin signal is expected and fine; the prompt tells the model to fall back to
    role-level generalization when this is sparse."""
    bits = []
    qualification = jobs.get("qualification")
    if qualification:
        bits.append("Qualification: {}".format(qualification))
    nyc_roles = jobs.get("nyc_roles") or []
    if nyc_roles:
        bits.append("{} open NYC role(s), e.g.: {}".format(
            len(nyc_roles), "; ".join(nyc_roles[:3])))
    band = jobs.get("role_band_used")
    if band and jobs.get("band_base"):
        bits.append("Salary band in play: {} (${:,})".format(band, int(jobs["band_base"])))
    driving_role = jobs.get("driving_role")
    if driving_role:
        bits.append("Specific role driving this signal: {}".format(driving_role))
    if stated_goal and stated_goal.get("found"):
        bits.append("Stated goal (verified): {} [{}]".format(
            stated_goal.get("goal") or "", stated_goal.get("source_type") or "?"))
    if not bits:
        return "(no company-specific signal gathered this run -- rely on role-level research only)"
    return "\n".join("- " + b for b in bits)


def research_likely_priorities(persona_key: str, contact: Dict[str, Any], company: str,
                               jobs: Dict[str, Any], stated_goal: Dict[str, Any],
                               ledger=None) -> Optional[str]:
    """Public entry point. Returns the formatted 3-section text, or None if this persona
    has no profile context yet, the LLM call fails, or the response fails the guardrail
    check -- in every None case the caller simply doesn't write the field this run, the
    same "skip rather than ship something under the bar" behavior as stated-goal misses.
    Deliberately uncached (unlike research_company/research_stated_goal): the call is a
    single cheap non-search completion, and caching it per-contact would need a new cache
    table for what's genuinely a low-cost convenience, not a budget-gated resource."""
    profile = _LIKELY_PRIORITIES_PERSONA_CONTEXT.get(persona_key)
    if not profile:
        return None
    prompt = _LIKELY_PRIORITIES_PROMPT.format(
        contact_name=contact.get("contact_name") or "this contact",
        title=contact.get("title") or "",
        company=company,
        persona_profile=profile,
        signals=_summarize_signals_for_likely_priorities(company, jobs, stated_goal),
    )
    try:
        text = _llm_extract(prompt, ledger=ledger, max_tokens=500,
                            call_site="likely-priorities")
    except MissingKey:
        return None
    text = (text or "").strip()
    if not _validate_likely_priorities(text):
        return None
    return text
