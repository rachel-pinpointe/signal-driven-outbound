"""Provider clients for the lead-enrichment pipeline (persona-agnostic as of
Phase 5, 2026-09-16 -- was cfo_providers.py; renamed since this was already
the shared client library, not CFO-specific).

Fetch and auth patterns are carried over from string_gtm_test_v5.py (the String
test this builds on): one shared session, Bearer auth from .env, and the
X-Status-Code header check that distinguishes a String-level 200 from the
origin's real status.

Credit discipline is the point of this module. Every paid call returns its
`meta.remaining_credits` where the provider gives one, and CreditLedger keeps a
running count so a batch can be stopped before it drains the pool.

Phase 5 additions: jina (free fetch), exa (paid escalation search), the
Anthropic-API web-search call that powers both `web_research_find_contact`
and `research_stated_goal` (in company_research.py), the two persona-agnostic
Layer 2 waterfalls (find_contact_generic / enrich_contact_generic), and real
monthly budget caps on Hunter/Apollo/exa (confirmed vendor numbers, not
estimates -- see the Phase 5 scope doc).
"""

import difflib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from . import cache

load_dotenv()

STRING_FETCH_URL = "https://request.usestring.ai/v1/fetch"
JINA_READER_BASE = "https://r.jina.ai/"
QE_BASE = "https://app.quickenrich.io/api"
APOLLO_BASE = "https://api.apollo.io/api/v1"
HUNTER_BASE = "https://api.hunter.io/v2"
SCRUBBY_BASE = "https://api.scrubby.io"
EXA_BASE = "https://api.exa.ai"

TIMEOUT = 30

# --------------------------------------------------------------------------
# Anthropic cost estimation (Phase 19).
#
# EDIT THESE. Plain values, nothing computed or fetched. Rates are USD per MILLION tokens -- the
# unit Anthropic publishes -- and USD per server-side web_search request. This table is the only
# place rates live, so a price change is a one-line edit here.
#
# These produce an ESTIMATE for visibility, not a bill. Anthropic's own console is authoritative:
# prompt caching, batch discounts and tier pricing are deliberately not modelled, so a run's real
# cost can come in under these numbers. Confirm the rates against current pricing before trusting
# a total to the cent.
ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "claude-opus-5": {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
    "claude-haiku-4-5": {"input_per_mtok": 1.00, "output_per_mtok": 5.00},
}

# Used when ANTHROPIC_MODEL names a model missing from the table above, so an unrecognised model
# still produces a number instead of silently costing zero -- a zero would read as "this is free"
# rather than "nobody added the rates".
ANTHROPIC_PRICING_FALLBACK = {"input_per_mtok": 3.00, "output_per_mtok": 15.00}

ANTHROPIC_WEB_SEARCH_PER_REQUEST = 0.01  # USD per server-side web_search request

# Every Anthropic call in the pipeline is tagged with one of these, so the end-of-run summary can
# say WHERE the money went rather than just how much. Keep in sync with the call sites:
#   research          -- research_stated_goal's web-search pass, and web_research_find_contact
#   extract           -- reading an exa result for a stated goal (no web-search tool)
#   likely-priorities -- research_likely_priorities' per-contact writeup
#   ghost-email       -- apollo_enroll's three-email generation
#   preflight         -- preflight_anthropic's credit/API check
ANTHROPIC_CALL_SITES = ("research", "extract", "likely-priorities", "ghost-email", "preflight")


def anthropic_cost_usd(model: Optional[str], input_tokens: int, output_tokens: int,
                       web_searches: int = 0) -> float:
    """Estimated USD for one Anthropic call, from the table above. Unknown model -> fallback rates."""
    rates = ANTHROPIC_PRICING.get((model or "").strip(), ANTHROPIC_PRICING_FALLBACK)
    return ((int(input_tokens or 0) / 1_000_000.0) * rates["input_per_mtok"]
            + (int(output_tokens or 0) / 1_000_000.0) * rates["output_per_mtok"]
            + int(web_searches or 0) * ANTHROPIC_WEB_SEARCH_PER_REQUEST)


_session = requests.Session()

# String bills per fetch, so the batch cost scales with this. Counted rather
# than estimated: markdown and raw fetches are tracked separately because a
# raw fetch only happens when a careers page hides its board slug in a script.
FETCH_COUNTS = {"markdown": 0, "executejs": 0, "raw": 0}


def fetch_total() -> int:
    return sum(FETCH_COUNTS.values())


class MissingKey(Exception):
    """A key the brief assumes exists is not in .env."""


def require_keys(names: List[str]) -> Dict[str, str]:
    """Load keys from .env, raising rather than silently working around a gap."""
    found, missing = {}, []
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            found[n] = v.strip()
        else:
            missing.append(n)
    if missing:
        raise MissingKey(
            "Missing from .env: " + ", ".join(missing) +
            "\nThe brief assumes these exist. Add them or narrow the run; "
            "this pipeline will not guess or work around a missing key."
        )
    return found


