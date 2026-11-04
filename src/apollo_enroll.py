"""
apollo_enroll.py -- Phase 12: writes LLM-generated, persona-grounded email copy into Apollo's
Ghost Email 1/2/3 + Ghost Subject 1 custom contact fields, then enrolls the contact in the
existing "Sales Outreach" sequence. Reads only what main.py already wrote to Fibery (this run or
any prior one) -- Outreach Status == "Cleared to proceed" (the Phase 8 60-day gate) AND Sequence
Status == "Not Sequenced" AND a resolved email + Persona + Likely Priorities -- never triggers new
research of its own. Full design: claude/nycbound-gtm-phase12-apollo-hypersignal-scope.md (Sales &
Marketing claude.ai Project).

Chained after `main.py --input fibery` in hypersignal/run-cron.sh. Talks to Fibery via
src/fibery_client.py and to Apollo via its REST API (same house style as src/providers.py:
requests + APOLLO_API_KEY from .env) -- no MCP, no Claude in the loop, so this step is
deterministic and doesn't depend on an LLM correctly following instructions every single weekday
(the one LLM call this script does make -- generating the email copy itself -- is guardrailed the
same way research_likely_priorities()/research_stated_goal() are: a response that fails
validation is skipped, never shipped under the bar).

The email play a contact's copy is grounded in is read live out of fibery_docs/email_plays.md at
run time, never hardcoded into this file -- Rachel edits plays in Fibery, not in this repo. Keep
that file in sync with Fibery (see its own header) or this script will ground new copy in a stale
play. (Found and fixed once already, 2026-09-18: the cached file still had the archived
"Recruiters / Talent Partners" play as if it were live, days after "Real Support vs. Lump Sum"
replaced it -- refreshed from Fibery before this script was written.)

Phase 17 (2026-10-01) added two pre-enrollment guards, applied per contact before any LLM call
is made (so a skip costs nothing). Both exist because real duplicates reached Apollo:
  - Person-level dedupe. A contact is skipped when ANOTHER Fibery contact for the same human at
    the same company is already in flight -- Sequence Status "Sequenced" or Outreach Status
    "Recently Contacted". Matching is nickname- and initial-aware (providers.same_person), because
    the duplicate pair is routinely spelled differently by two providers ("Sam Hollis" vs
    "Samantha Hollis", Apollo's masked "Robert G" vs Hunter's "Robert Gorski") and usually carries
    two DIFFERENT emails -- which is exactly why Apollo's own email-based dedupe never caught them.
  - Domain guard. A contact is skipped when its email domain isn't the company's Fibery Domain
    (providers.email_domain_matches_company) -- a contractco.io address on an Initech record is an
    agency contractor or a stale row, not someone to email about Initech's hiring.
Every skip is printed with its reason and counted in the run summary as "skipped-guard"; nothing
is ever dropped silently.

Usage (run as a module from the repo root -- this file uses package-relative imports, so
running it directly as `python3 src/apollo_enroll.py` fails with ImportError; found 2026-09-19
when that stale form was handed out as the documented usage and broke on first real use):
    python3 -m src.apollo_enroll                  # real run, enrolls every eligible contact
    python3 -m src.apollo_enroll --dry-run         # every stage runs; prints the Apollo/Fibery writes it skipped
    python3 -m src.apollo_enroll --redact          # mask contact names/emails in terminal output (demos)
    python3 -m src.apollo_enroll --limit 3         # cap this one invocation (testing only -- the
                                                    # cron invocation never passes this). No cap
                                                    # by default: main.py's --daily-cap (25
                                                    # companies/run) is the real, intended
                                                    # throttle upstream; a second cap here on top
                                                    # of it isn't needed and used to leave eligible
                                                    # contacts un-enrolled for no reason (this was
                                                    # a testing-only value that leaked into the
                                                    # real default -- fixed 2026-09-19).
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from . import cache
    from . import fibery_client as fc
    from . import providers as prov
    from .redact import Redactor, RedactingStream
except ImportError:  # running as a script (python3 src/apollo_enroll.py), not as a package module
    import cache
    import fibery_client as fc
    import providers as prov
    from redact import Redactor, RedactingStream

REPO_ROOT = Path(__file__).resolve().parent.parent
EMAIL_PLAYS_PATH = REPO_ROOT / "fibery_docs" / "email_plays.md"
CASE_STUDIES_PATH = REPO_ROOT / "fibery_docs" / "case_studies.md"

# --- Apollo account constants (pulled live 2026-09-18, see the Phase 12 scope doc) --------------
SALES_OUTREACH_SEQUENCE_ID = "6a28d18878f59c001469b89f"
GHOST_EMAIL_FIELD_IDS = {
    1: "690b8da2e4d3d900190c8f34",
    2: "690b8dbaf937700011e18e76",
    3: "690b8dcb239b9d00198ef31c",
}
GHOST_SUBJECT_1_FIELD_ID = "690b8d82e60bf2000dd5bcad"
# Personalization line (6a78abf22dba9700149db51f) is deliberately NEVER written by this script.
# Rachel's call, 2026-09-18: that field belongs to Investor Outreach's single-fixed-template
# design, where it's the one thing that varies contact to contact. Sales Outreach has no fixed
# template for it to slot into -- every field this script writes is already fully LLM-written.

# Fibery Persona enum name (see fibery_client.PERSONA) -> the exact "## <heading>" this persona's
# live play lives under in fibery_docs/email_plays.md.
PERSONA_PLAY_HEADING = {
    "CFO/Finance": "CFO / Finance Leader",
    "People Ops": "Ramp Risk (People Ops / HR Leader)",
    "Recruiter/TA": "Real Support vs. Lump Sum (Recruiter / Talent Partner)",
}

UNIVERSAL_RULES_HEADING = "Universal Cold Email Rules (Apply to Every Play)"

# Phase 18: how many DISTINCT COMPANIES may start enrolling per calendar day, counted across
# every invocation that day (cache.py's enroll_daily_runs table).
#
# Companies, not contacts. A contact cap was tried before and removed as a mistake
# (MAX_CONTACTS_PER_RUN, 2026-09-19): it throttled the wrong axis, leaving eligible contacts at an
# already-started company un-enrolled for no reason while doing nothing about how wide the day's
# outreach reached. The thing worth bounding is how many new companies hear from us in a day, so
# once 25 companies have enrolled at least one contact today, no NEW company is started -- and
# every remaining eligible contact at a company already in progress still goes out.
#
# Persisted rather than in-memory because the cron can legitimately run twice in a day (a manual
# catch-up after a timeout), and an in-memory counter would reset and silently double the day's
# reach.
DAILY_ENROLL_COMPANY_CAP = 25

# Phase 18: how recent is "too recent" to cold-sequence someone, measured against Apollo's own
# last_activity_date. Same 60 days as the Phase 8 Fibery-side gate, deliberately -- two views of
# one question, and this one catches what the Fibery view cannot (see the guards below).
_RECENT_ACTIVITY_DAYS = 60

# Apollo contact/email sources that mean the record came out of Rachel's own mailbox rather than
# from prospecting -- i.e. somebody she is already corresponding with. Dropping a cold sequence on
# one of those is the worst thing this script can do, and neither Fibery's Sequence Status nor its
# Last Sequenced Date knows anything about it: a mailbox-synced contact has never been "sequenced".
#
# "email_import" is the confirmed value (Leah Brandt's record, 2026-10-02). The rest are
# defensive -- Apollo's naming for mailbox-sync sources has varied -- and source_display_name is
# matched as a substring so "Synced from mailbox" style labels are caught whatever the underlying
# enum is called. For contrast, the values confirmed on real prospected records and deliberately
# NOT matched here: "api" ("Created from API") and "csv_import" ("Uploaded from CSV").
_MAILBOX_SOURCES = {"email_import", "mailbox_sync", "email_sync", "mailbox", "gmail", "outlook"}
_MAILBOX_DISPLAY_SUBSTRINGS = ("mailbox", "synced from email", "email sync")

# Phase 20: Apollo's own deliverability verdict, which is NOT the `email_status` field this script
# used to read. All four of the 2026-10-02 failed enrollments carried email_status "verified" while
# email_true_status disagreed, so a contact can look verified and still be declined on add.
#
# Only "Potentially Invalid" is skipped. "User Managed" is deliberately NOT -- it means a human set
# the address deliberately, which is a reason to trust it, not to doubt it (casey@example.net and
# dmercer@acme.com are both User Managed). Matched lowercase so Apollo's
# capitalisation can drift without silently disabling the guard.
_SKIP_EMAIL_TRUE_STATUSES = ("potentially invalid",)

# Apollo error bodies can be a full HTML page, so the excerpt is bounded.
_APOLLO_BODY_CHARS = 500


def enroll_failure_log_path() -> Path:
    """outputs/enroll_failures_YYYYMMDD.log.

    Its own file rather than the shared spend log: a failure body is diagnostic text, often a few
    hundred characters of JSON or HTML, and interleaving that with per-company spend lines would
    make both harder to read. Same outputs/ directory and same dated-file convention.
    """
    return Path("outputs") / "enroll_failures_{}.log".format(datetime.now().strftime("%Y%m%d"))


def _log_enroll_failure(email: str, company_name: str, detail: str,
                        log_path: Optional[Path] = None) -> None:
    """Append one enrollment failure, flushed. Best-effort -- a logging failure must never be the
    reason a run stops, same discipline as main.py's manual-review and spend logs."""
    path = log_path or enroll_failure_log_path()
    line = "{}\t{}\t{}\t{}\n".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), company_name or "?", email or "?", detail)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as e:
        print("    !! could not write enrollment-failure log ({}): {}".format(path, e))


