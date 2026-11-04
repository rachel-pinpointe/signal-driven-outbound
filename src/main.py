"""Lead enrichment pipeline. Multi-persona (Phase 8: CFO/Finance, HR/People
Ops, Recruiter/TA all live -- see src/personas.py). By default every live
persona runs for every company; --persona restricts to one for debugging.

One command for the routine case:

  python -m src.main --input companies.csv

That runs qualification, then enriches only the companies that qualify (all
live personas' contact-finders, up to 3 ranked contacts each), under a
built-in QuickEnrich credit cap and a persistent daily company-processing
cap. No --estimate/--dry-run/--max-credits sequencing needed. Those flags
still exist as overrides:

  python -m src.main --input companies.csv --estimate      # cost only, spends nothing
  python -m src.main --input companies.csv --dry-run        # free rungs only
  python -m src.main --input companies.csv --max-credits 40 # override the QE cap
  python -m src.main --input companies.csv --daily-cap 50   # override the daily company cap
  python -m src.main --input companies.csv --enrich-all     # ignore qualification
  python -m src.main --input companies.csv --persona cfo    # one persona only

--input also accepts the literal value "fibery" in place of a CSV path: this
queries GTM Pipeline/Companies live (Research Status != Disqualified AND
Promotion Status != Promoted to CRM -- fibery_client.is_active_company()) and
skips read_company_list() entirely (Phase 9). The same check runs again per
company as a defense-in-depth gate even on a hand-typed CSV, since main.py
accepts CSVs from anywhere, not just Fibery -- see
read_company_list_from_fibery() and run()'s gate_reason logic below, and
claude/nycbound-gtm-phase9-company-intake-scope.md (Sales & Marketing
claude.ai Project).

Phase 8 (see claude/nycbound-gtm-multi-persona-contact-scope.md, same
Project) replaced the old company-level "was ANY contact here sequenced in
the last 60 days" gate with a per-contact reconciliation: every
email-resolved contact is checked individually against Fibery's existing
Contacts (exact match) and, for a brand-new discovery, against Apollo's own
activity history, before it's allowed into the send-ready output. A contact
found "Recently Contacted" is still written to the CSV/Fibery (for
visibility) but routed out of the ready/needs-verification buckets. There is
deliberately no separate --backfill mode: read_company_list_from_fibery()
already returns every active company every run, and since new companies
start with empty per-persona caches, ordinary runs naturally backfill
People Ops/Recruiter-TA contacts for old CFO-only companies over time,
gated by the same daily cap as everything else.

Every company this pipeline touches also gets written back to Fibery when
FIBERY_API_TOKEN is set and the company's domain matches an existing GTM
Pipeline/Companies row: Research Status (mapped from `qualification`) and
Last Checked Date always; a Contacts row per resolved contact per persona,
each carrying the new Outreach Status field (Cleared to proceed / Recently
Contacted). A missing token or no domain match degrades to a printed note,
never a crash -- this pipeline must keep working against a plain hand-typed
CSV with zero Fibery involvement, exactly as it always has.

Step 3B (Apollo contact creation and sequence enrolment) is deliberately NOT
implemented here. Enrolling a contact in a live sequence is a real send action;
it requires an explicit go-ahead after the spreadsheet is reviewed, and it must
never fire as a side effect of Steps 1-2 finishing.
"""

import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import cache
from .company_research import (research_company, research_likely_priorities,
                               research_stated_goal, compute_ramp_calc)
from .providers import (
    FETCH_COUNTS, CreditLedger, MissingKey, apollo_contact_activity,
    email_domain_matches_company, fetch_total,
    hunter_account, qe_balance, require_keys, same_person, scrubby_classify,
    scrubby_domain_is_catch_all, scrubby_validate_email,
    # Aliased: run() already has a `spend_log_path` PARAMETER, and two different meanings for one
    # name in this module would be a trap for whoever edits it next.
    spend_log_path as providers_spend_log_path,
)
from .personas import PERSONAS, Persona, get_persona
from . import fibery_client as fc
from .redact import Redactor, RedactingStream

REQUIRED_KEYS = ["STRING_API_KEY", "QUICKENRICH_API_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY"]

# Built-in ceiling so a routine run cannot drain the pool. Sized per
# company-persona pair, since each persona spends its own waterfall of
# lookups per company -- main() multiplies by however many personas are
# actually running this pass.
CREDITS_PER_COMPANY_ALLOWANCE = 2
DEFAULT_CREDIT_CAP_FLOOR = 25
DEFAULT_CREDIT_CAP_CEILING = 250

# Phase 8: default ceiling on how many companies enter persona contact-
# discovery per calendar day, persisted in cache.py's daily_runs table.
# main.py has no fixed schedule of its own (confirmed 2026-09-17 -- only
# the HyperSignal signal-watcher and promote_to_crm.py run on a Cowork
# schedule), so an in-memory counter would reset every invocation and not
# actually bound daily volume; the cap has to live in storage.
DEFAULT_DAILY_CAP = 25


def default_credit_cap(n_company_persona_pairs: int) -> int:
    return max(DEFAULT_CREDIT_CAP_FLOOR,
               min(DEFAULT_CREDIT_CAP_CEILING, n_company_persona_pairs * CREDITS_PER_COMPANY_ALLOWANCE))


# Identical header on both output files so either can be imported directly.
OUTPUT_COLUMNS = [
    "Company", "Persona", "Contact Name", "First Name", "Last Name", "Title", "Email",
    "Email Source/Confidence",
    # Phase 8: the per-contact 60-day reconciliation verdict (see module
    # docstring). "Cleared to proceed" / "Recently Contacted" / blank
    # (no resolved contact on this row).
    "Outreach Status",
    "LinkedIn", "Qualification", "Open NYC Role(s)",
    "NYC Role Locations", "Role Band Used", "Signal Notes",
    # Populated only when --scrub is passed, and only for Apollo/Hunter
    # emails -- QuickEnrich rows are already verified and never sent through
    # Scrubby (CLAUDE.md). Blank on every other row.
    "Scrubby Verdict", "Scrubby Raw Status", "Scrubby Catch-All Domain",
    # Phase 5: search-and-cite, not inference -- see company_research.py's
    # research_stated_goal(). Only populated for enriched (qualifying)
    # companies, same gate as the contact columns; blank on every other row
    # and on a dry run (an LLM/exa call, skipped like every other paid rung).
    "Stated Goal", "Stated Goal Source", "Stated Goal Excerpt", "Stated Goal URL",
]

NAME_KEYS = ["company", "company name", "name", "account"]
URL_KEYS = ["url", "domain", "website", "company url", "site"]

# Phase 17 step 5: every contact the write guards decline to create, with its reason.
#
# The guards exist because bad rows reached Fibery, but a guard that silently drops a finding
# would be its own bug -- a wrong-domain email is sometimes a real person whose company record
# has the wrong Domain set, and nobody can tell which from an absence. So a skipped contact is
# always still written to the run's CSV, and always appended here with the reason, where it can
# be looked at by hand. Append-only across runs on purpose: the useful question is "what has this
# been quietly declining to write", which a per-run file makes harder to see.
#
# Lives under outputs/ (gitignored, same place the run's CSVs go) and is resolved relative to the
# cwd main.py runs from, which run-cron.sh guarantees is the repo root.
MANUAL_REVIEW_LOG = Path("outputs") / "manual_review_skips.log"


def _log_manual_review(company: str, persona: str, contact: Dict[str, Any],
                       reason: str, log_path: Optional[Path] = None) -> None:
    """Append one skipped contact to the manual-review log. Best-effort: a logging failure must
    never take down a run that is otherwise fine, so an OSError here is printed and swallowed
    exactly like the Fibery write-back failures around it."""
    path = log_path or MANUAL_REVIEW_LOG
    line = "{}\t{}\t{}\t{}\t{}\t{}\n".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), company or "?", persona or "?",
        contact.get("contact_name") or "(unnamed)", contact.get("email") or "(no email)", reason)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print("    !! could not write manual-review log ({}): {}".format(path, e))