class CreditLedger:
    """Tracks spend per provider and enforces hard ceilings on the providers
    that have a real monthly pool. Every provider call is logged as an
    "attempt" whether or not it costs anything -- visibility into what was
    tried, in what order, not just what was spent (Phase 5).
    """

    # Real vendor numbers, confirmed 2026-09-16 (not estimates): QuickEnrich
    # 300/mo (30-day expiry, no rollover -- protected in practice by the
    # remaining-balance warning below, not by tracking the 30-day window in
    # code), Hunter 50/mo, Apollo 230/mo on the free plan (charged only for a
    # verified net-new email -- see apollo_match), exa 1,000/mo free pool.
    # These are defaults a caller can override; main.py's own per-run cap on
    # top of qe_budget is a separate, smaller throttle for a single batch.
    DEFAULT_HUNTER_BUDGET = 50
    DEFAULT_APOLLO_BUDGET = 230
    DEFAULT_EXA_BUDGET = 1000

    def __init__(self, qe_budget: Optional[int] = None, scrubby_budget: Optional[int] = None,
                 hunter_budget: Optional[int] = DEFAULT_HUNTER_BUDGET,
                 apollo_budget: Optional[int] = DEFAULT_APOLLO_BUDGET,
                 exa_budget: Optional[int] = DEFAULT_EXA_BUDGET):
        self.spent = {"quickenrich": 0, "apollo": 0, "hunter": 0, "scrubby": 0,
                      "string": 0, "jina": 0, "web_research": 0, "exa": 0}
        self.attempts: Dict[str, int] = {}
        self.qe_remaining_reported = None
        self.qe_budget = qe_budget
        # Scrubby credits are real money per CLAUDE.md, same discipline as the
        # QuickEnrich cap: a routine run should not be able to drain the pool
        # just because it started calling a new provider.
        self.scrubby_remaining_reported = None
        self.scrubby_budget = scrubby_budget
        self.hunter_budget = hunter_budget
        self.apollo_budget = apollo_budget
        self.exa_budget = exa_budget
        self.warnings = []
        # Phase 19: Anthropic usage per call site --
        #   call_site -> {calls, input_tokens, output_tokens, web_searches, cost_usd}
        # Deliberately separate from `spent`, which counts vendor credits as integers. Anthropic
        # bills in tokens and this is an estimated dollar figure, so folding it into the same dict
        # would make two different units look like one.
        self.anthropic: Dict[str, Dict[str, Any]] = {}

    def charge(self, provider: str, credits: int, remaining: Optional[int] = None) -> None:
        self.attempts[provider] = self.attempts.get(provider, 0) + 1
        if credits:
            self.spent[provider] = self.spent.get(provider, 0) + credits
        if provider == "quickenrich" and remaining is not None:
            self.qe_remaining_reported = remaining
            if remaining <= 25:
                self.warnings.append(f"QuickEnrich balance low: {remaining} credits left")
        if provider == "scrubby" and remaining is not None:
            self.scrubby_remaining_reported = remaining
            if remaining <= 25:
                self.warnings.append(f"Scrubby balance low: {remaining} credits left")
        if provider == "hunter" and self.hunter_budget is not None and \
                self.hunter_budget - self.spent["hunter"] in (1, 2, 3, 4, 5):
            self.warnings.append(
                f"Hunter balance low: {self.spent['hunter']}/{self.hunter_budget} of the "
                f"50/mo pool spent this run")
        if provider == "apollo" and self.apollo_budget is not None and \
                self.apollo_budget - self.spent["apollo"] in (1, 2, 3, 4, 5, 10, 15, 20):
            self.warnings.append(
                f"Apollo balance low: {self.spent['apollo']}/{self.apollo_budget} of the "
                f"230/mo pool spent this run")

    def qe_budget_exhausted(self) -> bool:
        return self.qe_budget is not None and self.spent["quickenrich"] >= self.qe_budget

    def scrubby_budget_exhausted(self) -> bool:
        return self.scrubby_budget is not None and self.spent["scrubby"] >= self.scrubby_budget

    def hunter_budget_exhausted(self) -> bool:
        return self.hunter_budget is not None and self.spent["hunter"] >= self.hunter_budget

    def apollo_budget_exhausted(self) -> bool:
        return self.apollo_budget is not None and self.spent["apollo"] >= self.apollo_budget

    def exa_budget_exhausted(self) -> bool:
        return self.exa_budget is not None and self.spent["exa"] >= self.exa_budget

    def charge_anthropic(self, call_site: str, model: Optional[str], input_tokens: int,
                         output_tokens: int, web_searches: int = 0) -> Dict[str, Any]:
        """Accumulate one Anthropic call against its call site. No ceiling and no side effects on
        run behavior -- this records what happened, it never decides anything."""
        bucket = self.anthropic.setdefault(call_site or "unknown", {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["input_tokens"] += int(input_tokens or 0)
        bucket["output_tokens"] += int(output_tokens or 0)
        bucket["web_searches"] += int(web_searches or 0)
        bucket["cost_usd"] += anthropic_cost_usd(model, input_tokens, output_tokens, web_searches)
        return bucket

    def anthropic_calls(self) -> int:
        return sum(b["calls"] for b in self.anthropic.values())

    def anthropic_total_cost(self) -> float:
        return sum(b["cost_usd"] for b in self.anthropic.values())

    def anthropic_breakdown_lines(self) -> List[str]:
        """Multi-line Anthropic breakdown for the end-of-run summary and the SIGTERM handler.
        Returned as lines rather than printed so both callers format it the same way."""
        if not self.anthropic:
            return ["Anthropic            : no calls this run"]
        lines = ["Anthropic            : ${:.4f} estimated, {} call(s) across {} call site(s)"
                 .format(self.anthropic_total_cost(), self.anthropic_calls(), len(self.anthropic))]
        for site in sorted(self.anthropic):
            b = self.anthropic[site]
            lines.append("  {:<18}: {} call(s), {:,} in / {:,} out tokens{}, ${:.4f}".format(
                site, b["calls"], b["input_tokens"], b["output_tokens"],
                ", {} web search(es)".format(b["web_searches"]) if b["web_searches"] else "",
                b["cost_usd"]))
        return lines

    def summary(self) -> str:
        parts = [
            f"QuickEnrich {self.spent['quickenrich']} credits"
            + (f" (balance {self.qe_remaining_reported})" if self.qe_remaining_reported is not None else ""),
            f"Apollo {self.spent['apollo']} verified emails"
            + (f"/{self.apollo_budget} mo cap" if self.apollo_budget is not None else ""),
            f"Hunter {self.spent['hunter']} credits"
            + (f"/{self.hunter_budget} mo cap" if self.hunter_budget is not None else ""),
            f"Scrubby {self.spent['scrubby']} credits"
            + (f" (balance {self.scrubby_remaining_reported})" if self.scrubby_remaining_reported is not None else ""),
            f"exa {self.spent['exa']} credits"
            + (f"/{self.exa_budget} mo cap" if self.exa_budget is not None else ""),
        ]
        if self.anthropic:
            parts.append("Anthropic ${:.4f} est ({} call(s))".format(
                self.anthropic_total_cost(), self.anthropic_calls()))
        free_attempts = {p: n for p, n in self.attempts.items() if p in ("string", "jina", "web_research")}
        if free_attempts:
            parts.append("free rungs tried: " + ", ".join(
                "{}={}".format(p, n) for p, n in sorted(free_attempts.items())))
        if self.warnings:
            parts.append("{} warning(s)".format(len(self.warnings)))
        return " | ".join(parts)


# --------------------------------------------------------------------------
# String - raw API (the brief requires the raw API here, not the MCP tool:
# this is a deterministic batch over a fixed list, not exploratory discovery)
# --------------------------------------------------------------------------

def string_fetch(url: str, execute_js: bool = False, context: str = "",
                 ledger: Optional["CreditLedger"] = None,
                 quiet: bool = False) -> Optional[str]:
    """Fetch a URL as markdown. Returns None on any non-2xx origin status.

    Carried over from string_gtm_test_v5.string_fetch, plus the executeJS
    escalation rung: plenty of sites answer a plain fetch with HTTP 200 and a
    zero-length body.
    """
    key = os.environ.get("STRING_API_KEY")
    if not key:
        raise MissingKey("STRING_API_KEY missing from .env")
    payload = {"url": url, "format": "markdown"}
    if execute_js:
        payload["executeJS"] = True
    FETCH_COUNTS["executejs" if execute_js else "markdown"] += 1
    if ledger is not None:
        ledger.charge("string", 0)  # String wasn't tracked before Phase 5; now every attempt is logged.
    try:
        r = _session.post(
            STRING_FETCH_URL,
            headers={"Authorization": "Bearer " + key},
            json=payload,
            timeout=TIMEOUT,
        )
        origin_status = int(r.headers.get("X-Status-Code", r.status_code))
        if origin_status < 400 and r.text and r.text.strip():
            return r.text
        if not quiet:
            _provider_error(ledger, "string", context or url,
                            "origin HTTP {}{}".format(
                                origin_status,
                                "" if (r.text and r.text.strip()) else " (empty body)"))
        return None
    except (requests.RequestException, ValueError) as exc:
        if not quiet:
            _provider_error(ledger, "string", context or url, "request failed: {}".format(exc))
        return None


def string_fetch_raw(url: str, execute_js: bool = False, context: str = "",
                     ledger: Optional["CreditLedger"] = None,
                     quiet: bool = False) -> Optional[str]:
    """Fetch verbatim HTML instead of markdown.

    Needed because a client-side ATS embed keeps its board slug in a script tag
    or data attribute, and markdown conversion strips both. Rillet's careers
    page renders only `[Open positions](#ashby_embed)` in markdown, while the
    raw HTML carries `jobs.ashbyhq.com/rillet` outright.

    `execute_js` covers the harder variant: a board rendered entirely
    client-side, where even the raw pre-hydration HTML has no slug in it
    because the reference lives in a separate JS chunk. Counted as an
    executejs fetch, not a raw one, because it bills as a browser render.
    """
    key = os.environ.get("STRING_API_KEY")
    if not key:
        raise MissingKey("STRING_API_KEY missing from .env")
    payload = {"url": url, "format": "raw"}
    if execute_js:
        payload["executeJS"] = True
    FETCH_COUNTS["executejs" if execute_js else "raw"] += 1
    if ledger is not None:
        ledger.charge("string", 0)
    try:
        r = _session.post(
            STRING_FETCH_URL,
            headers={"Authorization": "Bearer " + key},
            json=payload,
            timeout=TIMEOUT + 15,
        )
        origin_status = int(r.headers.get("X-Status-Code", r.status_code))
        if origin_status < 400 and r.text and r.text.strip():
            return r.text
        if not quiet:
            _provider_error(ledger, "string", context or url,
                            "origin HTTP {}{}".format(
                                origin_status,
                                "" if (r.text and r.text.strip()) else " (empty body)"))
        return None
    except (requests.RequestException, ValueError) as exc:
        if not quiet:
            _provider_error(ledger, "string", context or url, "request failed: {}".format(exc))
        return None


def string_fetch_escalating(url: str) -> Optional[str]:
    """Plain fetch first, then executeJS. Only two rungs - `actions` runs a real
    browser and is far too slow/expensive for a batch this wide."""
    text = string_fetch(url)
    if text and len(text.strip()) > 200:
        return text
    return string_fetch(url, execute_js=True) or text


# --------------------------------------------------------------------------
# jina (r.jina.ai) - free fetch rung, ahead of String's paid executeJS
# escalation. Phase 5. No key required; an optional JINA_API_KEY in .env
# raises Jina's rate limit but the pipeline works without one.
# --------------------------------------------------------------------------

def jina_fetch(url: str, context: str = "", ledger: Optional["CreditLedger"] = None,
               quiet: bool = False) -> Optional[str]:
    """Free markdown fetch via Jina's Reader API. Mirrors string_fetch's
    shape (markdown text or None) so callers can slot it in ahead of String
    without special-casing the return value."""
    headers = {}
    key = os.environ.get("JINA_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        r = _session.get(JINA_READER_BASE + url, headers=headers, timeout=TIMEOUT)
        if ledger is not None:
            ledger.charge("jina", 0)
        if r.status_code < 400 and r.text and r.text.strip():
            return r.text
        if not quiet:
            _provider_error(ledger, "jina", context or url,
                            "HTTP {}{}".format(r.status_code,
                                                "" if (r.text and r.text.strip()) else " (empty body)"))
        return None
    except requests.RequestException as exc:
        if ledger is not None:
            ledger.charge("jina", 0)
        if not quiet:
            _provider_error(ledger, "jina", context or url, "request failed: {}".format(exc))
        return None


# --------------------------------------------------------------------------
# QuickEnrich
# --------------------------------------------------------------------------

def _qe_headers() -> Dict[str, str]:
    key = os.environ.get("QUICKENRICH_API_KEY")
    if not key:
        raise MissingKey("QUICKENRICH_API_KEY missing from .env")
    return {"Authorization": "Bearer " + key, "Accept": "application/json"}


def qe_contact_finder(domain: str, titles: List[str], ledger: CreditLedger) -> List[Dict[str, Any]]:
    """FREE discovery (credits_used: 0). Returns the finance roster with
    has_email/has_phone flags but no email values.

    This is deliberately the first rung. Ranking seniority costs nothing here,
    so only the single best contact ever consumes a paid lookup. It also avoids
    the has_email trap documented in CLAUDE.md: we pull the full roster and
    filter locally rather than letting the API drop people with no email on file.
    """
    body = {
        "company_url": {"include": [domain], "exclude": []},
        "title": {"include": titles, "exclude": []},
        "per_page": 100,
    }
    try:
        r = _session.post(
            QE_BASE + "/employees/contact-finder",
            headers=dict(_qe_headers(), **{"Content-Type": "application/json"}),
            json=body,
            timeout=TIMEOUT,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "quickenrich/contact-finder", domain,
                        "request failed: {}".format(exc))
        return []
    if r.status_code >= 400 or data.get("error") or data.get("errors"):
        _provider_error(ledger, "quickenrich/contact-finder", domain,
                        "HTTP {} {}".format(r.status_code,
                                            data.get("error") or data.get("errors") or ""))
        return []
    meta = data.get("meta") or {}
    ledger.charge("quickenrich", meta.get("credits_used", 0), meta.get("remaining_credits"))
    return data.get("data") or []


def qe_email_search(ledger: CreditLedger, linkedin_url: Optional[str] = None,
                    company_url: Optional[str] = None, first_name: Optional[str] = None,
                    last_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Paid: resolves one person to an email. LinkedIn URL is the exact-match
    path and is preferred; name+domain is the fallback."""
    params = {}
    if linkedin_url:
        params["linkedin_url"] = linkedin_url
    if company_url and first_name and last_name:
        params.update({"company_url": company_url, "first_name": first_name, "last_name": last_name})
    if not params:
        return None
    try:
        r = _session.get(QE_BASE + "/employees/search", headers=_qe_headers(),
                         params=params, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "quickenrich/email-search",
                        company_url or linkedin_url or "?", "request failed: {}".format(exc))
        return None
    # A 404 here is a legitimate "not in dataset", not a fault. Anything else is.
    if r.status_code >= 400 and r.status_code != 404:
        _provider_error(ledger, "quickenrich/email-search",
                        company_url or linkedin_url or "?",
                        "HTTP {} {}".format(r.status_code, str(data)[:120]))
        return None
    meta = data.get("meta") or {}
    ledger.charge("quickenrich", meta.get("credits_used", 0), meta.get("remaining_credits"))
    rec = data.get("data")
    if isinstance(rec, list):
        rec = rec[0] if rec else None
    if not rec or not _clean_email(rec.get("email")):
        return None
    return rec


# --------------------------------------------------------------------------
# Apollo
#
# The brief specifies Apollo MCP (apollo_mixed_people_api_search +
# apollo_people_match). No Apollo MCP server is configured in this project
# (.mcp.json has string-ai only), so this uses the REST API with APOLLO_API_KEY
# from .env. Verified live: /people/match and /people/bulk_match work on this key;
# /mixed_people/search returns API_INACCESSIBLE - the key is not scoped for it.
# Re-verified 2026-09-09 against a replacement key: still out of scope.
#
# Phase 5 (2026-09-16): apollo_people_search below adds the /mixed_people/search
# discovery call anyway, since Rachel confirmed Apollo has a real cheaper
# discovery mode worth using once this key is re-scoped or upgraded -- but it
# is UNVERIFIED against a live call as of this writing, given the history
# above. If it still returns API_INACCESSIBLE, find_contact_generic's Stage A
# falls through exactly like any other empty result -- it does not crash the
# run. Re-verifying this is the open Apollo wiring audit (Phase 7).
#
# Pacing: free-plan rate limit is 50 requests/min, shared per team across
# both endpoints' own daily/hourly counters -- see _pace_apollo below.
# --------------------------------------------------------------------------

_apollo_last_call = [0.0]
_APOLLO_MIN_INTERVAL = 60.0 / 50  # 50 requests/min free-plan limit


def _pace_apollo() -> None:
    """Simple client-side governor for Apollo's 50/min free-plan rate limit.
    This pipeline calls Apollo sequentially, never concurrently, so a fixed
    minimum interval between calls is enough -- no token-bucket needed."""
    elapsed = time.monotonic() - _apollo_last_call[0]
    if elapsed < _APOLLO_MIN_INTERVAL:
        time.sleep(_APOLLO_MIN_INTERVAL - elapsed)
    _apollo_last_call[0] = time.monotonic()


def _apollo_headers() -> Dict[str, str]:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise MissingKey("APOLLO_API_KEY missing from .env")
    return {"x-api-key": key, "Content-Type": "application/json", "Accept": "application/json"}


def apollo_match(ledger: CreditLedger, first_name: Optional[str] = None,
                 last_name: Optional[str] = None, domain: Optional[str] = None,
                 linkedin_url: Optional[str] = None,
                 apollo_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Stage B enrichment rung: name/LinkedIn -> email reveal. Charges the
    ledger 1 credit ONLY when Apollo reports the email as verified -- per
    Rachel's confirmed vendor terms (2026-09-16), an unverified email costs
    nothing. Still returned either way (Scrubby verification catches a bad
    email downstream regardless of Apollo's own verification status).

    apollo_id, when given, is Apollo's own internal person id (from
    apollo_people_search's discovery results) and takes over the request
    entirely -- confirmed live 2026-09-16 that `{"id": apollo_id}` reveals
    that exact person, real unobfuscated name included. This is more
    precise than name+domain matching and is what find_contact_generic's
    Apollo-discovery candidates use; name/domain/linkedin_url stay the path
    for candidates discovered by QuickEnrich or web_research, which never
    have an Apollo id to begin with.
    """
    if apollo_id:
        body = {"id": apollo_id}
    else:
        body = {}
        if linkedin_url:
            body["linkedin_url"] = linkedin_url
        if first_name and last_name:
            body.update({"first_name": first_name, "last_name": last_name})
        if domain:
            body["domain"] = domain
    if not body:
        return None
    _pace_apollo()
    try:
        r = _session.post(APOLLO_BASE + "/people/match", headers=_apollo_headers(),
                          json=body, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "apollo/people-match", domain or linkedin_url or "?",
                        "request failed: {}".format(exc))
        return None
    if r.status_code >= 400 or data.get("error"):
        _provider_error(ledger, "apollo/people-match", domain or linkedin_url or "?",
                        "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:120]))
        return None
    person = data.get("person") or {}
    email = _clean_email(person.get("email"))
    if not email:
        return None
    if (person.get("email_status") or "").strip().lower() == "verified":
        ledger.charge("apollo", 1)
    else:
        ledger.charge("apollo", 0)  # attempt logged, no charge -- not a verified net-new email
    return person


def apollo_people_search(ledger: CreditLedger, domain: str, titles: List[str],
                         per_page: int = 10) -> List[Dict[str, Any]]:
    """Stage A discovery rung: organization/people search by title + domain.

    Confirmed working live 2026-09-16 after Rachel's key was granted
    broader (master) access. History: originally wired to
    `/mixed_people/search` per Apollo's docs at scope time, which then
    returned HTTP 403 twice (key not scoped for it). After the key upgrade
    it returned HTTP 422 instead: Apollo has since deprecated that path in
    favor of `/mixed_people/api_search`, which is what this calls now.

    Real data-quality note, found live: results are NOT tightly filtered to
    one real company. A microsoft.com search for "CFO" returned three
    different people all tagged organization.name="Microsoft", and only one
    of them (once revealed via apollo_id below) was actually Microsoft's
    real CFO. Apollo's own org-domain matching on this endpoint is loose --
    this is exactly why find_contact_generic takes the first *qualifying*
    result and hands it to Stage B for a real reveal, rather than trying to
    rank or pick the "best" one from search data alone.

    IMPORTANT: this endpoint is search, not reveal -- it does NOT return
    email, linkedin_url, or an unobfuscated last name (Apollo masks it,
    e.g. "Ka***n"). What it does return is `id`, Apollo's own internal
    person id, which is the precise handoff to Stage B:
    enrich_contact_generic passes it straight to apollo_match(apollo_id=...),
    which reveals that exact person by id -- not a fuzzy name+domain
    re-match that could land on a different, wrong same-titled person.
    Returns [] on any failure, same as every other provider here, so the
    waterfall falls through cleanly.
    """
    _pace_apollo()
    body = {
        "q_organization_domains": domain,
        "person_titles": titles,
        "page": 1,
        "per_page": per_page,
    }
    try:
        r = _session.post(APOLLO_BASE + "/mixed_people/api_search", headers=_apollo_headers(),
                          json=body, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "apollo/people-search", domain, "request failed: {}".format(exc))
        return []
    if r.status_code >= 400 or data.get("error"):
        _provider_error(ledger, "apollo/people-search", domain,
                        "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:150]))
        return []
    people = data.get("people") or []
    ledger.charge("apollo", 0)  # search itself isn't the verified-email charge
    return [
        {"apollo_id": p.get("id") or "", "first_name": p.get("first_name") or "",
         "last_name_obfuscated": p.get("last_name_obfuscated") or "",
         "title": p.get("title") or ""}
        for p in people
    ]


# --------------------------------------------------------------------------
# Hunter
# --------------------------------------------------------------------------

def _provider_error(ledger: Optional[CreditLedger], provider: str, subject: str,
                    detail: str) -> None:
    """Surface a provider-side failure instead of returning a bare {}.

    An empty result is indistinguishable from a genuine "no data for this
    company", so swallowing the error turns a broken request into a confident
    negative. That single habit hid three separate bugs in the 2026-09-10
    batch: this Hunter cap, GlossGenius's 404 slug, and the run-wide
    assumption that 20 companies simply had no finance contact.
    """
    msg = "{} error for {}: {}".format(provider, subject, detail)
    print("    !! " + msg, flush=True)
    if ledger is not None:
        ledger.warnings.append(msg)


# Hunter 400s on a limit above the plan's per-search email cap. The Free plan
# caps at 10; raising this requires a paid plan, not just a bigger number.
HUNTER_EMAIL_LIMIT = 10


def hunter_domain_search(ledger: CreditLedger, domain: str,
                         department: str = "finance") -> Dict[str, Any]:
    """No longer in the live Stage A/B waterfall as of Phase 5 -- superseded
    by hunter_email_finder (real Email Finder endpoint, used once a name is
    already known) and apollo_people_search/web_research (discovery). Left
    in place, not deleted, since it still works and nothing is served by
    removing a working function; just flagging it's not what
    find_contact_generic/enrich_contact_generic call anymore.

    Sending limit=20 on a Free plan returned HTTP 400 pagination_error for
    every company in the 2026-09-10 batch. Because the error was swallowed,
    that produced 20 false "no finance contact found" results while charging
    0 credits and emitting no warning.
    """
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        raise MissingKey("HUNTER_API_KEY missing from .env")
    params = {"domain": domain, "department": department,
              "limit": HUNTER_EMAIL_LIMIT, "api_key": key}
    try:
        r = _session.get(HUNTER_BASE + "/domain-search", params=params, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "hunter", domain, "request failed: {}".format(exc))
        return {}
    if data.get("errors"):
        detail = "; ".join(
            "{} [{}]".format(e.get("details") or e.get("id") or "?",
                             e.get("code") or r.status_code)
            for e in data["errors"])
        _provider_error(ledger, "hunter", domain, detail)
        return {}
    ledger.charge("hunter", 1)
    return data.get("data") or {}


def hunter_email_finder(ledger: CreditLedger, domain: str, first_name: str,
                        last_name: str) -> Optional[Dict[str, Any]]:
    """Stage B rung 2 (Phase 5): the real Email Finder endpoint -- name +
    domain -> one email -- not hunter_domain_search's whole-roster pull.
    Hunter's tiny 50/mo pool is exactly why this only runs once a name is
    already known (find_contact_generic has already resolved one), never as
    a discovery rung."""
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        raise MissingKey("HUNTER_API_KEY missing from .env")
    params = {"domain": domain, "first_name": first_name, "last_name": last_name, "api_key": key}
    try:
        r = _session.get(HUNTER_BASE + "/email-finder", params=params, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "hunter/email-finder", domain, "request failed: {}".format(exc))
        return None
    if data.get("errors"):
        detail = "; ".join(
            "{} [{}]".format(e.get("details") or e.get("id") or "?",
                             e.get("code") or r.status_code)
            for e in data["errors"])
        _provider_error(ledger, "hunter/email-finder", domain, detail)
        return None
    ledger.charge("hunter", 1)
    return data.get("data") or {}


def hunter_account(ledger: Optional[CreditLedger] = None) -> Dict[str, Any]:
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        return {}
    try:
        r = _session.get(HUNTER_BASE + "/account", params={"api_key": key}, timeout=TIMEOUT)
        if r.status_code >= 400:
            _provider_error(ledger, "hunter/account", "balance check",
                            "HTTP {} {}".format(r.status_code, r.text[:100]))
            return {}
        return (r.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "hunter/account", "balance check",
                        "request failed: {}".format(exc))
        return {}


def qe_balance() -> Optional[int]:
    """Read the QuickEnrich balance off a free contact-finder call."""
    try:
        r = _session.post(
            QE_BASE + "/employees/contact-finder",
            headers=dict(_qe_headers(), **{"Content-Type": "application/json"}),
            json={"company_url": {"include": ["example.com"], "exclude": []}, "per_page": 1},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            _provider_error(None, "quickenrich/balance", "balance check",
                            "HTTP {} {}".format(r.status_code, r.text[:100]))
            return None
        return ((r.json() or {}).get("meta") or {}).get("remaining_credits")
    except (requests.RequestException, ValueError) as exc:
        _provider_error(None, "quickenrich/balance", "balance check",
                        "request failed: {}".format(exc))
        return None


_PLACEHOLDER_EMAIL = re.compile(r"email_not_unlocked|not_unlocked|^n/?a$", re.I)


def _clean_email(value: Any) -> Optional[str]:
    """Reject the placeholder strings these APIs use for 'no email on file'."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.upper() == "N/A" or _PLACEHOLDER_EMAIL.search(v) or "@" not in v:
        return None
    return v


# --------------------------------------------------------------------------
# Shared, persona-agnostic contact utilities (moved here from cfo_enrich.py
# in Phase 5, 2026-09-16 -- none of this depends on which title tier
# matched, so it belongs where every persona's waterfall can reach it, not
# inside the CFO-specific module).
# --------------------------------------------------------------------------

_LINKEDIN_URL = re.compile(r"linkedin\.com/(?:in|pub)/[A-Za-z0-9\-_%.]+", re.I)


def _clean_linkedin(value: Any) -> Optional[str]:
    """Reject placeholder LinkedIn values the same way _clean_email does.

    QuickEnrich returns the literal string "N/A" when it holds no LinkedIn for
    a person. Passing that through as `linkedin_url` makes the QuickEnrich
    email-search return HTTP 422 "Validation failed", which aborts the entire
    lookup - so the name+domain fallback never runs either. On the 2026-09-10
    83-company batch that cost Pave and Normal Computing their contacts
    outright, both qualified companies, and the failure was silent until
    provider errors started being logged.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.upper() in ("N/A", "NA", "NONE", "NULL", "-"):
        return None
    return v if _LINKEDIN_URL.search(v) else None


def _full_name(r: Dict[str, Any]) -> str:
    return ((r.get("first_name") or "") + " " + (r.get("last_name") or "")).strip()


def _domain_only(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").strip()).rstrip("/")
    return d.split("/")[0]


_TLD_SUFFIX = re.compile(
    r"\.(?:com|io|ai|co|net|org|dev|app|xyz|tech|inc|so|sh|gg|us|uk|health|security)$", re.I)

# Trailing corporate-entity words, stripped after the TLD so "Acme Inc" and
# "Acme" compare equal. Order matters only in that longer words are checked
# first where they could otherwise be masked by a shorter one, but none of
# these overlap that way in practice.
_CORP_SUFFIX_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "group", "llc", "ltd", "co",
    "company", "holdings",
}


def _registrable_root(domain: str) -> str:
    """Domain root with the TLD and a trailing corporate-entity word
    stripped, punctuation collapsed. "acme.com" and "acme-inc.com" reduce
    to the same string; two genuinely different companies do not."""
    d = _TLD_SUFFIX.sub("", (domain or "").strip().lower())
    parts = [p for p in re.split(r"[^a-z0-9]+", d) if p]
    while parts and parts[-1] in _CORP_SUFFIX_TOKENS:
        parts.pop()
    return "".join(parts)


# Below this string-similarity score, two domains are confidently unrelated
# companies -- the existing hard WARNING. Not calibrated against a labelled
# set of real mismatches (none exist yet); picked as a conservative middle
# value and worth revisiting once a few real runs go through it.
_DOMAIN_MISMATCH_FLOOR = 0.5


def _agency_domain_flag(email: str, company_domain: str) -> str:
    """CLAUDE.md: an email on an unrelated domain is an agency contractor or a
    stale row, not an employee.

    Three-tier check, in order:
      1. Substring match on the raw domains (subdomains, exact matches) --
         the fast path for the common case, unchanged from before.
      2. Normalized-root match, TLD and a trailing corporate-entity word
         stripped -- so "acme.com" and "acme-inc.com" or "acmegroup.io"
         read as the same company instead of failing (1) on formatting
         alone. This was a real false-positive gap in the old substring-only
         check.
      3. Only once both of those fail: a string-similarity score on the
         normalized roots. Below the floor, same hard WARNING as before.
         Above it but still not a match, a distinct VERIFY BY EYE flag --
         genuinely ambiguous cases get a human glance, not a silent guess
         either way.
    """
    if not email or "@" not in email:
        return ""
    email_domain = email.split("@")[-1].lower()
    root = _domain_only(company_domain).lower().replace("www.", "")
    if not root:
        return ""
    if root in email_domain or email_domain in root:
        return ""

    root_a, root_b = _registrable_root(root), _registrable_root(email_domain)
    if root_a and root_b and root_a == root_b:
        return ""

    score = difflib.SequenceMatcher(None, root_a, root_b).ratio() if (root_a and root_b) else 0.0
    if score < _DOMAIN_MISMATCH_FLOOR:
        return " | WARNING: email domain ({}) does not match company domain ({})".format(
            email_domain, root)
    return (" | VERIFY BY EYE: email domain ({}) is a partial match to company domain ({}) "
            "- not clearly the same company, not clearly a different one "
            "(similarity {:.2f})").format(email_domain, root, score)


# --------------------------------------------------------------------------
# Person identity + company-domain matching (Phase 17).
#
# Two distinct failure modes, both found in real Fibery data:
#
#   1. One human reaches Fibery twice under two spellings, because two
#      providers name them differently -- QuickEnrich's "Samantha Hollis"
#      beside Apollo's "Sam Hollis", or Apollo's masked "Robert G" beside
#      Hunter's "Robert Gorski". Email-based dedupe cannot catch these: the
#      two records routinely carry *different* emails (Whitlock), or one
#      carries a stale one (Gorski's formerco.io address).
#   2. A contact's email sits on a domain that isn't the company's at all
#      (contractco.io on an Initech record, priorco.io on a globex.co
#      one) -- the agency-contractor/stale-row case CLAUDE.md already
#      describes.
#
# These two helpers are deliberately conservative in OPPOSITE directions.
# same_person() must never merge two different people, because the merge
# silently drops one of them from outreach entirely. By contrast
# email_domain_matches_company() must never reject on absent data, because
# a blank domain is missing information, not evidence of a mismatch.
# --------------------------------------------------------------------------

# Nickname <-> formal-name equivalence. Each group holds first names that can
# refer to the same person.
#
# The groups are DISJOINT on purpose. A name appearing in two groups chains
# them together transitively -- a ("ted", "edward") group beside an
# ("ed", "edward", "eddie") one would quietly make "ted" and "eddie" the same
# person -- so genuine ambiguity is resolved by leaving the contested name
# out, never by merging the groups. tests/test_contact_dedupe.py asserts the
# disjointness, so a careless addition fails loudly instead of silently
# over-merging real people. Add entries here as real pairs turn up.
_NICKNAME_GROUPS = (
    ("abby", "abigail"),
    ("alex", "alexander", "alexandra"),
    ("andy", "andrew", "drew"),
    ("ben", "benjamin"),
    ("beth", "elizabeth", "liz", "lizzie", "betsy"),
    ("cathy", "catherine"),
    ("charlie", "charles", "chuck"),
    ("chris", "christopher", "christina", "christine"),
    ("dan", "daniel", "danny"),
    ("dave", "david"),
    ("deb", "debbie", "deborah", "debra"),
    ("gabe", "gabriel"),
    ("greg", "gregory"),
    ("jeff", "jeffrey", "geoff", "geoffrey"),
    ("jen", "jennifer", "jenny"),
    ("jess", "jessica"),
    ("jim", "james", "jimmy"),
    ("joe", "joseph", "joey"),
    ("josh", "joshua"),
    ("kate", "katherine", "kathryn", "katie"),
    ("ken", "kenneth"),
    ("larry", "lawrence"),
    ("matt", "matthew"),
    ("meg", "megan", "maggie", "margaret"),
    ("mike", "michael", "mick"),
    ("nate", "nathan", "nathaniel"),
    ("nick", "nicholas", "nicole"),
    ("pat", "patrick", "patricia"),
    ("pete", "peter"),
    ("phil", "philip", "phillip"),
    ("rich", "richard", "rick"),
    ("rob", "robert", "bob", "bobby", "robbie"),
    ("ron", "ronald"),
    ("sam", "samantha", "samuel"),
    ("steve", "stephen", "steven"),
    ("sue", "susan", "suzanne"),
    ("tim", "timothy"),
    ("tom", "thomas", "tommy"),
    ("tony", "anthony"),
    ("vicky", "victoria"),
    ("will", "william", "bill", "billy"),
    ("zach", "zachary"),
)

_FIRST_NAME_CANONICAL = {
    variant: group[0] for group in _NICKNAME_GROUPS for variant in group
}

# Apollo's people-search results mask the last name ("Ka***n"), and a Fibery
# row written from one reads as a bare initial ("Robert G"). Either form
# carries a usable first letter but can never be compared as a full name.
_OBFUSCATED_LAST = re.compile(r"[*•]")


def normalize_first_name(name: str) -> str:
    """Lowercased, punctuation-stripped first name mapped to its nickname
    group's canonical form, so "Sam" and "Samantha" compare equal. A name in
    no group normalizes to itself."""
    n = re.sub(r"[^a-z]", "", (name or "").strip().lower())
    return _FIRST_NAME_CANONICAL.get(n, n)


def split_person_name(full: str) -> Tuple[str, str]:
    """"Samantha Hollis" -> ("Samantha", "Hollis"). Everything after the
    first token is the last name, so "Robert G" -> ("Robert", "G") and a
    multi-word surname ("van der Berg") stays intact."""
    parts = (full or "").split()
    if not parts:
        return ("", "")
    return (parts[0], " ".join(parts[1:]))


def _last_name_form(last: str) -> Tuple[str, str]:
    """Classify a last name as ("full", normalized), ("initial", letter) or
    ("none", ""). An initial is a single letter ("G", "G.") or an
    Apollo-masked value ("Ka***n") -- both reduce to their first letter,
    which is all either form can honestly support."""
    raw = (last or "").strip().lower()
    if not raw:
        return ("none", "")
    letters = re.sub(r"[^a-z]", "", raw)
    if not letters:
        return ("none", "")
    if _OBFUSCATED_LAST.search(raw) or len(letters) == 1:
        return ("initial", letters[0])
    return ("full", letters)


def _normalize_linkedin(url: str) -> str:
    """linkedin.com/in/<slug> reduced to the bare slug, so scheme, www,
    trailing slash and query string don't make one profile look like two."""
    m = _LINKEDIN_URL.search(url or "")
    if not m:
        return ""
    return m.group(0).rstrip("/").split("/")[-1].lower()


def _person_name_parts(d: Dict[str, Any]) -> Tuple[str, str]:
    """(first, last) from a contact-ish dict, whichever keys it happens to
    carry. Explicit first_name/last_name win; an Apollo candidate's
    last_name_obfuscated fills in for a blank last name; a single `name` /
    `contact_name` string (what every Fibery row carries) is split."""
    first = (d.get("first_name") or "").strip()
    last = (d.get("last_name") or "").strip()
    if not last:
        last = str(d.get("last_name_obfuscated") or "").strip()
    if not (first and last):
        fallback_first, fallback_last = split_person_name(
            d.get("name") or d.get("contact_name") or "")
        first = first or fallback_first
        last = last or fallback_last
    return (first, last)


def same_person(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether two contact-ish dicts describe the same human.

    True when EITHER:
      - both carry the same LinkedIn profile slug, or
      - the first names match nickname-aware AND the last names are
        compatible -- both full and equal, or one a bare/masked initial
        matching the other's first letter.

    A missing last name on either side is never enough on its own. Two
    different people share a first name far too often (Peter Lindqvist and Peter
    Oduya are both genuinely at Northwind), and merging them would drop one
    of them from outreach with no trace.

    Two *differing* LinkedIn URLs are deliberately NOT treated as proof of
    two different people -- one of the records may simply hold a wrong or
    stale profile -- so a name match can still carry the comparison.
    """
    li_a = _normalize_linkedin(a.get("linkedin") or "")
    li_b = _normalize_linkedin(b.get("linkedin") or "")
    if li_a and li_b and li_a == li_b:
        return True

    first_a, last_a = _person_name_parts(a)
    first_b, last_b = _person_name_parts(b)
    if not first_a or not first_b:
        return False
    if normalize_first_name(first_a) != normalize_first_name(first_b):
        return False

    form_a, val_a = _last_name_form(last_a)
    form_b, val_b = _last_name_form(last_b)
    if form_a == "none" or form_b == "none":
        return False
    if form_a == "full" and form_b == "full":
        return val_a == val_b
    # At least one side is an initial, so a first-letter comparison is the
    # most either side can actually support.
    return val_a[0] == val_b[0]


# Legitimate alternate domains for one company -- a rebrand, or a separate
# careers/marketing domain -- where an email on the "other" domain still
# belongs to a real employee. Deliberately tiny and hand-curated: every entry
# is a company someone has actually confirmed, never a guess. dbt Labs is the
# one already documented in this repo (CLAUDE.md records getdbt.com serving
# its careers pages). Compared on registrable roots, so the TLD is irrelevant.
DOMAIN_ALIAS_GROUPS = (
    frozenset({"getdbt", "dbtlabs"}),
)


def email_domain_matches_company(email: str, company_domain: str) -> bool:
    """Whether `email` plausibly belongs to an employee of the company at
    `company_domain`.

    Returns True whenever there is nothing to judge against: a blank company
    domain or a blank email is missing data, and must never be the reason a
    real contact is dropped.

    Same comparison ladder as _agency_domain_flag (raw substring, then
    normalized registrable root) plus the curated alias groups above, but
    collapsed to a hard boolean. The difference in use matters: this gates
    whether a record is written at all, where _agency_domain_flag only
    annotates a CSV column for a human to read.
    """
    if not email or "@" not in email:
        return True
    root = _domain_only(company_domain).lower().replace("www.", "")
    if not root:
        return True
    email_domain = email.split("@")[-1].strip().lower()
    if not email_domain:
        return True
    if root in email_domain or email_domain in root:
        return True
    root_a, root_b = _registrable_root(root), _registrable_root(email_domain)
    if root_a and root_b and root_a == root_b:
        return True
    if root_a and root_b:
        for group in DOMAIN_ALIAS_GROUPS:
            if root_a in group and root_b in group:
                return True
    return False


# --------------------------------------------------------------------------
# Scrubby - email verification for Apollo/Hunter-sourced contacts.
#
# CLAUDE.md: strictly for addresses constructed outside QuickEnrich. Anything
# QuickEnrich returns already carries an email_verification_date and must
# not be re-verified here -- that's real credits spent confirming something
# already confirmed.
# --------------------------------------------------------------------------

def _scrubby_headers() -> Dict[str, str]:
    key = os.environ.get("SCRUBBY_API_KEY")
    if not key:
        raise MissingKey("SCRUBBY_API_KEY missing from .env")
    # CLAUDE.md: auth is the X-API-Key header, NOT Authorization: Bearer --
    # Bearer returns 401 {"detail":"Invalid API key"} even with a valid key.
    return {"X-API-Key": key, "Content-Type": "application/json"}


def scrubby_validate_email(ledger: Optional[CreditLedger], email: str) -> Optional[Dict[str, Any]]:
    """Verify one email via Scrubby's /validate_email.

    Returns the raw response dict on success -- fields are email, status,
    result, quick_status, credits_used, remaining_credits,
    retry_after_seconds per CLAUDE.md. This is NOT the same field set as
    the Scrubby web UI (no overall_score, no is_catch_all, no mx_records),
    so don't expect those. Returns None on any request failure or non-2xx
    response, logged via _provider_error the same as every other provider
    here rather than silently swallowed.
    """
    try:
        r = _session.post(SCRUBBY_BASE + "/validate_email", headers=_scrubby_headers(),
                          json={"email": email}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        _provider_error(ledger, "scrubby", email, "request failed: {}".format(exc))
        return None
    try:
        data = r.json()
    except ValueError:
        _provider_error(ledger, "scrubby", email, "non-JSON response: {}".format(r.text[:200]))
        return None
    if r.status_code >= 400:
        _provider_error(ledger, "scrubby", email,
                        "HTTP {}: {}".format(r.status_code, r.text[:200]))
        return None
    if ledger is not None:
        ledger.charge("scrubby", data.get("credits_used") or 1,
                      remaining=data.get("remaining_credits"))
    return data


# Confirmed against Scrubby's own published API reference (2026-09-16) --
# result: Valid/Invalid/Risky/Unknown. quick_status and status: OK/
# HARD_BOUNCE/SOFT_BOUNCE/UNKNOWN. No real call made from this code, but
# these are Scrubby's documented enum values, not a guess. "Risky"
# (deliverable but risky) and "SOFT_BOUNCE" (temporarily undeliverable)
# are deliberately their own verdict, not folded into valid or invalid --
# CLAUDE.md's own pattern is to flag ambiguous things for a human glance
# rather than silently deciding, and a soft bounce / risky address isn't
# a clean yes or no. Kept as its own function on purpose so a correction
# is a one-function fix, not a hunt through the pipeline.
_SCRUBBY_VALID_VALUES = {"valid", "ok"}
_SCRUBBY_INVALID_VALUES = {"invalid", "hard_bounce"}
_SCRUBBY_RISKY_VALUES = {"risky", "soft_bounce"}


def scrubby_classify(response: Optional[Dict[str, Any]]) -> str:
    """Collapse status/result/quick_status into one verdict: 'valid',
    'invalid', 'risky', or 'unknown'. See the note above -- this is the
    one place that interprets Scrubby's response, so a correction only
    needs to happen here."""
    if not response:
        return "unknown"
    for field in ("status", "result", "quick_status"):
        v = (response.get(field) or "").strip().lower()
        if v in _SCRUBBY_VALID_VALUES:
            return "valid"
        if v in _SCRUBBY_INVALID_VALUES:
            return "invalid"
        if v in _SCRUBBY_RISKY_VALUES:
            return "risky"
    return "unknown"


def scrubby_domain_is_catch_all(ledger: Optional[CreditLedger], domain: str) -> Optional[bool]:
    """CLAUDE.md's catch-all control: before trusting any 'valid' result on
    a domain, test one deliberately fake address. A hard bounce there
    confirms the domain isn't catch-all, so other 'valid' results on it
    mean something; anything else means every 'valid' on that domain is
    ambiguous. Returns True/False, or None if the probe itself failed (in
    which case the caller should treat the domain as unconfirmed, not as
    "not catch-all").

    Callers should run this once per domain per session, not once per
    email -- see the _catch_all_cache pattern in main.py's run().
    """
    probe = "nonsense123@" + domain
    data = scrubby_validate_email(ledger, probe)
    if data is None:
        return None
    verdict = scrubby_classify(data)
    # A hard bounce (invalid) on a fake address means the domain rejects
    # unknown mailboxes -- not catch-all. A "valid"/"unknown" verdict on a
    # fake address means the domain accepts anything -- catch-all.
    return verdict != "invalid"


# --------------------------------------------------------------------------
# exa - paid escalation search engine. Used only when the Anthropic
# web-search pass below comes back empty: a second, different index rather
# than retrying the same one. Phase 5. Budget-capped at 1,000/mo (free pool).
# REST API directly, not MCP -- this pipeline is a batch script calling
# providers synchronously, same reasoning as everywhere else in this file.
# --------------------------------------------------------------------------

def exa_search(query: str, ledger: Optional[CreditLedger] = None,
              num_results: int = 5) -> List[Dict[str, Any]]:
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise MissingKey("EXA_API_KEY missing from .env")
    if ledger is not None and ledger.exa_budget_exhausted():
        return []
    try:
        r = _session.post(
            EXA_BASE + "/search",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"query": query, "numResults": num_results, "contents": {"text": True}},
            timeout=TIMEOUT,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "exa", query[:60], "request failed: {}".format(exc))
        return []
    if r.status_code >= 400:
        _provider_error(ledger, "exa", query[:60], "HTTP {} {}".format(r.status_code, str(data)[:150]))
        return []
    results = data.get("results") or []
    if ledger is not None:
        ledger.charge("exa", len(results) or 1)
    return results


def verify_excerpt_on_page(url: str, excerpt: str, ledger: Optional[CreditLedger] = None) -> bool:
    """Citation-verification step for research_stated_goal() (company_research.py):
    confirms an LLM-reported excerpt genuinely appears on the page it claims
    to be from, rather than trusting a self-reported quote. Reuses the same
    free-before-paid fetch chain as everything else (jina, then String) --
    this is a read, not a new cost center.

    Loose match: whitespace collapsed on both sides so a reflow/line-wrap
    difference between the quote and the live page doesn't fail a real
    match. Still requires the actual words to appear in order -- this is
    what catches a paraphrase or fabricated citation dressed up as a quote.
    """
    if not url or not excerpt:
        return False
    text = jina_fetch(url, context="stated-goal verification", ledger=ledger, quiet=True)
    if not text:
        text = string_fetch(url, context="stated-goal verification", ledger=ledger, quiet=True)
    if not text:
        return False
    norm_page = re.sub(r"\s+", " ", text).lower()
    norm_excerpt = re.sub(r"\s+", " ", excerpt).strip().lower()
    return len(norm_excerpt) > 8 and norm_excerpt in norm_page


# --------------------------------------------------------------------------
# web_research / stated-goal research - the pipeline's first direct LLM call
# (Phase 5, 2026-09-16). Uses the Anthropic API's own server-side web-search
# tool (not this pipeline's browsing, not the interactive Claude Code
# session) so an unattended batch script can search the live web on its own.
# New dependency: `anthropic` in requirements.txt. Model comes from
# ANTHROPIC_MODEL in .env, same variable the old (removed) pipeline used.
# --------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # local import: only paid/free-LLM callers need this installed
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise MissingKey("ANTHROPIC_API_KEY missing from .env")
        _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


def spend_log_path() -> Path:
    """outputs/spend_YYYYMMDD.log -- shared by main.py's per-company ledger lines and the per-call
    Anthropic lines written below, so one file holds the whole picture of a day's spend. Resolved
    at call time so a run spanning midnight rolls over."""
    return Path("outputs") / "spend_{}.log".format(time.strftime("%Y%m%d"))


def _extract_usage(response: Any) -> Tuple[int, int, int]:
    """(input_tokens, output_tokens, web_search_requests) off an Anthropic response.

    Read defensively, by name, tolerating both attribute and dict shapes and a missing field: the
    SDK's usage block has changed between versions, and this sits in a cost-tracking path that must
    never be the reason a working run fails. A field that isn't there reads as 0.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0, 0

    def _get(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    in_tok = int(_get(usage, "input_tokens") or 0)
    out_tok = int(_get(usage, "output_tokens") or 0)
    server_tool_use = _get(usage, "server_tool_use")
    searches = int((_get(server_tool_use, "web_search_requests") or 0)
                   if server_tool_use is not None else 0)
    return in_tok, out_tok, searches


def record_anthropic_call(ledger: Optional[CreditLedger], call_site: str, model: Optional[str],
                          response: Any, log_path: Optional[Path] = None) -> None:
    """Record one Anthropic call: accumulate it on the ledger (when there is one) and append a
    flushed per-call line to the spend log.

    Never raises and never changes control flow. This is observability -- a bug in cost tracking
    must not be able to break a run that otherwise worked, so everything here is inside one
    try/except and a failure prints rather than propagates.
    """
    try:
        in_tok, out_tok, searches = _extract_usage(response)
        cost = anthropic_cost_usd(model, in_tok, out_tok, searches)
        if ledger is not None:
            ledger.charge_anthropic(call_site, model, in_tok, out_tok, searches)
        path = log_path or spend_log_path()
        line = ("{}\tanthropic\t{}\tmodel={}\tin={}\tout={}\tsearches={}\test=${:.6f}\n"
                .format(time.strftime("%Y-%m-%d %H:%M:%S"), call_site or "unknown",
                        model or "?", in_tok, out_tok, searches, cost))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception as exc:  # observability must never take down a run
        print("    !! could not record Anthropic usage for {}: {}".format(call_site, exc))


def _llm_web_search(prompt: str, ledger: Optional[CreditLedger] = None,
                    max_tokens: int = 1024, call_site: str = "research") -> Optional[str]:
    """Calls the Anthropic API with the native web-search tool on, returns the
    model's final text response (expected to be a JSON blob per the caller's
    prompt) or None on any failure. Logged to the ledger as "web_research" --
    free in vendor-credit terms (this is Claude usage), but tracked for
    visibility into what was tried.

    NOTE: the web-search tool's type string (web_search_20250305 below) is
    versioned by Anthropic and can change over time -- if this starts
    failing with an unrecognized-tool error, check
    console.anthropic.com/docs for the current string rather than assuming
    the whole approach is broken.
    """
    client = _get_anthropic_client()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # anthropic SDK's own exception hierarchy varies by version
        _provider_error(ledger, "web_research", prompt[:60], "request failed: {}".format(exc))
        if ledger is not None:
            ledger.charge("web_research", 0)
        return None
    if ledger is not None:
        ledger.charge("web_research", 0)
    record_anthropic_call(ledger, call_site, model, resp)
    text_blocks = [b.text for b in (resp.content or []) if getattr(b, "type", "") == "text"]
    if not text_blocks:
        return None
    return "\n".join(text_blocks).strip()


def _llm_extract(prompt: str, ledger: Optional[CreditLedger] = None,
                 max_tokens: int = 512, call_site: str = "extract") -> Optional[str]:
    """Plain (no web-search tool) LLM call -- used when the caller already
    has the source text in hand (e.g. an exa result) and only needs the
    model to read and extract from it, not go search again. Logged to the
    ledger the same way as _llm_web_search."""
    client = _get_anthropic_client()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _provider_error(ledger, "web_research", prompt[:60], "request failed: {}".format(exc))
        if ledger is not None:
            ledger.charge("web_research", 0)
        return None
    if ledger is not None:
        ledger.charge("web_research", 0)
    record_anthropic_call(ledger, call_site, model, resp)
    text_blocks = [b.text for b in (resp.content or []) if getattr(b, "type", "") == "text"]
    if not text_blocks:
        return None
    return "\n".join(text_blocks).strip()


def _parse_json_response(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The prompts here ask for strict JSON, but a model can still wrap it in
    prose or a code fence. Try a direct parse first, then pull the first
    {...} block out as a fallback rather than failing outright."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None


def web_research_find_contact(domain: str, title_query: List[str],
                              ledger: Optional[CreditLedger] = None) -> Optional[Dict[str, Any]]:
    """Stage A rung 2: identity-only web search for a person holding one of
    title_query's titles at this domain. Returns a name+title+LinkedIn
    candidate (never an email -- Stage B's job), or None."""
    titles_str = ", ".join(title_query)
    prompt = (
        "Search the web to find the name of the person who currently holds one of "
        "these titles at the company with domain {domain}: {titles}. "
        "Only report someone if you find a specific, current source (their LinkedIn "
        "profile, the company's own team/leadership page, or a press mention) naming "
        "them in that role. Do not guess based on a similar company or an outdated "
        "source. If you can't confirm a specific person, say so.\n\n"
        "Respond as JSON: {{\"found\": bool, \"first_name\": string|null, "
        "\"last_name\": string|null, \"title\": string|null, "
        "\"linkedin_url\": string|null}}"
    ).format(domain=domain, titles=titles_str)
    text = _llm_web_search(prompt, ledger=ledger, call_site="research")
    data = _parse_json_response(text)
    if not data or not data.get("found"):
        return None
    first, last = (data.get("first_name") or "").strip(), (data.get("last_name") or "").strip()
    if not first and not last:
        return None
    return {
        "first_name": first, "last_name": last,
        "name": (first + " " + last).strip(),
        "title": (data.get("title") or "").strip(),
        "linkedin": _clean_linkedin(data.get("linkedin_url")) or "",
    }


# --------------------------------------------------------------------------
# Phase 8: shared title classification. Promoted out of cfo_enrich.py
# (which the CFO persona used to own outright) so every persona's
# classify_fn -- CFO/Finance, People Ops, Recruiter/TA -- shares one
# "not a real decision-maker" rejection list and one tier-matching
# algorithm, parameterized only by each persona's own seniority ladder.
# Confirmed by Rachel (2026-09-17): the same role-nouns that disqualify a
# Finance Analyst also disqualify an HR Coordinator or a Recruiting
# Assistant, so this is one shared list, not a per-persona copy.
# --------------------------------------------------------------------------

NON_QUALIFYING = re.compile(
    r"\banalyst\b|\bcontroller\b|bookkeep|\baccountant\b"
    r"|accounts (?:payable|receivable)"
    r"|\bintern\b|\binternship\b|\bcoordinator\b|\bassistant\b"
    r"|\breceptionist\b|\bclerk\b", re.I)


def classify_by_tiers(title: str, tiers: List[Tuple[int, str, "re.Pattern"]],
                      non_qualifying: "re.Pattern" = NON_QUALIFYING) -> Optional[Dict[str, Any]]:
    """Map a raw title to {"rank": int, "tier": label} via the first tier
    pattern in `tiers` (rank, label, compiled regex; rank 1 = most senior)
    that matches, or None if nothing qualifies. Rejects first against
    `non_qualifying` even when a tier pattern would also have matched --
    "Senior Executive Assistant to CMO & CFO" matches \bcfo\b but is an
    EA, not the CFO. This check order (reject first, then classify) is
    what each persona's own classify_title() did individually before this
    was shared; now every persona's thin config module calls this one
    implementation with its own tier ladder.
    """
    if not title:
        return None
    t = title.strip()
    if non_qualifying.search(t):
        return None
    for rank, label, pattern in tiers:
        if pattern.search(t):
            return {"rank": rank, "tier": label}
    return None


# --------------------------------------------------------------------------
# Phase 5/8: the two persona-agnostic Layer 2 waterfalls. Every persona's
# thin wrapper (persona_finance.py / persona_people_ops.py /
# persona_recruiter_ta.py) calls these two with its own title_query and
# classify_fn -- title seniority is the only persona-specific piece.
# --------------------------------------------------------------------------

def find_contact_generic(domain: str, title_query: List[str],
                         classify_fn: Callable[[str], Optional[Dict[str, Any]]],
                         ledger: CreditLedger, dry_run: bool = False,
                         max_candidates: int = 3) -> Dict[str, Any]:
    """Stage A - contact discovery. classify_fn maps a raw title to
    {"rank": int, "tier": label} or None (reject) -- the only persona-
    specific input. Rungs run in cost order -- QuickEnrich free discovery
    -> web_research (LLM) -> Apollo discovery (paid) -- and the waterfall
    STOPS at the first rung that yields at least one candidate passing
    classify_fn. `max_candidates` is a cap WITHIN that rung, not a target
    to fill across rungs.

    Phase 17 change: the cap used to be a target. A rung returning 1
    qualifying candidate still fell through to web_research and then to
    Apollo discovery, trying to top up to 3 -- so a company QuickEnrich
    already had a real CFO for still cost an LLM call and a paid Apollo
    search. Those later rungs now run only when every earlier rung came
    back empty, which is what "only touch a more expensive rung than you
    need to" meant in the first place. The practical effect is fewer
    contacts per persona for some companies and no paid calls for most of
    them; one good contact from the free rung beats three assembled by
    paying for the privilege.

    Phase 8 history, still true within a rung: a rung's own response is
    mined for everything it already contains (QuickEnrich's full roster,
    Apollo's single paid search already returning up to `per_page`
    results), up to max_candidates, with no extra API calls -- this never
    makes a second call to the same provider to reach the cap.

    Same-person dedupe is person-level (same_person), not an exact
    (first, last) string key as it was before Phase 17. That key could only
    ever catch an identical spelling, so "Sam Hollis" and "Samantha
    Hollis" both survived it, and Apollo's masked surnames ("Ka***n")
    were excluded from it outright. same_person is nickname-aware and
    initial-aware, so the masked rows now dedupe too -- which is why
    Apollo candidates carry `last_name_obfuscated` through from the search
    result instead of discarding it. Stage B's email-based dedupe in
    find_persona_contacts_generic is still the backstop for anything that
    reaches it with no usable name at all.

    Returns {"candidates": [...up to max_candidates...], "trace": [...],
    "status": str}. Each candidate carries: name, first_name, last_name,
    title, tier, rank, linkedin, has_email (only meaningful for the
    quickenrich rung), source ("quickenrich" | "web_research" | "apollo").
    status is "found" (>=1 candidate), "unresolved - no contact found" (0
    candidates, every rung tried), or a "dry-run - ..." status when
    dry_run stopped things early.
    """
    trace: List[str] = []
    candidates: List[Dict[str, Any]] = []

    def _add(c: Dict[str, Any]) -> None:
        """Append unless an already-accepted candidate is the same human.

        Phase 17: compares with same_person instead of an exact lowercased
        (first, last) key. The old key only caught an identical spelling, so
        a roster holding both "Sam Hollis" and "Samantha Hollis" produced
        two candidates and then paid Stage B twice to resolve one person.
        """
        for existing in candidates:
            if same_person(c, existing):
                trace.append("dropped same-person duplicate: {} == {}".format(
                    c.get("name") or "?", existing.get("name") or "?"))
                return
        candidates.append(c)

    # --- Rung 1: QuickEnrich contact-finder (free) ---
    roster = qe_contact_finder(domain, title_query, ledger)
    qualifying = []
    for r in roster:
        title = (r.get("title") or "").strip()
        cls = classify_fn(title)
        if cls is None:
            continue
        qualifying.append({
            "rank": cls["rank"], "tier": cls["tier"], "title": title,
            "first_name": (r.get("first_name") or "").strip(),
            "last_name": (r.get("last_name") or "").strip(),
            "name": _full_name(r),
            "linkedin": _clean_linkedin(r.get("employee_linkedin")) or "",
            "has_email": bool(r.get("has_email")),
            "source": "quickenrich",
        })
    qualifying.sort(key=lambda c: (c["rank"], not c["has_email"]))
    trace.append("QE discovery: {} record(s), {} qualifying".format(len(roster), len(qualifying)))
    for c in qualifying:
        if len(candidates) >= max_candidates:
            break
        _add(c)
    if len(qualifying) > len(candidates):
        dropped = qualifying[len(candidates):]
        trace.append("QE discovery: {} candidate(s) beyond the cap of {} not taken ({})".format(
            len(dropped), max_candidates, ", ".join(c["name"] or "?" for c in dropped)))
    # Phase 17: this rung produced something, so no later rung runs at all --
    # not even to top up toward max_candidates. See the docstring.
    if candidates:
        trace.append("stopping at QE rung: {} candidate(s) found, skipping web_research and "
                     "Apollo discovery".format(len(candidates)))
        return {"candidates": candidates[:max_candidates], "trace": trace, "status": "found"}

    # Reached only when QuickEnrich yielded nothing qualifying: a dry run stops
    # here rather than spending an LLM call to preview what it would have found.
    if dry_run:
        trace.append("dry-run: stopping before web_research/Apollo discovery (paid/LLM rungs)")
        return {"candidates": [], "trace": trace, "status": "dry-run - no QE candidate"}

    # --- Rung 2: web_research (free -- Claude usage, not vendor credit) ---
    try:
        wr = web_research_find_contact(domain, title_query, ledger=ledger)
    except MissingKey as exc:
        trace.append("web_research skipped: {}".format(exc))
        wr = None
    if wr:
        cls = classify_fn(wr.get("title") or "")
        if cls:
            wr = dict(wr, rank=cls["rank"], tier=cls["tier"], source="web_research", has_email=False)
            trace.append("web_research hit: {} ({})".format(wr.get("name"), wr.get("title")))
            _add(wr)
        else:
            trace.append("web_research returned a candidate but title didn't qualify: {}".format(
                wr.get("title")))
    else:
        trace.append("web_research: no candidate found")

    if candidates:
        trace.append("stopping at web_research rung: {} candidate(s) found, skipping Apollo "
                     "discovery".format(len(candidates)))
        return {"candidates": candidates[:max_candidates], "trace": trace, "status": "found"}

    # --- Rung 3: Apollo people-search (paid, discovery mode -- see the
    # module note on apollo_people_search re: verification status) ---
    if ledger.apollo_budget_exhausted():
        trace.append("Apollo budget cap reached, skipping discovery rung")
        status = "found" if candidates else "unresolved - no contact found"
        return {"candidates": candidates, "trace": trace, "status": status}

    apollo_candidates = apollo_people_search(ledger, domain, title_query)
    qualifying_apollo = []
    for r in apollo_candidates:
        title = (r.get("title") or "").strip()
        cls = classify_fn(title)
        if cls is None:
            continue
        first = (r.get("first_name") or "").strip()
        qualifying_apollo.append({
            "rank": cls["rank"], "tier": cls["tier"], "title": title,
            "first_name": first, "last_name": "", "name": first,
            # Apollo masks the surname ("Ka***n"). Phase 17 keeps it instead of
            # dropping it: the first letter is real, and it is the only thing
            # that lets same_person tell two masked rows apart from one repeated
            # one. last_name stays "" so nothing downstream mistakes a mask for
            # a usable surname.
            "last_name_obfuscated": (r.get("last_name_obfuscated") or "").strip(),
            "linkedin": "", "has_email": False, "source": "apollo",
            "apollo_id": r.get("apollo_id") or "",
        })
    qualifying_apollo.sort(key=lambda c: c["rank"])
    trace.append("Apollo discovery: {} record(s), {} qualifying".format(
        len(apollo_candidates), len(qualifying_apollo)))
    before = len(candidates)
    for c in qualifying_apollo:
        if len(candidates) >= max_candidates:
            break
        _add(c)
        trace.append("Apollo discovery hit: {} ({}, id={})".format(
            c["name"] or "?", c["title"], c["apollo_id"] or "none"))
    added_from_apollo = len(candidates) - before
    if len(qualifying_apollo) > added_from_apollo:
        trace.append("Apollo discovery: {} candidate(s) beyond the cap of {} not taken".format(
            len(qualifying_apollo) - added_from_apollo, max_candidates))

    status = "found" if candidates else "unresolved - no contact found"
    return {"candidates": candidates[:max_candidates], "trace": trace, "status": status}


def enrich_contact_generic(candidate: Dict[str, Any], domain: str, ledger: CreditLedger,
                           dry_run: bool = False) -> Dict[str, Any]:
    """Stage B - contact enrichment. Given a Stage A candidate, resolves a
    verified email: QuickEnrich paid -> Hunter Email Finder -> Apollo
    reveal. Persona-agnostic -- nothing here depends on which title tier
    matched.

    Returns {"email": str, "source": str, "confidence": str,
             "linkedin": str, "trace": [...], "status": str}.
    status is "resolved", "unresolved - contact found, no email", or
    "halted - budget caps reached" (every remaining paid rung was capped,
    not just tried-and-missed -- cache.py's write rule skips caching this
    one, same discipline as the old QuickEnrich-only halt).
    """
    trace: List[str] = []
    out = {"email": "", "source": "", "confidence": "",
          "linkedin": candidate.get("linkedin") or "",
          # Set only when a reveal returns a real name Stage A didn't already
          # have (the Apollo-discovery path, whose search results carry only
          # an obfuscated last name) -- empty otherwise so a caller that
          # already has the right name from Stage A never gets overwritten
          # with blank.
          "name": "", "status": "", "trace": trace}
    first, last = candidate.get("first_name") or "", candidate.get("last_name") or ""
    linkedin = candidate.get("linkedin") or None
    apollo_id = candidate.get("apollo_id") or None

    if dry_run:
        out.update(status="dry-run - contact identified, email not resolved",
                   confidence="not resolved (dry run)")
        return out

    any_rung_tried = False

    # --- Rung 1: QuickEnrich paid email lookup ---
    if not ledger.qe_budget_exhausted():
        any_rung_tried = True
        rec = qe_email_search(ledger, linkedin_url=linkedin, company_url=domain,
                              first_name=first, last_name=last)
        if rec:
            email = _clean_email(rec.get("email"))
            if email:
                out.update(
                    email=email, source="QuickEnrich",
                    confidence="verified {}".format(rec.get("email_verification_date") or "date n/a"),
                    linkedin=_clean_linkedin(rec.get("employee_linkedin")) or linkedin or "",
                    status="resolved")
                trace.append("QE email lookup hit")
                return out
        trace.append("QE email lookup miss")
    else:
        trace.append("QuickEnrich budget cap reached, skipping")

    # --- Rung 2: Hunter Email Finder ---
    if not ledger.hunter_budget_exhausted() and first and last:
        any_rung_tried = True
        rec = hunter_email_finder(ledger, domain, first, last)
        if rec:
            email = _clean_email(rec.get("email"))
            if email:
                out.update(
                    email=email, source="Hunter",
                    confidence="hunter score={} (NOT pre-verified - run Scrubby before "
                               "send)".format(rec.get("score")),
                    linkedin=_clean_linkedin(rec.get("linkedin")) or linkedin or "",
                    status="resolved")
                trace.append("Hunter Email Finder hit")
                return out
        trace.append("Hunter Email Finder miss")
    elif ledger.hunter_budget_exhausted():
        trace.append("Hunter budget cap reached, skipping")
    else:
        trace.append("no first/last name for Hunter Email Finder, skipping")

    # --- Rung 3: Apollo people/match with email reveal ---
    if not ledger.apollo_budget_exhausted():
        any_rung_tried = True
        person = apollo_match(ledger, first_name=first, last_name=last, domain=domain,
                              linkedin_url=linkedin, apollo_id=apollo_id)
        if person:
            email = _clean_email(person.get("email"))
            if email:
                status_txt = person.get("email_status") or "unknown"
                out.update(
                    email=email, source="Apollo",
                    confidence="apollo email_status={} (NOT pre-verified - run Scrubby "
                               "before send)".format(status_txt),
                    linkedin=_clean_linkedin(person.get("linkedin_url")) or linkedin or "",
                    status="resolved")
                # apollo_id reveals are the first point a real (unobfuscated)
                # name is available for an Apollo-discovery candidate --
                # surface it so the output isn't stuck with just a first name.
                real_first = (person.get("first_name") or "").strip()
                real_last = (person.get("last_name") or "").strip()
                if apollo_id and (real_first or real_last):
                    out["name"] = (real_first or first) + " " + (real_last or last)
                    out["name"] = out["name"].strip()
                trace.append("Apollo reveal hit")
                return out
        trace.append("Apollo reveal miss")
    else:
        trace.append("Apollo budget cap reached, skipping")

    if not any_rung_tried:
        out.update(status="halted - budget caps reached (QuickEnrich/Hunter/Apollo)",
                  confidence="not attempted - every paid rung was capped")
        return out

    out.update(status="unresolved - contact found, no email", confidence="no email from any provider")
    return out


# --------------------------------------------------------------------------
# Phase 8: per-contact recency check for anyone NOT already tracked in
# Fibery -- the "not in Fibery, but Apollo shows a send <=60 days" branch
# of the reconciliation logic in main.py's per-company loop.
# --------------------------------------------------------------------------

def apollo_contact_activity(ledger: Optional[CreditLedger], email: str) -> Optional[Dict[str, Any]]:
    """Look up whether `email` already exists as an Apollo Contact (which
    happens automatically once someone is added to a sequence) and, if
    so, its most recent activity timestamp.

    Confirmed live 2026-09-17 against the real account: POST
    /contacts/search returns a `last_activity_date` ISO timestamp per
    contact. That field is Apollo's general "any activity on this
    contact" timestamp (sends, opens, clicks, manual edits all touch it),
    not a "last email sent" field specifically -- it's the closest
    verified signal available without deeper per-message reconciliation
    against every sequence, and a reasonable proxy either way: a contact
    with no Apollo activity in 60 days has certainly not been emailed in
    that window either. Known, accepted limitation (same one already in
    the scope doc): this only sees activity that went through Apollo -- a
    manual email from Rachel's inbox outside Apollo won't be caught.

    q_keywords is a fuzzy match, not an exact filter, so the result is
    confirmed against the exact email before being trusted. Returns None
    if no matching Apollo Contact exists (never added/sequenced) or the
    lookup fails -- both cases mean "no evidence of recent Apollo
    activity," which the caller treats as safe to proceed.
    """
    if not email:
        return None
    body = {"q_keywords": email, "per_page": 5}
    try:
        r = _session.post(APOLLO_BASE + "/contacts/search", headers=_apollo_headers(),
                          json=body, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        _provider_error(ledger, "apollo/contacts-search", email, "request failed: {}".format(exc))
        return None
    if r.status_code >= 400 or data.get("error"):
        _provider_error(ledger, "apollo/contacts-search", email,
                        "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:120]))
        return None
    contacts = data.get("contacts") or []
    email_l = email.strip().lower()
    match = next((c for c in contacts if (c.get("email") or "").strip().lower() == email_l), None)
    if not match:
        return None
    return {"last_activity_date": match.get("last_activity_date"),
           "apollo_contact_id": match.get("id") or ""}


# --------------------------------------------------------------------------
# Phase 8: the shared multi-contact orchestration. Every persona's thin
# wrapper (persona_finance.py / persona_people_ops.py /
# persona_recruiter_ta.py) calls this one with its own title_query and
# classify_fn -- replaces cfo_enrich.py's old single-contact
# _find_finance_contact_live()/find_finance_contact() pair, generalized to
# up to `max_candidates` contacts and cached per-persona (cache.py's
# contacts_<persona_key> tables, one per persona, Phase 8).
# --------------------------------------------------------------------------

def find_persona_contacts_generic(persona_key: str, company: str, website: str,
                                  title_query: List[str],
                                  classify_fn: Callable[[str], Optional[Dict[str, Any]]],
                                  ledger: CreditLedger, dry_run: bool = False,
                                  no_cache: bool = False, max_candidates: int = 3) -> Dict[str, Any]:
    """Cache-aware public entry point every persona's find_contacts()
    wrapper calls. Wraps find_contact_generic (Stage A, up to
    max_candidates ranked candidates) and enrich_contact_generic (Stage
    B, called once per candidate to resolve a verified email).

    Returns the cached/fresh envelope: {"company", "persona", "contacts":
    [...up to max_candidates contact dicts...], "status", "notes"}. Each
    contact dict has the same shape the old single-contact
    find_finance_contact() returned (contact_name, title, title_tier,
    email, source, confidence, linkedin, status, notes) so main.py's
    write-back/CSV logic only has to loop over a list, not reshape
    anything.

    Same cache-write discipline as the old single-contact version: a dry
    run never writes (cache would poison a real answer for up to 90 days
    with an incomplete preview) -- allow_write=not dry_run enforces that.
    An envelope where ANY candidate's Stage B run halted on a budget cap
    gets an overall "halted - ..." status, which cache.py's existing
    contacts write rule (unchanged) skips caching entirely -- the whole
    persona-for-this-company result retries clean next time with fresh
    budget rather than freezing a partially-filled list for 90 days.
    """
    domain = _domain_only(website)

    def _live() -> Dict[str, Any]:
        stage_a = find_contact_generic(domain, title_query, classify_fn, ledger,
                                       dry_run=dry_run, max_candidates=max_candidates)
        trace_a = list(stage_a["trace"])
        stage_a_candidates = stage_a["candidates"]

        if not stage_a_candidates:
            status = stage_a["status"] if stage_a["status"].startswith("dry-run") \
                else "skipped - no {} contact found".format(persona_key)
            return {"company": company, "persona": persona_key, "contacts": [],
                   "status": status, "notes": "; ".join(trace_a)}

        # Phase 17 step 4: person-level dedupe BEFORE Stage B, so two spellings of
        # one human cost one email resolution rather than two. Stage A already
        # dedupes within itself; this also covers a cached/caller-supplied list and
        # is the layer that protects the spend. enrich_contact_generic itself is
        # deliberately untouched -- only which candidates reach it changes.
        deduped: List[Dict[str, Any]] = []
        for candidate in stage_a_candidates:
            twin = next((d for d in deduped if same_person(candidate, d)), None)
            if twin:
                trace_a.append("dropped same-person duplicate before Stage B: {} == {}".format(
                    candidate.get("name") or "?", twin.get("name") or "?"))
                continue
            deduped.append(candidate)
        stage_a_candidates = deduped

        contacts: List[Dict[str, Any]] = []
        seen_emails: set = set()
        any_halted = False
        for candidate in stage_a_candidates:
            out = {"company": company, "contact_name": candidate["name"], "title": candidate["title"],
                  "title_tier": candidate["tier"], "email": "", "source": "", "confidence": "",
                  "linkedin": candidate.get("linkedin") or "", "status": "", "notes": ""}

            if dry_run:
                out.update(status="dry-run - contact identified, email not resolved",
                           source="{} (discovery only)".format(candidate.get("source") or "?"),
                           confidence="not resolved (dry run)", notes="; ".join(trace_a))
                contacts.append(out)
                continue

            stage_b = enrich_contact_generic(candidate, domain, ledger, dry_run=False)
            trace = trace_a + stage_b["trace"]

            if stage_b["status"] == "resolved":
                email = stage_b["email"]
                email_key = email.strip().lower()
                if email_key in seen_emails:
                    # Stage B's real backstop: two Stage-A candidates that
                    # slipped past the name-based dedupe (e.g. two of
                    # Apollo's obfuscated-last-name candidates) resolved
                    # to the same real person. Drop the duplicate rather
                    # than surface the same email twice under one persona.
                    continue
                seen_emails.add(email_key)
                out.update(
                    email=email, source=stage_b["source"], confidence=stage_b["confidence"],
                    linkedin=stage_b.get("linkedin") or out["linkedin"],
                    contact_name=stage_b.get("name") or out["contact_name"],
                    status="resolved",
                    notes="; ".join(trace) + _agency_domain_flag(email, domain))
            elif stage_b["status"].startswith("halted"):
                any_halted = True
                out.update(status=stage_b["status"], notes="; ".join(trace))
            else:
                out.update(status="unresolved - contact found, no email",
                           source="{} (discovery only)".format(candidate.get("source") or "?"),
                           confidence="no email from any provider",
                           notes="; ".join(trace))
            contacts.append(out)

        if any_halted:
            envelope_status = "halted - budget caps reached (QuickEnrich/Hunter/Apollo)"
        elif any(c["status"] == "resolved" for c in contacts):
            envelope_status = "found"
        else:
            envelope_status = "unresolved - contact(s) found, no email"
        return {"company": company, "persona": persona_key, "contacts": contacts,
               "status": envelope_status, "notes": "; ".join(trace_a)}

    table = cache.persona_contacts_table(persona_key)
    return cache.get_or_fetch(
        table, company, website, fetch_fn=_live,
        no_cache=no_cache, allow_write=not dry_run,
    )