def _apollo_body_excerpt(data: Any, raw_text: str = "") -> str:
    """Apollo's own response body, whitespace-collapsed and truncated to 500 chars.

    The BODY only. Request headers -- the one place the API key appears -- are never passed in and
    never logged, and the body is Apollo's own output, so it carries no credential of ours.
    """
    try:
        body = json.dumps(data) if data is not None else (raw_text or "")
    except (TypeError, ValueError):
        body = str(data)
    return " ".join(str(body).split())[:_APOLLO_BODY_CHARS]

# STAGED ROLLOUT HISTORY (Rachel's gate, 2026-09-18 -- see the Phase 12 scope doc).
#
# The staged rollout is COMPLETE and its mechanism is gone. Kept as history because each stage
# found a real bug, and because the shape of the throttle changed:
#   Stage 1 (2026-09-18): 1 contact, first automated run -- live-sent successfully (one
#     Recruiter/TA contact). Two Apollo API param bugs and two copy rules (company-name leak,
#     missing sign-off) fixed as a result.
#   Stage 2 (2026-09-18): multiple contacts within 1 company. Nora Vance enrolled cleanly;
#     Owen Pratt was correctly skipped on a validation failure, then correctly blocked from a
#     second enrollment -- Apollo had silently declined to add him over a same-day job change,
#     which the code now catches instead of reporting a false success.
#   Stage 3 (2026-09-19): the per-contact cap it was enforced with (MAX_CONTACTS_PER_RUN = 12)
#     turned out to be a testing value that had leaked into the real default, and was removed
#     (69f8a5c). It throttled the wrong axis.
#   Now (2026-10-02): the real throttle is DAILY_ENROLL_COMPANY_CAP below -- distinct COMPANIES
#     per calendar day, persisted across runs. No per-contact cap exists, deliberately.