# Phase 18: main.py's own internal deadline, in seconds, env-overridable as MAIN_PY_DEADLINE_SEC.
#
# Sized BELOW run-cron.sh's outer time-box (MAIN_BUDGET_SECONDS, default 1800) on purpose. That
# outer box kills the whole process group -- SIGTERM, then SIGKILL five seconds later -- which
# ends the run mid-company with no CSV written, no Fibery write-back for the company in flight,
# and no spend summary. 300 seconds of headroom is enough to stop starting new companies, finish
# the one in hand, write the three output CSVs and print the ledger, so a long run degrades into
# a complete partial result instead of an abrupt nothing.
DEFAULT_MAIN_DEADLINE_SEC = 1500


def _spend_log_path() -> Path:
    """outputs/spend_YYYYMMDD.log -- one file per calendar day, appended across runs, next to the
    run's CSVs.

    Delegates to providers.spend_log_path (Phase 19) rather than computing the name again here, so
    the per-company ledger lines written from this module and the per-call Anthropic lines written
    from providers.py cannot drift onto two different files.
    """
    return providers_spend_log_path()


def _append_spend_line(ledger: CreditLedger, label: str,
                       log_path: Optional[Path] = None) -> None:
    """Append one running-spend snapshot and flush it immediately.

    Phase 18: the flush is the point. Python buffers stdout and file writes in blocks when not
    attached to a tty, so a run killed by the outer time-box lost whatever sat in the buffer --
    which is exactly the final stretch you need in order to see what the run had spent by the time
    it died. Flushing per company means the log on disk is always current to the last completed
    company, however the process ends.

    Best-effort, like _log_manual_review: a logging failure must never take down a run.
    """
    path = log_path or _spend_log_path()
    line = "{}\t{}\t{}\n".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), label, ledger.summary())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as e:
        print("    !! could not write spend log ({}): {}".format(path, e))


def _deadline_exceeded(started_monotonic: float, deadline_sec: Optional[int],
                       now: Optional[float] = None) -> bool:
    """Whether this run has used up its internal budget. A falsy deadline disables the check
    entirely, so a caller that wants no deadline (a test, an interactive run) passes 0 or None."""
    if not deadline_sec:
        return False
    current = time.monotonic() if now is None else now
    return (current - started_monotonic) >= deadline_sec


def _install_sigterm_handler(ledger: CreditLedger, log_path: Optional[Path] = None):
    """Register a SIGTERM handler that records where the run got to before dying, and return it
    (returned so it can be invoked directly in a test rather than by raising a real signal).

    Phase 18: run-cron.sh's time-box kills the whole process group with SIGTERM and then SIGKILL
    five seconds later. Without a handler, Python's default SIGTERM disposition terminates the
    process immediately: no ledger summary, no spend line, nothing on disk explaining what the run
    had already spent. That is precisely the run you most want a record of.

    Exits 143 (128 + SIGTERM), the conventional code for a SIGTERM death. run_timeboxed reports
    124 for a timeout regardless of the child's code, so this is for anyone reading the log rather
    than for the shell's control flow.
    """
    def _handle(signum, frame):  # noqa: ARG001 -- signal handler signature
        print("\n!! SIGTERM received -- the outer time-box is shutting this run down.", flush=True)
        print("Spend at SIGTERM     : {}".format(ledger.summary()), flush=True)
        for line in ledger.anthropic_breakdown_lines():
            print(line, flush=True)
        for w in dict.fromkeys(ledger.warnings):
            print("WARNING: {}".format(w), flush=True)
        _append_spend_line(ledger, "SIGTERM (run terminated by the outer time-box)", log_path)
        sys.exit(143)

    signal.signal(signal.SIGTERM, _handle)
    return _handle