# Guards against the model echoing the play's own illustrative worked example instead of writing
# a fresh one (the play docs explicitly say worked examples are "illustrative only, don't follow
# word for word") -- these strings only ever appear in this repo inside those worked examples.
_PLACEHOLDER_TOKENS = ("{{", "}}", "Rowan", "Sr. Data Eng", "Gotta get that")


def _load_markdown_section(path: Path, heading: str) -> Optional[str]:
    """Extracts everything between a top-level `## <heading>` line (exact match, stripped) and
    the next `## ` line, from a markdown file. Used to pull one persona's live play, and the
    Universal Cold Email Rules, straight out of fibery_docs/email_plays.md at run time."""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").split("\n")
    out: List[str] = []
    capturing = False
    for line in lines:
        if line.startswith("## "):
            if line[3:].strip() == heading:
                capturing = True
                out.append(line)
                continue
            elif capturing:
                break
        if capturing:
            out.append(line)
    return "\n".join(out).strip() if out else None


_GHOST_EMAIL_PROMPT = (
    "You write cold outreach email copy for Pinpointe NYCbound, a company that helps employers "
    "relocate new NYC hires. Below are Pinpointe's own approved rules and the specific play for "
    "this contact's persona -- follow them exactly. You are NOT researching this specific person "
    "-- you have no information about them beyond what's given below.\n\n"
    "CONTACT: {contact_name}, {title}.\n\n"
    "WHAT WE KNOW ABOUT THIS CONTACT'S LIKELY PRIORITIES (Pinpointe's own guardrailed research "
    "on this contact -- your primary source for what to reference about them):\n{likely_priorities}\n\n"
    "{stated_goal_block}"
    "UNIVERSAL RULES (apply to every play):\n{universal_rules}\n\n"
    "THIS CONTACT'S PLAY:\n{play_section}\n\n"
    "APPROVED CASE STUDY LIBRARY (the ONLY case studies, customer stories, or outcomes you may "
    "ever reference -- these are real, verified relocations; nothing else is real):\n"
    "{case_studies}\n\n"
    "Hard rules on top of the play's own rules above:\n"
    "- Never write the specific company's name anywhere in the subject or body. Use \"you/your\" "
    "and the contact's first name only -- exactly as the play itself requires.\n"
    "- Never state as settled fact that this specific person or their specific hire is relocating, "
    "comparing offers, or hesitating -- frame at the pattern/role level, using hedged language, "
    "exactly as the play's own hard rules require.\n"
    "- Never invent a specific fact (a name, a number, a quote) that isn't in the inputs above or "
    "in the play's own approved case studies/numbers.\n"
    "- Never invent a case study, customer story, or outcome -- not a name, a role, a timeframe, "
    "or a detail. When the play calls for a case study, choose the single best match from the "
    "APPROVED CASE STUDY LIBRARY above for this persona and role/situation, and use only the "
    "details given there. If none of them fit reasonably well, leave the case study out of the "
    "email entirely rather than inventing one.\n"
    "- Whenever you cite a dollar figure, salary, or comp band, name the specific role it "
    "applies to, using the real title given in your inputs above -- never a vague placeholder "
    "like \"a local hire\" or \"that role,\" and never an invented title (e.g. \"Sr. SWE,\" "
    "\"sr. director\") that isn't grounded in what you were given. If no specific title is "
    "given, describe the role generically without inventing one (e.g. \"a senior hire\") "
    "rather than guessing a title.\n"
    "- Any Year-1 net value / dollar gap figures the play calls for: if WHAT WE KNOW ABOUT THIS "
    "CONTACT'S LIKELY PRIORITIES contains a bracketed [RAMP CALC ...] block, those figures are "
    "the ones to cite, in place of the play's own worked-example numbers (which are for a "
    "different, illustrative salary); otherwise cite the play's own approved numbers instead. "
    "Whichever source you used, state the resulting figures as plain, certain fact and nothing "
    "else -- the email must never describe its own sourcing, process, or inputs in any way. "
    "Banned, verbatim and in substance -- do not write anything like these, ever: \"no [RAMP "
    "CALC] block was provided\", \"pulling from the approved default\", \"since no data was "
    "given\", \"using the default numbers\", \"a calculation was run\", or any other sentence "
    "that tells the reader where a number came from rather than just stating the number. A "
    "sentence that mentions \"provided\", \"default\", \"block\", or \"calculation\" in "
    "connection with these figures is always wrong, no exceptions.\n"
    "- Use the exact basis phrase given (e.g. \"the JD floor of $X\" or \"the market rate of "
    "$X\") for where the salary came from -- never call a market-rate estimate a JD floor.\n"
    "- Write a fresh worked example if the play shows one -- never copy the play's worked example "
    "verbatim, it is illustrative only.\n\n"
    "Respond with plain text in exactly this structure (labels verbatim, each on its own line, "
    "nothing before SUBJECT: or after EMAIL 3's body):\n\n"
    "SUBJECT: <email 1's subject line>\n"
    "EMAIL 1:\n<email 1 body>\n"
    "EMAIL 2:\n<email 2 body -- reply in the same thread, no new subject>\n"
    "EMAIL 3:\n<email 3 body -- reply in the same thread, no new subject>"
)


def _build_stated_goal_block(stated_goal: Optional[str]) -> str:
    if not stated_goal:
        return ""
    return ("WHAT WE KNOW ABOUT THIS COMPANY'S STATED GOAL (verified, from the job posting or a "
            "cited source -- your only source for a specific hiring goal/deadline):\n{}\n\n"
           ).format(stated_goal)


def _parse_ghost_email_response(text: str) -> Optional[Dict[str, str]]:
    """Parses the SUBJECT:/EMAIL 1:/EMAIL 2:/EMAIL 3: structure. Returns None on any structural
    miss -- same "skip rather than ship" behavior as _validate_likely_priorities."""
    if not text:
        return None
    subject_m = re.search(r"SUBJECT:\s*(.+)", text)
    email1_m = re.search(r"EMAIL 1:\s*(.*?)(?=\nEMAIL 2:|\Z)", text, re.S)
    email2_m = re.search(r"EMAIL 2:\s*(.*?)(?=\nEMAIL 3:|\Z)", text, re.S)
    email3_m = re.search(r"EMAIL 3:\s*(.*)", text, re.S)
    if not (subject_m and email1_m and email2_m and email3_m):
        return None
    result = {
        "subject": subject_m.group(1).strip().splitlines()[0].strip(),
        "email_1": email1_m.group(1).strip(),
        "email_2": email2_m.group(1).strip(),
        "email_3": email3_m.group(1).strip(),
    }
    if not all(result.values()):
        return None
    return result


def _validate_ghost_email(parsed: Dict[str, str], company_name: str) -> bool:
    """Mechanical guardrail check -- not a substitute for Rachel's own review of Stage 1's actual
    output, but catches the two failure modes that are cheap to check for certain: a leaked
    company name (both live plays' shared #1 hard rule) and leftover placeholder/worked-example
    text (a sign the model echoed the play's illustrative example instead of writing its own)."""
    all_text = " ".join(parsed.values())
    if company_name and company_name.strip() and company_name.strip().lower() in all_text.lower():
        return False
    if any(tok in all_text for tok in _PLACEHOLDER_TOKENS):
        return False
    if len(parsed["subject"].split()) > 9:
        return False
    return True


def generate_ghost_emails(contact: Dict[str, Any], company_name: str,
                          stated_goal: Optional[str], ledger=None) -> Optional[Dict[str, str]]:
    """Public entry point. Returns {"subject", "email_1", "email_2", "email_3"} or None if the
    persona has no live play mapped, the play/rules can't be loaded, the LLM call fails, or the
    response fails validation -- in every None case the caller skips this contact this run, same
    "skip rather than ship under the bar" behavior as research_likely_priorities()/
    research_stated_goal()."""
    play_heading = PERSONA_PLAY_HEADING.get(contact["persona"])
    if not play_heading:
        return None
    play_section = _load_markdown_section(EMAIL_PLAYS_PATH, play_heading)
    universal_rules = _load_markdown_section(EMAIL_PLAYS_PATH, UNIVERSAL_RULES_HEADING)
    case_studies = CASE_STUDIES_PATH.read_text(encoding="utf-8").strip() if CASE_STUDIES_PATH.exists() else None
    if not play_section or not universal_rules or not case_studies:
        return None
    prompt = _GHOST_EMAIL_PROMPT.format(
        contact_name=contact["name"] or "this contact",
        title=contact["title"] or "",
        likely_priorities=contact["likely_priorities"],
        stated_goal_block=_build_stated_goal_block(stated_goal),
        universal_rules=universal_rules,
        play_section=play_section,
        case_studies=case_studies,
    )
    try:
        text = prov._llm_extract(prompt, ledger=ledger, max_tokens=900,
                                 call_site="ghost-email")
    except prov.MissingKey:
        return None
    parsed = _parse_ghost_email_response((text or "").strip())
    if not parsed:
        return None
    if not _validate_ghost_email(parsed, company_name):
        return None
    return parsed


# --- Pre-enrollment guards (Phase 17) -----------------------------------------------------------
#
# Both run before generate_ghost_emails(), so a skipped contact never costs an LLM call. See the
# module docstring for why each exists; the matching logic itself is shared with main.py's own
# write-time guards and lives in providers.py, not here.

# Statuses that mean a person is already in flight: enrolled in a sequence by this pipeline, or
# held back by the Phase 8 60-day recency gate. Either way a SECOND record for the same human must
# not be enrolled on top of them. Deliberately narrow -- "Bounced", "Replied" and "Do Not Contact"
# are their own situations and are not this guard's business.
_IN_FLIGHT_SEQUENCE_STATUSES = ("Sequenced",)
_IN_FLIGHT_OUTREACH_STATUSES = ("Recently Contacted",)


def duplicate_person_skip_reason(contact: Dict[str, Any],
                                 company_contacts: List[Dict[str, Any]]) -> Optional[str]:
    """Reason string if another Fibery contact for the SAME person at this company is already in
    flight, else None. `company_contacts` is every contact row at this company (fibery_client.
    load_all_contacts, grouped by company_id) -- including, harmlessly, this contact's own row,
    which is skipped by id.

    Only the already-in-flight rows are compared, not every sibling: two genuinely different
    finance contacts at one company are normal and expected (the pipeline resolves up to 3 per
    persona), so the question is never "is there another contact here" but "is this specific human
    already being messaged under a different spelling of their name".
    """
    for other in company_contacts:
        if other.get("id") and other.get("id") == contact.get("id"):
            continue
        blockers = []
        if other.get("sequence_status") in _IN_FLIGHT_SEQUENCE_STATUSES:
            blockers.append("Sequence Status '{}'".format(other["sequence_status"]))
        if other.get("outreach_status") in _IN_FLIGHT_OUTREACH_STATUSES:
            blockers.append("Outreach Status '{}'".format(other["outreach_status"]))
        if not blockers:
            continue
        if prov.same_person(contact, other):
            return "same person already in flight: '{}' <{}> has {}".format(
                other.get("name") or "(unnamed)", other.get("email") or "no email",
                " and ".join(blockers))
    return None