def read_company_list(path: str) -> List[Dict[str, str]]:
    """Accepts any CSV with a name column and a URL column, header names
    case-insensitive (Company/URL, company/domain, Name/Website ... )."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("Company list not found: " + path)

    rows = []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Company list has no header row: " + path)
        lookup = {(h or "").strip().lower(): h for h in reader.fieldnames}
        name_col = next((lookup[k] for k in NAME_KEYS if k in lookup), None)
        url_col = next((lookup[k] for k in URL_KEYS if k in lookup), None)
        if not name_col or not url_col:
            raise ValueError(
                "Company list needs a name column ({}) and a URL column ({}). Found: {}"
                .format("/".join(NAME_KEYS), "/".join(URL_KEYS), reader.fieldnames))
        for r in reader:
            name = (r.get(name_col) or "").strip()
            url = (r.get(url_col) or "").strip()
            if not name or not url:
                continue
            # Phase 21: same read-side guard as fibery_client.load_company_map(), for CSV input.
            combined = fc.combined_company_reason(name, url)
            if combined:
                print("[skip:combined-company] {} (CSV row)".format(combined), file=sys.stderr)
                continue
            if not url.startswith("http"):
                url = "https://" + url.lstrip("/")
            rows.append({"company": name, "website": url})
    return rows


# qualification (from company_research.py's exact string constants, confirmed via grep 2026-09-16:
# "QUALIFIED", "QUALIFIED (comp unproven)", "DISQUALIFIED", "NO BOARD DETECTED - MANUAL CHECK
# NEEDED") -> Fibery GTM Pipeline/Research Status enum name.
def _map_research_status(qualification: str) -> str:
    if qualification.startswith("QUALIFIED"):
        return "Qualified"
    if qualification.startswith("DISQUALIFIED"):
        return "Disqualified"
    if qualification.startswith("NO BOARD"):
        return "Needs Manual Review"
    # Added 2026-09-22 (Phase 14) -- company_research.py's _research_company_live()
    # returns this instead of falling through to the jina/String live-discovery fallback
    # when HyperSignal has no fresh data for the domain yet. Honest "haven't looked"
    # state, not a verdict -- must not collapse into the "unexpected string" default
    # below, which reads as Needs Manual Review (a real verdict implying someone should
    # go check by hand).
    if qualification.startswith("PENDING"):
        return "Pending Not Yet Scanned"
    return "Needs Manual Review"  # safe default for anything unexpected (e.g. "UNKNOWN")


# jobs["signal_tier"] (company_research.py, ~line 1169) is a full descriptive string, e.g.
# "Tier 1 - explicit relocation/visa language". Fibery's Signal Tier enum (renamed
# Noise -> Tier 3, 2026-09-17) only wants the "Tier 1"/"Tier 2"/"Tier 3" prefix.
def _map_signal_tier(signal_tier: str) -> Optional[str]:
    for key in ("Tier 1", "Tier 2", "Tier 3"):
        if signal_tier.startswith(key):
            return key
    return None  # unexpected string (e.g. "no signal") -- don't write a wrong tier


# contact["source"] (providers.py's enrich_contact_generic, Stage B) is already exactly
# "QuickEnrich" / "Hunter" / "Apollo" -- a direct match to Fibery's Enrichment Source enum.
# "Manual" is the fallback for anything else (blank, or a future source not added here yet).
def _map_enrichment_source(source: Optional[str]) -> str:
    return source if source in fc.ENRICHMENT_SOURCE else "Manual"


# All three personas are live (Phase 8). Fibery's Persona enum names (see
# fibery_client.PERSONA), confirmed 2026-09-17.
PERSONA_TO_FIBERY = {
    "cfo": "CFO/Finance",
    "hr_people_ops": "People Ops",
    "recruiter_ta": "Recruiter/TA",
}


# Signal Tier prioritization (2026-09-17): Tier 1 first, then never-classified (blank --
# freshly surfaced by HyperSignal, could be anything, worth a prompt look), then Tier 2,
# then Tier 3. A company whose Signal Tier holds something unexpected (shouldn't happen --
# _map_signal_tier only ever writes these three -- but a hand-edited Fibery value is real
# life) falls in with never-classified rather than crashing the sort.
_TIER_RANK = {"Tier 1": 0, None: 1, "Tier 2": 2, "Tier 3": 3}


def _company_priority_key(c: dict) -> Tuple[int, str]:
    """Lower sorts first. Primary: Signal Tier rank (see _TIER_RANK). Secondary, within a
    tier: staleness -- never-checked (blank Last Checked Date) before oldest-checked, so a
    big backlog in one tier can't let a handful of companies hog every day's cap while the
    rest of that same tier never gets a turn. Last Checked Date is the "YYYY-MM-DD" string
    main.py writes it as, so plain string comparison sorts oldest-first correctly; "" (never
    checked) sorts before any real date."""
    tier_rank = _TIER_RANK.get(c.get("signal_tier"), _TIER_RANK[None])
    last_checked = c.get("last_checked_date") or ""
    return (tier_rank, last_checked)


def read_company_list_from_fibery(company_map: Dict[str, dict]) -> List[Dict[str, str]]:
    """The paid gate's upstream half (Phase 9): only companies that are still fair game get into
    the run at all. `company_map` is fibery_client.load_company_map()'s return value, loaded once
    in main() and reused here rather than a second live query. Falls back to https://<domain> when
    Website is blank but Domain isn't.

    Sorted by Signal Tier + staleness (2026-09-17) so the daily cap in run() -- which just
    consumes this list top-down -- spends each day's budget on the strongest, most-overdue
    signals first instead of arbitrary dict order. See
    claude/nycbound-gtm-signal-tier-prioritization-scope.md (Sales & Marketing claude.ai
    Project)."""
    active = [(domain, c) for domain, c in company_map.items()
              if fc.is_active_company(c) and c.get("name")]
    active.sort(key=lambda pair: _company_priority_key(pair[1]))
    out = []
    for domain, c in active:
        website = (c.get("website") or "").strip() or ("https://" + domain)
        out.append({"company": c["name"], "website": website})
    return out


def print_estimate(companies: List[Dict[str, str]], qe_bal, hunter_bal, n_personas: int) -> None:
    n = len(companies)
    pairs = n * n_personas
    print("\n=== COST ESTIMATE: {} companies x {} persona(s) = {} company-persona pairs ===\n"
          .format(n, n_personas, pairs))
    print("QuickEnrich")
    print("  discovery (contact-finder)   {:>4} calls x 0 credits = 0  [free]".format(pairs))
    print("  email lookup, best contact   {:>4} paid lookups max     = {} credits".format(pairs, pairs))
    print("  realistic (~70% in dataset)  {:>4} lookups              ~ {} credits".format(
        int(pairs * 0.7), int(pairs * 0.7)))
    print("  built-in cap for this run    {:>4} credits".format(default_credit_cap(pairs)))
    print("  balance available            {}".format(qe_bal if qe_bal is not None else "unknown"))
    print("\nApollo")
    print("  people/match, only on QE miss  ~{}-{} matches (1 credit each)".format(
        int(pairs * 0.1), int(pairs * 0.3)))
    print("  discovery, only on QE miss     ~0-1 searches via /mixed_people/api_search")
    print("        (fixed Phase 7, 2026-09-16 -- Apollo can discover a new candidate,")
    print("        not just enrich a known name; verified-email reveals count against")
    print("        the 230/mo cap, the search call itself does not).")
    print("\nHunter")
    print("  domain-search, last resort     ~{}-{} searches".format(int(pairs * 0.05), int(pairs * 0.4)))
    print("  note: only billed when the response is non-empty.")
    print("  balance available              {}".format(
        hunter_bal if hunter_bal is not None else "unknown"))
    print("\nString (per-fetch, real money)")
    print("  homepage + careers discovery   ~{} fetches (5.9/company, measured -- Layer 1 runs".format(
        int(n * 5.9)))
    print("  once per company, not per persona)")
    print("  ATS job boards + slug probes   free (provider public APIs, not String)")
    print("\nDaily company-processing cap: {} companies/day (persistent -- see cache.py)".format(
        DEFAULT_DAILY_CAP))
    print("No LLM tokens: Steps 1-2 are deterministic. No Anthropic API cost.\n")


def _split_name(full: str):
    parts = (full or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _blank_contact(company: str, reason: str) -> Dict[str, str]:
    return {"company": company, "contact_name": "", "title": "", "title_tier": "",
            "email": "", "source": "", "confidence": "", "linkedin": "",
            "status": reason, "notes": reason}


def _blank_stated_goal(reason: str) -> Dict[str, str]:
    return {"found": False, "goal": "", "source_type": "", "excerpt": "", "url": "",
            "status": reason}


def _days_since(date_str: str, today_str: str) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        t = datetime.strptime(today_str[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (t - d).days


def _contact_write_skip_reason(contact: Dict[str, Any],
                               people_seen: List[Dict[str, Any]],
                               company_domain: str = "",
                               existing_emails: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """Reason this contact must NOT be created as a new Fibery Contact, or None.

    Three guards, checked in this order -- most specific and most actionable first:

    1. Duplicate person (step 4). The same human is routinely found by more than one
       persona at the same company: a Head of Finance who is also the de-facto People
       Ops owner at a 300-person company matches both ladders. Each persona resolved
       them independently and each created its own row, so one person became two or
       three contacts and could be enrolled more than once. First persona to reach
       them wins. Compared with providers.same_person, so it still fires when the two
       personas' providers spelled the name differently or resolved DIFFERENT emails
       -- an email-equality check cannot see that case at all, which is how the real
       duplicates got in.

    2. Domain mismatch (step 5). An email on a domain that isn't the company's is an
       agency contractor or a stale row, not someone to email about this company's
       hiring: contractco.io on an Initech record, priorco.io on a globex.co one.
       providers.email_domain_matches_company allows subdomains, corporate-suffix and
       TLD differences, and a small curated alias dict, and treats a blank domain on
       either side as missing data rather than a mismatch -- it never rejects on
       absent information.

    3. Email already on another Fibery contact (step 5). One address appearing on two
       contact records, often at two different companies. Note this only reaches the
       create path: an email matching a contact at THIS company is handled upstream by
       _reconcile_contact, which updates that row instead of creating a second one.

    Pure function of its arguments: no writes, no network, no logging. The caller
    decides what to do with a reason, which is what makes the whole thing testable.
    """
    for seen in people_seen:
        if same_person(contact, seen):
            return ("same person already written this run by persona '{}' as '{}' <{}>"
                    .format(seen.get("persona") or "?", seen.get("name") or "(unnamed)",
                            seen.get("email") or "no email"))

    email = (contact.get("email") or "").strip()
    if not email_domain_matches_company(email, company_domain):
        return ("email domain '{}' does not match company Domain '{}'".format(
            email.split("@")[-1], company_domain))

    existing = (existing_emails or {}).get(email.lower())
    if existing:
        return ("email already on Fibery contact '{}' (id {}, company id {})".format(
            existing.get("name") or "(unnamed)", existing.get("id") or "?",
            existing.get("company_id") or "none"))

    return None


def _reconcile_contact(fcompany_id: Optional[str], contact: Dict[str, Any],
                       contacts_by_company: Dict[str, list],
                       ledger: CreditLedger, today_str: str) -> Dict[str, Any]:
    """The per-contact 60-day reconciliation (Phase 8): decides whether a
    newly email-resolved contact is safe to message, checking Fibery's own
    Contacts first (an exact record of what this pipeline has already sent
    to Fibery/sequenced) and Apollo's contact activity second (a backstop
    for a contact Apollo already knows about -- e.g. sequenced by Rachel
    outside this pipeline's own Fibery write-back -- that hasn't reached
    Fibery yet). Only called for a contact with status == "resolved" and an
    email; never during a dry run (nothing is ever "resolved" there).

    Returns {"outreach_status": "Cleared to proceed" | "Recently Contacted",
    "existing_fibery_id": str | None, "last_sequenced_hint": "YYYY-MM-DD" |
    None}. last_sequenced_hint is only set when this is a brand-new contact
    (no existing Fibery row) that Apollo showed recent activity for -- so
    the new Fibery row main.py creates carries a real Last Sequenced Date
    from the start, rather than one contact-discovery cycle running with no
    date on record at all.
    """
    email_l = (contact.get("email") or "").strip().lower()
    existing = None
    if fcompany_id:
        for c in contacts_by_company.get(fcompany_id, []):
            if (c.get("email") or "").strip().lower() == email_l:
                existing = c
                break

    if existing:
        days = _days_since(existing.get("last_sequenced"), today_str)
        if days is not None and days <= 60:
            return {"outreach_status": "Recently Contacted", "existing_fibery_id": existing["id"],
                   "last_sequenced_hint": None}
        return {"outreach_status": "Cleared to proceed", "existing_fibery_id": existing["id"],
               "last_sequenced_hint": None}

    activity = apollo_contact_activity(ledger, contact.get("email") or "")
    if activity and activity.get("last_activity_date"):
        days = _days_since(activity["last_activity_date"], today_str)
        if days is not None and days <= 60:
            return {"outreach_status": "Recently Contacted", "existing_fibery_id": None,
                   "last_sequenced_hint": activity["last_activity_date"][:10]}
    return {"outreach_status": "Cleared to proceed", "existing_fibery_id": None,
           "last_sequenced_hint": None}


def _signal_bits(jobs: Dict[str, Any], contact: Dict[str, Any]) -> List[str]:
    bits = [jobs["signal_notes"]]
    if contact["status"] and contact["status"] != "resolved":
        bits.append("CONTACT: " + contact["status"])
    if contact["notes"] and contact["notes"] != contact["status"]:
        bits.append(contact["notes"])
    if jobs["careers_url"]:
        bits.append("careers: " + jobs["careers_url"])
    if jobs["ats"]:
        bits.append("ats: " + jobs["ats"])
    return bits


def _band_str(jobs: Dict[str, Any]) -> str:
    band = jobs["role_band_used"]
    if band and jobs["band_base"]:
        band = "{} (${:,})".format(band, int(jobs["band_base"]))
        if jobs["salary_source"]:
            band += " - " + jobs["salary_source"]
    return band


def _build_row(co: Dict[str, str], persona_label: str, contact: Dict[str, Any],
              outreach_status: str, qualification: str, jobs: Dict[str, Any],
              stated_goal: Dict[str, Any], likely_priorities: str = "") -> Dict[str, str]:
    first, last = _split_name(contact["contact_name"])
    return {
        "Company": co["company"],
        "Persona": persona_label,
        "Contact Name": contact["contact_name"],
        "First Name": first,
        "Last Name": last,
        "Title": contact["title"] + (" [{}]".format(contact["title_tier"])
                                     if contact["title_tier"] else ""),
        "Email": contact["email"],
        "Email Source/Confidence": (contact["source"] + " - " + contact["confidence"]).strip(" -"),
        "Outreach Status": outreach_status,
        "LinkedIn": contact["linkedin"],
        "Qualification": qualification,
        "Open NYC Role(s)": " | ".join(jobs["nyc_roles"]),
        "NYC Role Locations": " | ".join(jobs.get("nyc_role_locations") or []),
        "Role Band Used": _band_str(jobs),
        "Signal Notes": " || ".join(b for b in _signal_bits(jobs, contact) if b),
        "Stated Goal": stated_goal.get("goal") or "",
        "Stated Goal Source": stated_goal.get("source_type") or "",
        "Stated Goal Excerpt": stated_goal.get("excerpt") or "",
        "Stated Goal URL": stated_goal.get("url") or "",
        # Guardrailed LLM inference (2026-09-17), deliberately separate from the
        # verified Stated Goal fields above -- see
        # claude/nycbound-gtm-likely-priorities-scope.md (Sales & Marketing
        # claude.ai Project).
        "Likely Priorities": likely_priorities or "",
    }


def run(companies: List[Dict[str, str]], ledger: CreditLedger, dry_run: bool,
        enrich_all: bool, personas: List[Persona],
        no_cache: bool = False,
        fibery_token: Optional[str] = None,
        company_map: Optional[Dict[str, dict]] = None,
        contacts_by_company: Optional[Dict[str, list]] = None,
        existing_contact_emails: Optional[Dict[str, dict]] = None,
        daily_cap: int = DEFAULT_DAILY_CAP,
        deadline_sec: Optional[int] = None,
        spend_log_path: Optional[Path] = None) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    rows = []
    total = len(companies)
    started_monotonic = time.monotonic()
    cache_stats = {"company_hit": 0, "company_miss": 0, "contact_hit": 0, "contact_miss": 0}
    company_map = company_map or {}
    contacts_by_company = contacts_by_company or {}
    # Phase 17 step 5: email (lowercased) -> {id, name, company_id} for EVERY existing Fibery
    # contact, so a cross-record collision is visible. Mutated as this run creates contacts, so
    # an address this run just wrote is caught too -- a startup snapshot alone cannot see two
    # personas resolving the same email at the same company within one run.
    existing_contact_emails = dict(existing_contact_emails or {})
    for i, co in enumerate(companies, 1):
        # Phase 18: checked BEFORE any work for this company, so the deadline stops the run
        # starting new companies rather than interrupting one mid-flight. Everything downstream
        # (the output CSVs, the ledger summary) still runs -- this is a clean early finish, not
        # an abort, which is the whole difference between it and the outer time-box's SIGKILL.
        if _deadline_exceeded(started_monotonic, deadline_sec):
            print("\n!! internal deadline ({}s) reached at company {} of {}. Not starting any "
                  "more companies; finishing cleanly so the CSVs and ledger still get written."
                  .format(deadline_sec, i, total), flush=True)
            print("   Companies not processed: {}".format(
                ", ".join(c["company"] for c in companies[i - 1:]) or "none"), flush=True)
            _append_spend_line(
                ledger, "DEADLINE {}s reached at company {}/{}".format(deadline_sec, i, total),
                spend_log_path)
            break
        print("[{}/{}] {}".format(i, total, co["company"]), flush=True)

        domain = fc.normalize_domain(co["website"])
        fcompany = company_map.get(domain)
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Qualification first, so credits are only ever spent on companies that
        # clear the bar. Reversing this was how earlier runs paid to enrich
        # companies with no NYC roles at all. Layer 1 always runs, once per
        # company regardless of how many personas are active -- company
        # qualification doesn't depend on who's being contacted.
        jobs = research_company(co["company"], co["website"], no_cache=no_cache)
        cache_stats["company_hit" if jobs.get("_cache") == "hit" else "company_miss"] += 1
        qualification = jobs.get("qualification") or "UNKNOWN"

        should_enrich = enrich_all or qualification.startswith("QUALIFIED")

        # Defense-in-depth paid gate (Phase 9). The upstream filter
        # (read_company_list_from_fibery, or a pre-filtered CSV) is the primary defense; this
        # check makes "run against everybody" safe regardless of where the CSV came from, since
        # main.py accepts hand-typed/edited CSVs too, not just Fibery-sourced ones. Phase 8: the
        # old company-level "recently sequenced" gate that used to live here is gone -- replaced
        # by the per-contact reconciliation below, which is precise about WHICH contact is
        # blocked rather than blocking every persona at a company because one contact there was
        # recently messaged.
        gate_reason = None
        if should_enrich and fcompany and not fc.is_active_company(fcompany):
            gate_reason = "gated - Fibery status is {}/{}".format(
                fcompany.get("research_status"), fcompany.get("promotion_status"))

        # Phase 8: the persistent daily company-processing cap. Only meters real
        # work -- skipped for dry runs (a free/consequence-free preview) and for
        # a company every active persona already has a fresh cache entry for
        # (no live discovery about to happen).
        cap_reason = None
        if should_enrich and not gate_reason and not dry_run:
            already_cached = all(
                cache.is_fresh_cached(cache.persona_contacts_table(p.key), co["website"])
                for p in personas)
            if not already_cached:
                if cache.get_daily_count(today_str) >= daily_cap:
                    cap_reason = "gated - daily company-processing cap ({}) reached".format(daily_cap)
                else:
                    cache.increment_daily_count(today_str)

        effective_reason = gate_reason or cap_reason

        if should_enrich and not effective_reason:
            if dry_run:
                # research_stated_goal has no free discovery rung of its own
                # (it's LLM/exa from the first call) -- skipped on dry run
                # the same way find_contact_generic stops before its
                # web_research/Apollo rungs, rather than spending a real
                # Anthropic call just to preview cost.
                stated_goal = _blank_stated_goal(
                    "dry-run - stated-goal research skipped (LLM/exa call)")
            else:
                stated_goal = research_stated_goal(co["company"], co["website"],
                                                    ledger=ledger, no_cache=no_cache)
        else:
            stated_goal = _blank_stated_goal(effective_reason or "not enriched - {}".format(qualification))

        company_rows: List[Dict[str, str]] = []
        persona_print_lines: List[str] = []
        fibery_contact_notes: List[str] = []
        # Phase 17 step 4: every person written to Fibery for THIS company this run,
        # accumulated across personas so the first persona to reach someone wins.
        # Per-company by construction (re-initialised each iteration) -- two people
        # with the same name at different companies are different people.
        people_seen_at_company: List[Dict[str, Any]] = []

        for persona in personas:
            if should_enrich and not effective_reason:
                envelope = persona.find_contacts(co["company"], co["website"], ledger,
                                                 dry_run=dry_run, no_cache=no_cache)
            else:
                reason = effective_reason or "not enriched - {}".format(qualification)
                envelope = {"company": co["company"], "persona": persona.key, "contacts": [],
                           "status": reason, "notes": reason, "_cache": "n/a"}
            cache_stats["contact_hit" if envelope.get("_cache") == "hit" else "contact_miss"] += 1

            contacts = envelope.get("contacts") or []
            _redact_learn(names=[c.get("contact_name") for c in contacts])
            if not contacts:
                persona_print_lines.append("    {:<14}: {}".format(persona.key, envelope.get("status")))
                continue

            for contact in contacts:
                outreach_status = ""
                likely_priorities = ""
                if contact.get("status") == "resolved" and contact.get("email"):
                    rec = _reconcile_contact(fcompany["id"] if fcompany else None, contact,
                                            contacts_by_company, ledger, today_str)
                    contact["_reconcile"] = rec
                    outreach_status = rec["outreach_status"]
                    # Likely Priorities (2026-09-17): guardrailed inference, no new
                    # search/spend -- skipped on dry-run the same way stated-goal
                    # research is, rather than spending a real Anthropic call just to
                    # preview cost.
                    if not dry_run:
                        likely_priorities = research_likely_priorities(
                            persona.key, contact, co["company"], jobs, stated_goal,
                            ledger=ledger) or ""
                        # Ramp Calculator figures (2026-09-19 fix -- see
                        # compute_ramp_calc()'s own docstring/comment in company_research.py):
                        # only the CFO play's Email 2 cites Year-1 net-value/gap numbers, and
                        # only those need a real calculated answer instead of the play's own
                        # worked-example figures. Appended to Likely Priorities (not a new
                        # Fibery field) since that's the one per-contact text field that
                        # already flows, unmodified, from here through to
                        # apollo_enroll.py's generate_ghost_emails() -- see the matching
                        # hard-rule addition in apollo_enroll.py's _GHOST_EMAIL_PROMPT.
                        if likely_priorities and persona.key == "cfo":
                            ramp = compute_ramp_calc(jobs.get("band_salary_floor"),
                                                     jobs.get("band_role_name"))
                            # basis_phrase is the exact wording for where the salary came from --
                            # plain prose, no parentheses (2026-09-19: Rachel's call, every word
                            # counts) -- and a fallback to the calculator's own default must NEVER
                            # be called a JD floor (caught live: every draft said "the JD floor of
                            # $165K" even with no real salary found at all).
                            basis_phrase = (
                                "the market rate of {}".format(ramp["salary_used_fmt"])
                                if ramp["used_default"] else
                                "the JD floor of {}".format(ramp["salary_used_fmt"])
                            )
                            # The real, specific job title (e.g. "Director, Data Analytics"),
                            # not the coarse band category compute_ramp_calc uses to pick a
                            # factor -- 2026-09-19: caught live, Email 2 was naming the hire "a
                            # local hire" or inventing a title ("Sr. SWE", "sr. director") instead
                            # of the real one already sitting in jobs["driving_role"].
                            role_title = jobs.get("driving_role") or ramp["role_name"]
                            likely_priorities += (
                                "\n\n[RAMP CALC - verified, do not alter or recompute, and "
                                "never mention this block, this instruction, or that a "
                                "calculation was performed -- just state the numbers as fact: "
                                "this is for the {} role -- name this exact role (or a natural "
                                "shortened version of it) when you open the email, never a vague "
                                "placeholder like \"a local hire\" or an invented title that "
                                "isn't this one. Based on {}, Year-1 net value if this hire works "
                                "out locally is {}; if they DIY-relocate and ramp normally, "
                                "{} (gap: {}); if that DIY relocation fails outright (~{}% "
                                "of them, per Jobera), {}. These are already in the shorthand "
                                "form Rachel's copy uses -- reuse them exactly as given, never "
                                "expand to the full number or add more digits/precision. Use the "
                                "exact basis phrase above verbatim, written as plain prose with "
                                "no parentheses -- never call {} a \"JD floor.\"]"
                            ).format(
                                role_title, basis_phrase, ramp["normal_ramp_year1_fmt"],
                                ramp["relo_ramp_year1_fmt"], ramp["gap_fmt"],
                                ramp["failure_rate_pct"], ramp["relo_failure_year1_fmt"],
                                "market rate" if ramp["used_default"] else "the JD floor")
                    contact["_likely_priorities"] = likely_priorities

                company_rows.append(_build_row(co, persona.label, contact, outreach_status,
                                               qualification, jobs, stated_goal,
                                               likely_priorities))
                persona_print_lines.append("    {:<14}: {} | {} | {}{}".format(
                    persona.key, contact["contact_name"] or "-", contact["status"],
                    contact["email"] or "no email",
                    " [{}]".format(outreach_status) if outreach_status else ""))

                # Fibery write-back (Phase 9/8). Best-effort: a failure here is
                # printed and the run continues -- Fibery being down or a field
                # mapping being wrong must never lose the local CSV output,
                # which stays the pipeline's real deliverable.
                if fibery_token and fcompany and not dry_run and contact.get("status") == "resolved" \
                        and contact.get("email"):
                    rec = contact["_reconcile"]
                    # Phase 17 step 4: only a CREATE can duplicate a person. Updating a
                    # row this run's earlier persona already matched is the correct
                    # outcome, not a duplicate, so the guard is scoped to the create path.
                    # The CSV row above is written either way -- a skip here never loses
                    # the finding, it only declines to open a second Fibery record for
                    # someone who already has one.
                    skip_reason = (None if rec["existing_fibery_id"]
                                   else _contact_write_skip_reason(
                                       contact, people_seen_at_company,
                                       company_domain=domain,
                                       existing_emails=existing_contact_emails))
                    if skip_reason:
                        fibery_contact_notes.append(
                            "{} -> contact NOT created ({})".format(persona.key, skip_reason))
                        _log_manual_review(co["company"], persona.key, contact, skip_reason)
                        continue
                    person_record = {"name": contact.get("contact_name") or "",
                                     "linkedin": contact.get("linkedin") or "",
                                     "email": contact.get("email") or "",
                                     "persona": persona.key}
                    try:
                        if rec["existing_fibery_id"]:
                            update_fields = {
                                "GTM Pipeline/Outreach Status": {
                                    "fibery/id": fc.OUTREACH_STATUS[outreach_status]},
                            }
                            if likely_priorities:
                                update_fields["GTM Pipeline/Likely Priorities"] = likely_priorities
                            fc.update(fibery_token, "GTM Pipeline/Contacts", rec["existing_fibery_id"],
                                     update_fields)
                            people_seen_at_company.append(person_record)
                            fibery_contact_notes.append(
                                "{} -> existing contact updated ({})".format(persona.key, outreach_status))
                        else:
                            contact_entity = {
                                "GTM Pipeline/Name": contact["contact_name"] or "(unknown name)",
                                "GTM Pipeline/Title": contact.get("title") or "",
                                "GTM Pipeline/Email": contact["email"],
                                "GTM Pipeline/LinkedIn Profile": contact.get("linkedin") or "",
                                "GTM Pipeline/Contact Company": {"fibery/id": fcompany["id"]},
                                "GTM Pipeline/Enrichment Source": {
                                    "fibery/id": fc.ENRICHMENT_SOURCE[
                                        _map_enrichment_source(contact.get("source"))]},
                                "GTM Pipeline/Verified": contact.get("source") == "QuickEnrich",
                                "GTM Pipeline/Outreach Status": {
                                    "fibery/id": fc.OUTREACH_STATUS[outreach_status]},
                            }
                            if likely_priorities:
                                contact_entity["GTM Pipeline/Likely Priorities"] = likely_priorities
                            persona_name = PERSONA_TO_FIBERY.get(persona.key)
                            if persona_name:
                                contact_entity["GTM Pipeline/Persona"] = {
                                    "fibery/id": fc.PERSONA[persona_name]}
                            if rec["last_sequenced_hint"]:
                                contact_entity["GTM Pipeline/Last Sequenced Date"] = \
                                    rec["last_sequenced_hint"]
                            created = fc.create(fibery_token, "GTM Pipeline/Contacts", contact_entity)
                            people_seen_at_company.append(person_record)
                            # Phase 17 step 5: make this run's own creates visible to the
                            # cross-record email guard, so a second persona resolving the same
                            # address at this company is caught rather than creating a twin.
                            existing_contact_emails[contact["email"].strip().lower()] = {
                                "id": (created or {}).get("fibery/id") or "(this run)",
                                "name": contact.get("contact_name") or "",
                                "company_id": fcompany["id"],
                            }
                            fibery_contact_notes.append(
                                "{} -> contact created ({})".format(persona.key, outreach_status))
                    except RuntimeError as e:
                        fibery_contact_notes.append("{} -> contact write FAILED: {}".format(persona.key, e))

        # Phase 8: a single blank row when every persona came up empty, rather
        # than one blank row per persona -- keeps a zero-contact company
        # visible in the CSV without flooding it with near-duplicate rows.
        if not company_rows:
            reason = effective_reason or "not enriched - {}".format(qualification)
            company_rows.append(_build_row(co, "", _blank_contact(co["company"], reason), "",
                                           qualification, jobs, stated_goal, ""))

        rows.extend(company_rows)

        print("    qualify: {}".format(qualification))
        for line in persona_print_lines:
            print(line)
        print("    jobs   : {} | {}".format(
            jobs["signal_tier"], (jobs["nyc_roles"][0] if jobs["nyc_roles"] else "no NYC roles")))
        if stated_goal.get("found"):
            print("    goal   : {} ({})".format(stated_goal.get("goal") or "-",
                                                 stated_goal.get("source_type") or "?"))

        if fibery_token and fcompany and not dry_run:
            try:
                company_update = {
                    "GTM Pipeline/Research Status": {
                        "fibery/id": fc.RESEARCH_STATUS[_map_research_status(qualification)]},
                    "GTM Pipeline/Last Checked Date": datetime.now().strftime("%Y-%m-%d"),
                }
                # Signal Tier write-back (2026-09-17, prioritization build). Layer 1
                # (research_company, above) already computes this every run for every
                # active company regardless of the daily cap -- it just used to get
                # thrown away after the console print. Written for every company,
                # qualified or not, so a DISQUALIFIED-but-Tier-1 posting is still
                # visible in Fibery.
                mapped_tier = _map_signal_tier(jobs.get("signal_tier") or "")
                if mapped_tier:
                    company_update["GTM Pipeline/Signal Tier"] = {
                        "fibery/id": fc.SIGNAL_TIER[mapped_tier]}
                # Found 2026-09-17 (Phase 11 Stage 2): research_stated_goal() has
                # produced verified findings since Phase 5, but they were only ever
                # written to the CSV -- never to the Fibery Company record itself,
                # so the finding never reached the source of truth. Companies has
                # a single plain-text Stated Goal field (no separate Source/Excerpt/
                # URL fields), so source type and URL are folded into that one
                # field rather than dropped.
                if stated_goal.get("found"):
                    goal_text = stated_goal.get("goal") or ""
                    source_type = stated_goal.get("source_type") or "?"
                    url = stated_goal.get("url") or ""
                    company_update["GTM Pipeline/Stated Goal"] = (
                        "{} [source: {}; {}]".format(goal_text, source_type, url)
                        if url else "{} [source: {}]".format(goal_text, source_type)
                    )
                fc.update(fibery_token, "GTM Pipeline/Companies", fcompany["id"], company_update)
                note = "company -> {}".format(_map_research_status(qualification))
            except RuntimeError as e:
                note = "company update FAILED: {}".format(e)
            if fibery_contact_notes:
                note += " | " + " | ".join(fibery_contact_notes)
            print("    fibery : {}".format(note))
        elif fibery_token and domain and not fcompany and not dry_run:
            print("    fibery : no Fibery Company matches domain '{}' -- write-back skipped"
                  .format(domain))
        elif fibery_token and fcompany and dry_run:
            print("    fibery : dry-run -- write-back skipped")

        # Phase 18: one flushed line per company, so the on-disk record of what this run has
        # spent is never behind by more than the company in hand -- however the process ends.
        _append_spend_line(ledger, "[{}/{}] {}".format(i, total, co["company"]), spend_log_path)

        if ledger.qe_budget_exhausted():
            print("\n!! QuickEnrich credit cap ({}) reached at company {} of {}. Stopping."
                  .format(ledger.qe_budget, i, total))
            print("   Companies not processed: {}".format(
                ", ".join(c["company"] for c in companies[i:]) or "none"))
            break
    return rows, cache_stats


def scrub_needs_verification(rows: List[Dict[str, str]], ledger: CreditLedger,
                             scrub: bool) -> None:
    """Run every Apollo/Hunter-sourced email through Scrubby, per CLAUDE.md's
    mandatory verification step for anything not already QuickEnrich-verified.
    Mutates `rows` in place. No-op unless scrub=True.

    Opt-in via --scrub rather than always-on: this spends real Scrubby
    credits per email. providers.scrubby_classify maps Scrubby's
    documented response values (confirmed against their published API
    reference 2026-09-16), but no real call has been made from this code
    yet -- worth a spot-check of the 'Scrubby Raw Status' column on the
    first live batch before trusting it at volume.

    Runs the catch-all control once per domain (CLAUDE.md: a fake address
    tested first, since a "valid" result on a catch-all domain doesn't mean
    what it normally means), not once per email, so a company with several
    candidate emails doesn't pay for the probe repeatedly.

    Phase 8: a row already flagged "Recently Contacted" is skipped -- it's
    routed out of the ready/needs-verification buckets regardless of
    Scrubby's verdict (see write_outputs), so verifying its email would
    just spend Scrubby credits on a contact this run isn't sending to
    anyway.
    """
    if not scrub:
        return
    catch_all_cache: Dict[str, Optional[bool]] = {}
    for r in rows:
        email = r.get("Email") or ""
        source = r.get("Email Source/Confidence") or ""
        if not email or source.startswith("QuickEnrich"):
            continue
        if r.get("Outreach Status") == "Recently Contacted":
            continue
        if ledger.scrubby_budget_exhausted():
            r["Scrubby Verdict"] = "not run - Scrubby budget cap reached"
            continue

        domain = email.split("@")[-1].lower()
        if domain not in catch_all_cache:
            catch_all_cache[domain] = scrubby_domain_is_catch_all(ledger, domain)
        is_catch_all = catch_all_cache[domain]

        data = scrubby_validate_email(ledger, email)
        verdict = scrubby_classify(data)
        if is_catch_all is None:
            r["Scrubby Catch-All Domain"] = "unknown - probe failed"
        elif is_catch_all:
            r["Scrubby Catch-All Domain"] = "yes"
            if verdict in ("valid", "risky"):
                verdict = verdict + " (unreliable - catch-all domain)"
        else:
            r["Scrubby Catch-All Domain"] = "no"
        r["Scrubby Verdict"] = verdict
        r["Scrubby Raw Status"] = ", ".join(
            "{}={}".format(k, (data or {}).get(k)) for k in ("status", "result", "quick_status")
            if (data or {}).get(k))


def _write(rows: List[Dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_outputs(rows: List[Dict[str, str]], stamp: str, outdir: Path):
    """Split by whether the address is already verified AND safe to
    message right now.

    QuickEnrich echoes an email_verification_date, so those are send-ready.
    Apollo and Hunter addresses are not pre-verified and must go through
    Scrubby first (CLAUDE.md), so they must not sit in the same file as
    something safe to send. Phase 8: a contact flagged "Recently Contacted"
    never lands in ready/needs regardless of email source -- the per-contact
    60-day reconciliation overrides email-source verification, since the
    email being valid was never the question for that row.
    """
    recently_contacted = lambda r: r.get("Outreach Status") == "Recently Contacted"
    ready = [r for r in rows if r["Email"] and r["Email Source/Confidence"].startswith("QuickEnrich")
            and not recently_contacted(r)]
    needs = [r for r in rows if r["Email"] and not r["Email Source/Confidence"].startswith("QuickEnrich")
            and not recently_contacted(r)]
    rest = [r for r in rows if not r["Email"] or recently_contacted(r)]

    ready_path = outdir / "leads_ready_{}.csv".format(stamp)
    needs_path = outdir / "leads_needs_verification_{}.csv".format(stamp)
    rest_path = outdir / "leads_no_contact_{}.csv".format(stamp)
    _write(ready, ready_path)
    _write(needs, needs_path)
    _write(rest, rest_path)
    return (ready, ready_path), (needs, needs_path), (rest, rest_path)


def _redact_learn(names=(), protected=()) -> None:
    """Tell the active --redact wrapper about contact names (to mask) and company names (to keep
    visible). A no-op when --redact is off. Called as soon as names become known -- at startup
    from Fibery, and per persona right after the contact waterfall returns -- so every line that
    prints a name afterwards is already covered."""
    redactor = getattr(sys.stdout, "redactor", None)
    if redactor is None:
        return
    protected = [p for p in protected if p]
    names = [n for n in names if n]
    if protected:
        redactor.protect(protected)
    if names:
        redactor.add_names(names)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Multi-persona lead enrichment - qualification + contacts (Phase 8)")
    ap.add_argument("--input", required=True,
                    help="Company list CSV (name column + URL column), or the literal value "
                         "'fibery' to query GTM Pipeline/Companies live instead (Phase 9)")
    ap.add_argument("--outdir", default="outputs", help="Directory for the output CSVs")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N companies")
    ap.add_argument("--estimate", action="store_true",
                    help="Print the cost estimate and exit. Spends nothing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Free rungs only: no paid email lookups (still pays String for job research)")
    ap.add_argument("--max-credits", type=int, default=0,
                    help="Override the built-in QuickEnrich credit cap")
    ap.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP,
                    help="Override the persistent daily company-processing cap "
                         "(default: {}). Companies already fully cached across every persona "
                         "being run don't count against it; dry runs are exempt.".format(
                             DEFAULT_DAILY_CAP))
    ap.add_argument("--enrich-all", action="store_true",
                    help="Enrich every company, including disqualified ones")
    ap.add_argument("--no-cache", action="store_true",
                    help="Force a live refetch for every company, bypassing the cache. "
                         "Still writes fresh results back, so it doubles as a manual refresh.")
    ap.add_argument("--scrub", action="store_true",
                    help="Run every Apollo/Hunter email through Scrubby before writing output "
                         "(CLAUDE.md's mandatory verification step). Spends real Scrubby "
                         "credits. Off by default -- see scrub_needs_verification()'s docstring.")
    ap.add_argument("--scrubby-budget", type=int, default=200,
                    help="Cap on Scrubby credits for this run, only relevant with --scrub "
                         "(default 200)")
    ap.add_argument("--persona", default=None,
                    help="Restrict this run to a single persona's Layer 2 contact-finder "
                         "(default: run every live persona -- cfo, hr_people_ops, recruiter_ta). "
                         "See src/personas.py for the registry.")
    ap.add_argument("--company", default=None,
                    help="Restrict this run to a single company by exact name match "
                         "(case-insensitive). Added for Phase 11 Stage 1 so a single-company "
                         "live-spend test can target a specific company instead of relying on "
                         "--limit's list-order position, which isn't guaranteed.")
    ap.add_argument("--redact", action="store_true",
                    help="Mask contact names, emails and LinkedIn slugs in terminal output "
                         "(company names stay visible). Output CSVs, logs and Fibery writes are "
                         "unchanged. Independent of --dry-run.")
    args = ap.parse_args(argv)

    if not args.redact:
        return _main(args)
    # One Redactor shared by both streams: the STOPPING/WARNING lines go to stderr.
    redactor = Redactor()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = RedactingStream(real_out, redactor)
    sys.stderr = RedactingStream(real_err, redactor)
    try:
        return _main(args)
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def _main(args) -> int:

    if args.persona:
        try:
            persona = get_persona(args.persona)
        except KeyError as e:
            print("STOPPING. {}".format(e), file=sys.stderr)
            return 2
        if persona.status != "live":
            print("STOPPING. Persona '{}' ({}) has no Layer 2 contact-finder built yet "
                  "-- status: {}. Nothing to run.".format(
                      persona.key, persona.label, persona.status), file=sys.stderr)
            return 2
        personas = [persona]
    else:
        personas = [p for p in PERSONAS.values() if p.status == "live"]
        if not personas:
            print("STOPPING. No live personas found in src/personas.py.", file=sys.stderr)
            return 2

    try:
        require_keys(REQUIRED_KEYS)
        if args.scrub:
            require_keys(["SCRUBBY_API_KEY"])
    except MissingKey as e:
        print("STOPPING.\n{}".format(e), file=sys.stderr)
        return 2

    fibery_token = fc.get_token()
    use_fibery_input = args.input.strip().lower() == "fibery"

    # Loaded once, used both for --input fibery (the upstream gate) and, regardless of input
    # source, for the defense-in-depth gate + write-back inside run() -- a plain CSV gets checked
    # against Fibery too whenever a company's domain happens to match a tracked row.
    company_map: Dict[str, dict] = {}
    contacts_by_company: Dict[str, list] = {}
    existing_contact_emails: Dict[str, dict] = {}
    if fibery_token:
        try:
            company_map = fc.load_company_map(fibery_token)
            contacts_by_company = fc.load_contacts_by_company(fibery_token)
            # Phase 17 step 5: the whole Contacts table, indexed by email, for the
            # cross-record guard. load_contacts_by_company() above is grouped by company and
            # drops rows with no Contact Company set, so it cannot answer "does this address
            # exist ANYWHERE in Fibery" -- which is the actual question when one email turns
            # up on two companies' records.
            existing_contact_emails = {}
            for row in fc.load_all_contacts(fibery_token):
                key = (row.get("email") or "").strip().lower()
                if key and key not in existing_contact_emails:
                    existing_contact_emails[key] = {
                        "id": row.get("id"), "name": row.get("name"),
                        "company_id": row.get("company_id"),
                    }
            _redact_learn(names=[v.get("name") for v in existing_contact_emails.values()],
                          protected=[c.get("name") for c in company_map.values()])
        except RuntimeError as e:
            print("WARNING: couldn't load Fibery Companies/Contacts for the paid gate / "
                  "write-back ({}) -- continuing without them.".format(e), file=sys.stderr)
            company_map, contacts_by_company, existing_contact_emails = {}, {}, {}
            if use_fibery_input:
                print("STOPPING. --input fibery requires a working Fibery query.", file=sys.stderr)
                return 2
    elif use_fibery_input:
        print("STOPPING. --input fibery needs FIBERY_API_TOKEN in .env.", file=sys.stderr)
        return 2

    if use_fibery_input:
        companies = read_company_list_from_fibery(company_map)
    else:
        try:
            companies = read_company_list(args.input)
        except (FileNotFoundError, ValueError) as e:
            print("STOPPING. {}".format(e), file=sys.stderr)
            return 2

    _redact_learn(protected=[c.get("company") for c in companies])

    if args.company:
        target = args.company.strip().lower()
        companies = [c for c in companies if c["company"].strip().lower() == target]
        if not companies:
            print("STOPPING. No company named '{}' found in the input list.".format(
                args.company), file=sys.stderr)
            return 2
    if args.limit:
        companies = companies[: args.limit]
    if not companies:
        print("No usable rows in the company list.", file=sys.stderr)
        return 2

    if args.estimate:
        hb = (hunter_account() or {}).get("requests", {}).get("searches", {}).get("remaining")
        print_estimate(companies, qe_balance(), hb, len(personas))
        return 0

    cap = args.max_credits or default_credit_cap(len(companies) * len(personas))
    print("QuickEnrich credit cap for this run: {}{}".format(
        cap, "" if args.max_credits else " (built-in default)"))
    print("Personas this run: {}".format(", ".join(p.key for p in personas)))
    print("Daily company-processing cap: {}{}".format(
        args.daily_cap, "" if args.daily_cap == DEFAULT_DAILY_CAP else " (override)"))

    # Phase 18: internal deadline, env-overridable so run-cron.sh can tune it alongside its own
    # MAIN_BUDGET_SECONDS without a code change. A garbage value degrades to the default rather
    # than crashing a run over a typo in the cron environment.
    try:
        deadline_sec = int(os.environ.get("MAIN_PY_DEADLINE_SEC", DEFAULT_MAIN_DEADLINE_SEC))
    except ValueError:
        print("WARNING: MAIN_PY_DEADLINE_SEC is not an integer -- using the default {}s."
              .format(DEFAULT_MAIN_DEADLINE_SEC), file=sys.stderr)
        deadline_sec = DEFAULT_MAIN_DEADLINE_SEC
    print("Internal deadline    : {}s{} (outer time-box is run-cron.sh's MAIN_BUDGET_SECONDS)"
          .format(deadline_sec, "" if deadline_sec == DEFAULT_MAIN_DEADLINE_SEC else " (override)"))

    ledger = CreditLedger(qe_budget=cap, scrubby_budget=args.scrubby_budget if args.scrub else None)
    # Installed as soon as the ledger exists, so a SIGTERM at any point from here on still
    # produces a spend summary and a spend-log line instead of dying silently.
    _install_sigterm_handler(ledger)
    rows, cache_stats = run(companies, ledger, dry_run=args.dry_run,
                            enrich_all=args.enrich_all, personas=personas,
                            no_cache=args.no_cache, fibery_token=fibery_token,
                            company_map=company_map,
                            contacts_by_company=contacts_by_company,
                            existing_contact_emails=existing_contact_emails,
                            daily_cap=args.daily_cap,
                            deadline_sec=deadline_sec)

    if args.scrub:
        print("\nRunning Scrubby verification on Apollo/Hunter emails...")
    scrub_needs_verification(rows, ledger, scrub=args.scrub)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ready, ready_path), (needs, needs_path), (rest, rest_path) = write_outputs(
        rows, stamp, Path(args.outdir))

    qual = sum(1 for r in rows if r["Qualification"].startswith("QUALIFIED"))
    disq = sum(1 for r in rows if r["Qualification"].startswith("DISQUALIFIED"))
    manual = sum(1 for r in rows if r["Qualification"].startswith("NO BOARD"))
    recently_contacted = sum(1 for r in rows if r.get("Outreach Status") == "Recently Contacted")
    print("\n--- {} companies processed, {} contact rows ---".format(len(companies), len(rows)))
    print("Qualified            : {}".format(qual))
    print("Disqualified         : {}".format(disq))
    print("No board / manual    : {}".format(manual))
    print("Emails ready to send : {}  -> {}".format(len(ready), ready_path))
    print("Emails needing Scrubby: {} -> {}".format(len(needs), needs_path))
    print("No contact / recently contacted: {}  -> {}".format(len(rest), rest_path))
    print("  (of which recently contacted, held back by the 60-day gate): {}".format(recently_contacted))
    print("Spend                : {}".format(ledger.summary()))
    # Phase 19: Anthropic is billed in tokens rather than credits, so it gets its own breakdown
    # by call site -- the one-line summary above can only carry the total.
    for line in ledger.anthropic_breakdown_lines():
        print(line)
    print("String fetches       : {} total ({} markdown, {} executeJS, {} raw)"
          .format(fetch_total(), FETCH_COUNTS["markdown"], FETCH_COUNTS["executejs"],
                  FETCH_COUNTS["raw"]))
    if companies:
        print("String fetches/company: {:.1f}".format(fetch_total() / len(companies)))
    print("Cache (company research): {} hit, {} miss".format(
        cache_stats["company_hit"], cache_stats["company_miss"]))
    print("Cache (contacts, all personas): {} hit, {} miss".format(
        cache_stats["contact_hit"], cache_stats["contact_miss"]))
    print("Fibery write-back       : {}".format(
        "enabled ({} companies tracked, {} with existing contacts)".format(
            len(company_map), len(contacts_by_company))
        if fibery_token else "disabled (no FIBERY_API_TOKEN)"))
    if args.scrub:
        verdicts = [r.get("Scrubby Verdict") for r in needs if r.get("Scrubby Verdict")]
        valid_ct = sum(1 for v in verdicts if v and v.startswith("valid"))
        invalid_ct = sum(1 for v in verdicts if v == "invalid")
        risky_ct = sum(1 for v in verdicts if v and v.startswith("risky"))
        unknown_ct = len(verdicts) - valid_ct - invalid_ct - risky_ct
        print("Scrubby verification  : {} valid, {} invalid, {} risky, {} unknown (of {} checked)"
              .format(valid_ct, invalid_ct, risky_ct, unknown_ct, len(verdicts)))
        print("  NOTE: verdicts are mapped from Scrubby's own published API docs, not from a real\n"
              "  call made by this code yet -- spot-check the 'Scrubby Raw Status' column on the\n"
              "  first live batch before trusting it at volume.")
    for w in dict.fromkeys(ledger.warnings):
        print("WARNING: {}".format(w))
    print("\nStep 3B (Apollo sequence enrolment) NOT run - needs explicit approval\n"
          "after you review these spreadsheets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