def _days_since_iso(timestamp: Optional[str], today_str: str) -> Optional[int]:
    """Whole days between an Apollo ISO timestamp and today, or None if unparseable/absent.
    Apollo returns "2026-09-21T16:39:34.000+00:00"; only the date half is used, since a 60-day
    window is not sensitive to hours or timezone."""
    if not timestamp:
        return None
    try:
        d = datetime.strptime(str(timestamp)[:10], "%Y-%m-%d")
        t = datetime.strptime(today_str[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (t - d).days


def apollo_precheck_skip_reason(apollo_contact: Optional[Dict[str, Any]],
                                today_str: str) -> Optional[str]:
    """Reason not to enroll, read off the contact's live Apollo record, or None.

    Two checks, both of which the Fibery-side guards structurally cannot make:

    1. last_activity_date inside 60 days. Fibery's Phase 8 gate asks the same question, but it
       answers it from Last Sequenced Date -- which is blank for anyone this pipeline never
       sequenced. Apollo's record knows about activity that happened outside the pipeline.
    2. The record came from a mailbox/email import rather than prospecting. Someone synced out of
       Rachel's inbox is already in a conversation; Fibery has no field that says so, and their
       Sequence Status is legitimately "Not Sequenced" because nothing ever sequenced them.

    Leah Brandt is the case that motivated both: an existing Fibery row with NO Last Sequenced
    Date (so the 60-day Fibery gate cleared her), Apollo source email_import, last activity
    2026-09-21. Every Fibery-side guard passed her; only her Apollo record shows the problem.

    `apollo_contact` is whatever /contacts/search returned (None when Apollo has no record, which
    is the normal case for a genuinely new prospect and never a reason to skip).
    """
    if not apollo_contact:
        return None

    days = _days_since_iso(apollo_contact.get("last_activity_date"), today_str)
    if days is not None and days <= _RECENT_ACTIVITY_DAYS:
        return ("Apollo shows activity {} day(s) ago ({}), inside the {}-day window".format(
            days, str(apollo_contact.get("last_activity_date"))[:10], _RECENT_ACTIVITY_DAYS))

    for field in ("source", "original_source", "email_source"):
        value = str(apollo_contact.get(field) or "").strip().lower()
        if value and value in _MAILBOX_SOURCES:
            return ("Apollo {} is '{}' -- a mailbox/email import, not a prospected record".format(
                field, value))

    # Phase 20: Apollo's deliverability verdict, which disagrees with email_status often enough to
    # matter -- all four of the 2026-10-02 failed enrollments were email_status "verified". Only
    # "Potentially Invalid" skips; "User Managed" explicitly does not.
    true_status = str(apollo_contact.get("email_true_status") or "").strip()
    if true_status.lower() in _SKIP_EMAIL_TRUE_STATUSES:
        return ("Apollo email_true_status is '{}' -- Apollo doubts this address will deliver, and "
                "declines the sequence add even though email_status reads '{}'".format(
                    true_status, apollo_contact.get("email_status") or "unknown"))

    display = str(apollo_contact.get("source_display_name") or "").strip().lower()
    if display and any(frag in display for frag in _MAILBOX_DISPLAY_SUBSTRINGS):
        return ("Apollo source_display_name is '{}' -- a mailbox/email import, not a prospected "
                "record".format(apollo_contact.get("source_display_name")))

    return None


def company_cap_reached(company_id: str, today_str: str,
                        cap: int = DAILY_ENROLL_COMPANY_CAP) -> bool:
    """Whether starting this company would exceed today's distinct-company enrollment cap.

    Always False for a company already in progress today, however full the day is -- the cap
    bounds how many companies get STARTED, not how many contacts each yields, so a company that
    has already sent today keeps working through its remaining eligible contacts.
    """
    if cache.has_company_enrolled_today(today_str, company_id):
        return False
    return cache.get_enrolled_company_count(today_str) >= cap


def contact_skip_reason(contact: Dict[str, Any], company: Dict[str, Any],
                        company_contacts: List[Dict[str, Any]]) -> Optional[str]:
    """The full pre-enrollment check: returns a reason string if this contact must NOT be enrolled
    this run, or None to proceed. Pure function of its inputs -- no network, no writes -- so the
    caller decides what to do with a skip (log it, count it, move on)."""
    # Phase 21: never sequence anyone hanging off a combined (comma-joined) company record. Its Domain,
    # Stated Goal and the job data behind Likely Priorities describe several companies at once.
    combined = fc.combined_company_reason((company or {}).get("name"), (company or {}).get("domain"))
    if combined:
        return combined
    dup = duplicate_person_skip_reason(contact, company_contacts)
    if dup:
        return dup
    # Phase 18: a contact promoted to the CRM is in a real conversation (promote_to_crm.py only
    # links one after Rachel judges a reply positive). Cold-sequencing them would be the single
    # most damaging thing this script could do, and nothing else here would catch it -- their
    # Sequence Status can legitimately read "Not Sequenced".
    crm_contact_id = (contact.get("crm_contact_id") or "").strip()
    if crm_contact_id:
        return ("already linked to a CRM Contact (id {}) -- promoted contacts are in a live "
                "conversation, not a cold sequence".format(crm_contact_id))
    company_domain = (company or {}).get("domain") or ""
    email = contact.get("email") or ""
    if not prov.email_domain_matches_company(email, company_domain):
        return "email domain '{}' does not match company Domain '{}'".format(
            email.split("@")[-1], company_domain)
    return None


# --- Apollo REST calls (same house style as src/providers.py: requests + _apollo_headers) -------

def _apollo_find_contact_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Returns the existing Apollo Contact record for `email`, or None. Reuses the same
    /contacts/search endpoint providers.apollo_contact_activity() already verified live
    2026-09-17 -- q_keywords is a fuzzy match, so the result is confirmed against the exact
    email before being trusted, same discipline as that function."""
    try:
        r = prov._session.post(prov.APOLLO_BASE + "/contacts/search", headers=prov._apollo_headers(),
                               json={"q_keywords": email, "per_page": 5}, timeout=prov.TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        prov._provider_error(None, "apollo/contacts-search", email, "request failed: {}".format(exc))
        return None
    if r.status_code >= 400 or data.get("error"):
        prov._provider_error(None, "apollo/contacts-search", email,
                             "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:120]))
        return None
    email_l = email.strip().lower()
    for c in data.get("contacts") or []:
        if (c.get("email") or "").strip().lower() == email_l:
            return c
    return None


def _apollo_already_in_sequence(apollo_contact: Dict[str, Any], sequence_id: str) -> bool:
    """Defensive dedupe check (scope-doc edge case: a contact already enrolled or previously
    finished should be skipped, not double-enrolled). Apollo's contact record carries
    `contact_campaign_statuses` per Apollo's own API; if that key is absent from this account's
    response shape, this returns False (proceed) rather than guessing -- a missing field here is
    not evidence of anything either way."""
    statuses = apollo_contact.get("contact_campaign_statuses") or []
    return any(s.get("emailer_campaign_id") == sequence_id and s.get("status") in ("active", "finished")
              for s in statuses)


def _apollo_create_contact(contact: Dict[str, Any], company_name: str) -> Optional[str]:
    parts = (contact["name"] or "").split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    body = {
        "first_name": first or None,
        "last_name": last or None,
        "email": contact["email"],
        "title": contact["title"] or None,
        "organization_name": company_name or None,
        "linkedin_url": contact["linkedin"] or None,
    }
    body = {k: v for k, v in body.items() if v}
    try:
        r = prov._session.post(prov.APOLLO_BASE + "/contacts", headers=prov._apollo_headers(),
                               json=body, timeout=prov.TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        prov._provider_error(None, "apollo/contacts-create", contact["email"],
                             "request failed: {}".format(exc))
        return None
    if r.status_code >= 400 or data.get("error"):
        prov._provider_error(None, "apollo/contacts-create", contact["email"],
                             "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:120]))
        return None
    return (data.get("contact") or {}).get("id")


def _apollo_update_ghost_fields(apollo_contact_id: str, ghost: Dict[str, str]) -> bool:
    typed_custom_fields = {
        GHOST_SUBJECT_1_FIELD_ID: ghost["subject"],
        GHOST_EMAIL_FIELD_IDS[1]: ghost["email_1"],
        GHOST_EMAIL_FIELD_IDS[2]: ghost["email_2"],
        GHOST_EMAIL_FIELD_IDS[3]: ghost["email_3"],
    }
    try:
        r = prov._session.put(
            prov.APOLLO_BASE + "/contacts/{}".format(apollo_contact_id),
            headers=prov._apollo_headers(),
            json={"typed_custom_fields": typed_custom_fields}, timeout=prov.TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        prov._provider_error(None, "apollo/contacts-update", apollo_contact_id,
                             "request failed: {}".format(exc))
        return False
    if r.status_code >= 400 or data.get("error"):
        prov._provider_error(None, "apollo/contacts-update", apollo_contact_id,
                             "HTTP {} {}".format(r.status_code, data.get("error") or str(data)[:120]))
        return False
    return True


def _apollo_default_send_account_id() -> Optional[str]:
    """Apollo id of the mailbox to send from -- the account's own linked inbox marked
    default: true (same one the Apollo UI pre-selects). Required by /add_contact_ids since an
    Apollo API change added it after this build's original wiring audit (Phase 7); found live,
    2026-09-18, when the first real Stage 1 send returned HTTP 422 without it. Never hardcoded --
    fetched fresh each run in case the linked mailbox ever changes."""
    try:
        r = prov._session.get(prov.APOLLO_BASE + "/email_accounts", headers=prov._apollo_headers(),
                              timeout=prov.TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        prov._provider_error(None, "apollo/email-accounts", "n/a", "request failed: {}".format(exc))
        return None
    if r.status_code >= 400:
        prov._provider_error(None, "apollo/email-accounts", "n/a",
                             "HTTP {} {}".format(r.status_code, str(data)[:120]))
        return None
    for acct in data.get("email_accounts") or []:
        if acct.get("default"):
            return acct.get("id")
    accts = data.get("email_accounts") or []
    return accts[0].get("id") if accts else None


def _apollo_add_to_sequence(apollo_contact_id: str, sequence_id: str, email: str):
    """Returns (ok, detail). `detail` is None on success and a failure description otherwise.

    Phase 20: returns the detail rather than a bare bool so Apollo's own response body reaches the
    caller's failure message and the enrollment-failure log. The 2026-10-02 failures were all the
    HTTP-200-but-not-added case, and the body was discarded at the time -- which is exactly why
    nobody could say WHY Apollo declined them. Logged body only, never request headers.
    """
    send_from = _apollo_default_send_account_id()
    if not send_from:
        detail = "no linked Apollo email account found to send from"
        prov._provider_error(None, "apollo/add-to-sequence", apollo_contact_id, detail)
        return False, detail
    try:
        r = prov._session.post(
            prov.APOLLO_BASE + "/emailer_campaigns/{}/add_contact_ids".format(sequence_id),
            headers=prov._apollo_headers(),
            json={"contact_ids": [apollo_contact_id],
                  "emailer_campaign_id": sequence_id,
                  "send_email_from_email_account_id": send_from}, timeout=prov.TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        detail = "request failed: {}".format(exc)
        prov._provider_error(None, "apollo/add-to-sequence", apollo_contact_id, detail)
        return False, detail
    if r.status_code >= 400 or data.get("error"):
        detail = "HTTP {} -- Apollo response body: {}".format(
            r.status_code, _apollo_body_excerpt(data, getattr(r, "text", "")))
        prov._provider_error(None, "apollo/add-to-sequence", apollo_contact_id, detail)
        return False, detail
    # Apollo can return HTTP 200 while silently declining to add the contact -- a recent job
    # change, an unverified email, or another per-contact safety block -- and the response body
    # doesn't reliably surface this. Found live, 2026-09-18: Owen Pratt's add returned a
    # clean HTTP 200, but his contact record's contact_campaign_statuses stayed empty (Apollo had
    # just detected his same-day job change to a new employer). The only trustworthy confirmation
    # is to re-fetch the contact and check it actually landed in the sequence.
    refreshed = _apollo_find_contact_by_email(email)
    statuses = (refreshed or {}).get("contact_campaign_statuses") or []
    confirmed = any(s.get("emailer_campaign_id") == sequence_id for s in statuses)
    if not confirmed:
        # Phase 20: the body is now carried out with the failure. This is the case that hit all
        # four of the 2026-10-02 failures, and with the body discarded the only available
        # explanation was a guess about per-contact safety blocks.
        detail = ("Apollo accepted the request (HTTP {}) but the contact was not actually added to "
                  "the sequence -- a per-contact safety block (deliverability verdict, recent job "
                  "change, etc.), not an API failure. Apollo response body: {}").format(
                      r.status_code, _apollo_body_excerpt(data, getattr(r, "text", "")))
        prov._provider_error(None, "apollo/add-to-sequence", apollo_contact_id, detail)
        return False, detail
    return True, None


def _dry_run_plan(contact: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> List[str]:
    """The writes a real run would make for this contact, in order, as printable lines. Checks the
    send account (a read) so a dry run surfaces the one failure it can predict without writing."""
    plan = []
    if existing and existing.get("id"):
        plan.append("reuse Apollo contact {}".format(existing["id"]))
    else:
        plan.append("create Apollo contact for {} ({})".format(
            contact.get("name") or "?", contact["email"]))
    plan.append("write Ghost Email subject + emails 1-3 to the Apollo contact's custom fields")
    send_from = _apollo_default_send_account_id()
    if send_from:
        plan.append("add to Sales Outreach sequence {} sending from account {}".format(
            SALES_OUTREACH_SEQUENCE_ID, send_from))
    else:
        plan.append("add to Sales Outreach sequence {} -- WOULD FAIL: no linked Apollo email "
                    "account to send from".format(SALES_OUTREACH_SEQUENCE_ID))
    plan.append("update Fibery contact {}: Sequence Status = Sequenced, Last Sequenced Date = {}"
                .format(contact["id"], datetime.now().strftime("%Y-%m-%d")))
    plan.append("count this company against today's enroll company cap")
    return plan


def enroll_contact(contact: Dict[str, Any], company_name: str, stated_goal: Optional[str],
                   fibery_token: Optional[str], dry_run: bool,
                   apollo_contact: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Runs the full per-contact flow: generate -> validate -> (dry-run stops here) -> find/create
    Apollo contact -> write Ghost fields -> add to Sales Outreach -> write Fibery Sequence
    Status/Last Sequenced Date back. Returns a result dict for the run summary; never raises --
    every failure mode is caught, logged into the result, and the caller moves to the next
    contact (same best-effort discipline as main.py's own Fibery write-back)."""
    result: Dict[str, Any] = {"email": contact["email"], "company": company_name,
                              "status": None, "detail": None, "ghost": None}
    ghost = generate_ghost_emails(contact, company_name, stated_goal)
    if not ghost:
        result["status"] = "skipped"
        result["detail"] = "generation failed validation or persona has no live play"
        return result
    result["ghost"] = ghost

    # Phase 18: main() already fetched this record for the pre-check guards, so reuse it rather
    # than paying for a second identical /contacts/search. Falls back to fetching when called
    # without one (the existing tests, and any direct caller). A read, so it runs on a dry run too.
    existing = (apollo_contact if apollo_contact is not None
                else _apollo_find_contact_by_email(contact["email"]))
    if existing and _apollo_already_in_sequence(existing, SALES_OUTREACH_SEQUENCE_ID):
        result["status"] = "skipped"
        result["detail"] = "already enrolled/finished in Sales Outreach"
        return result

    if dry_run:
        # Every read-only stage above has run for real; from here on each step is a write, so
        # describe it instead of doing it.
        result["status"] = "dry-run"
        result["detail"] = "no Apollo enrollment, no Fibery writes"
        result["planned"] = _dry_run_plan(contact, existing)
        return result

    apollo_id = existing.get("id") if existing else None
    if not apollo_id:
        apollo_id = _apollo_create_contact(contact, company_name)
    if not apollo_id:
        result["status"] = "failed"
        result["detail"] = "could not find or create Apollo contact"
        return result

    if not _apollo_update_ghost_fields(apollo_id, ghost):
        result["status"] = "failed"
        result["detail"] = "Apollo custom-field write failed"
        return result

    added, add_detail = _apollo_add_to_sequence(apollo_id, SALES_OUTREACH_SEQUENCE_ID,
                                                contact["email"])
    if not added:
        result["status"] = "failed"
        result["detail"] = ("Apollo sequence-add failed (Ghost fields were already written): {}"
                           .format(add_detail or "no detail returned"))
        _log_enroll_failure(contact["email"], company_name, result["detail"])
        return result

    if fibery_token:
        try:
            fc.update(fibery_token, "GTM Pipeline/Contacts", contact["id"], {
                "GTM Pipeline/Sequence Status": {"fibery/id": fc.SEQUENCE_STATUS["Sequenced"]},
                "GTM Pipeline/Last Sequenced Date": datetime.now().strftime("%Y-%m-%d"),
            })
        except RuntimeError as e:
            result["status"] = "enrolled-fibery-write-failed"
            result["detail"] = str(e)
            return result

    result["status"] = "enrolled"
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Run every stage (Fibery/Apollo reads, guards, copy generation) but skip "
                         "Apollo enrollment and all Fibery writes; print what would have happened.")
    ap.add_argument("--redact", action="store_true",
                    help="Mask contact names and emails in terminal output (company names stay "
                         "visible). Does not change what is written anywhere.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap this one invocation to N contacts (testing only -- the cron "
                         "invocation never passes this; no cap applies by default).")
    args = ap.parse_args(argv)

    if not args.redact:
        return _run(args)
    real_stdout = sys.stdout
    sys.stdout = RedactingStream(real_stdout, Redactor())
    try:
        return _run(args)
    finally:
        sys.stdout = real_stdout


def _run(args) -> int:
    redactor = getattr(sys.stdout, "redactor", None)
    cap = args.limit  # None => contacts[:None] below enrolls every eligible contact, uncapped
    if args.dry_run:
        print("DRY RUN -- no Apollo enrollment, no Fibery writes. Reads and copy generation are real.")
    fibery_token = fc.get_token()
    if not fibery_token:
        print("FIBERY_API_TOKEN missing -- cannot load eligible contacts. Exiting.")
        return 1

    contacts = fc.load_enrollable_contacts(fibery_token)
    companies = fc.load_company_context_map(fibery_token)
    # Phase 17: the whole Contacts table, grouped by company, so the per-contact guards below can
    # see siblings that load_enrollable_contacts() filters out by design -- an already-"Sequenced"
    # duplicate of this person is precisely the row that function excludes.
    all_contacts = fc.load_all_contacts(fibery_token)
    contacts_by_company: Dict[str, List[Dict[str, Any]]] = {}
    for existing in all_contacts:
        if existing["company_id"]:
            contacts_by_company.setdefault(existing["company_id"], []).append(existing)
    if redactor is not None:
        # Company names first, so a name token that is also a company word is never masked alone.
        redactor.protect(c.get("name") or "" for c in companies.values())
        redactor.add_names(c.get("name") or "" for c in list(all_contacts) + list(contacts))
    print("{} contact(s) eligible for enrollment (cap this run: {})".format(
        len(contacts), cap if cap is not None else "none"))

    today_str = datetime.now().strftime("%Y-%m-%d")
    if not args.dry_run:
        print("Daily enroll company cap: {} distinct companies/day ({} already started today)"
              .format(DAILY_ENROLL_COMPANY_CAP, cache.get_enrolled_company_count(today_str)))

    results = []
    for contact in contacts[:cap]:
        company = companies.get(contact["company_id"], {})
        # Phase 18: checked before the per-contact guards and before any LLM call, since a
        # capped-out company costs nothing to decline. Exempt on a dry run, which enrolls
        # nothing and so cannot consume the day's reach (same exemption main.py's own daily
        # company cap has).
        if not args.dry_run and company_cap_reached(contact["company_id"], today_str):
            detail = ("daily enroll company cap reached ({} distinct companies today) -- not "
                      "starting a new company".format(DAILY_ENROLL_COMPANY_CAP))
            print("  {} @ {} -> skipped-cap ({})".format(
                contact["email"], company.get("name") or "?", detail))
            results.append({"email": contact["email"], "company": company.get("name") or "",
                            "status": "skipped-cap", "detail": detail, "ghost": None})
            continue
        skip_reason = contact_skip_reason(
            contact, company, contacts_by_company.get(contact["company_id"], []))
        if skip_reason:
            print("  {} @ {} -> skipped-guard ({})".format(
                contact["email"], company.get("name") or "?", skip_reason))
            results.append({"email": contact["email"], "company": company.get("name") or "",
                            "status": "skipped-guard", "detail": skip_reason, "ghost": None})
            continue

        # Phase 18: look the contact up in Apollo BEFORE generating copy, so a mailbox-synced or
        # recently-active person costs no LLM call. A read-only /contacts/search, so it runs on a
        # dry run too -- a dry run that didn't report these skips would misrepresent what a real
        # run would do. The record is handed to enroll_contact so it isn't fetched twice.
        apollo_contact = _apollo_find_contact_by_email(contact["email"])
        apollo_skip = apollo_precheck_skip_reason(apollo_contact, today_str)
        if apollo_skip:
            print("  {} @ {} -> skipped-guard ({})".format(
                contact["email"], company.get("name") or "?", apollo_skip))
            results.append({"email": contact["email"], "company": company.get("name") or "",
                            "status": "skipped-guard", "detail": apollo_skip, "ghost": None})
            continue

        r = enroll_contact(contact, company.get("name") or "", company.get("stated_goal"),
                           fibery_token, args.dry_run, apollo_contact=apollo_contact)
        results.append(r)
        # Phase 18: the company counts against the day only once a contact there actually
        # enrolled. A company whose every contact was skipped or failed never consumed a slot,
        # so tomorrow is not the first time it gets another chance -- later today is.
        if not args.dry_run and r["status"] in ("enrolled", "enrolled-fibery-write-failed"):
            cache.record_enrolled_company(today_str, contact["company_id"])
        print("  {} @ {} -> {} ({})".format(contact["email"], company.get("name") or "?",
                                            r["status"], r["detail"] or ""))
        for step in r.get("planned") or []:
            print("    would: {}".format(step))
        if r["ghost"] and (args.dry_run or r["status"] == "enrolled"):
            print("    subject: {}".format(r["ghost"]["subject"]))
            print("    email 1: {}".format(r["ghost"]["email_1"]))
            print("    email 2: {}".format(r["ghost"]["email_2"]))
            print("    email 3: {}".format(r["ghost"]["email_3"]))

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nSummary: {}".format(counts) if results else "\nNo eligible contacts this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
