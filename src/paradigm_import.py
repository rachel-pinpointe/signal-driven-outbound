"""
paradigm_import.py — Paradigm CSV export -> Fibery GTM Pipeline/Companies.

Part of Phase 9 (company intake + paid-gate + Fibery write-back). Manual/on-demand, not part of the
daily cron -- Rachel pulls Paradigm exports on her own schedule (all-at-once or in chunks), not
daily, so this matches how she actually gets the data rather than pretending it's a feed.

Two-stage dedupe, in order:
  1. CRM cross-check (1 - Pinpointe CRM/Accounts, by normalized domain). If the matched Account's
     workflow/state is anything past Prospect (Rachel: "Skip only past Prospect", 2026-09-16), the
     row is skipped -- this is a real relationship already, don't create a second cold-outreach
     company for a domain we've already engaged with, dormant with, or lost.
  2. GTM Pipeline/Companies dedupe, by normalized domain first (exact match -> skip, existing row is
     left alone -- this import only creates, never refreshes, per Rachel: "Leave it alone",
     2026-09-16). If no domain match, a case-insensitive exact Name match is flagged "possible
     duplicate, needs manual review" rather than silently created or merged -- the H1 h1.com/h1.co
     case from the real sample export is exactly this: dedupe-by-domain alone would have missed it.

Field mapping (full detail + rationale in claude/nycbound-gtm-phase9-company-intake-scope.md on the
Sales & Marketing claude.ai Project):
  Company Name           -> Name (falls back to a domain-derived name on blank)
  Website                 -> Website + Domain (normalized)
  Employee Count (Range)  -> Headcount (floor-of-midpoint int, e.g. "101-250" -> 175)
  Tier                     -> skipped entirely (Rachel: "Skip Tier on import", 2026-09-16)
  Hiring in NYC            -> NYC Required (Yes/No/Unclear -- only "Actively" seen in the real
                               sample, so anything else is flagged Unclear rather than guessed)
  Stage, Money Raised Total, NYC Office Address
                            -> NOT written to Fibery. The scope doc originally proposed folding
                               these into the Description field as plain text, but Description is a
                               Collaboration~Documents/Document field -- verified live (2026-09-16)
                               that Fibery's entity/create and entity/update commands reject a plain
                               string for it ("Entity reference should contain 'fibery/id' key");
                               writing document content requires Fibery's block-editing tools
                               (insert_document_blocks etc.), which only exist as Claude-facing MCP
                               tools, not a plain REST endpoint -- wiring this standalone script to
                               depend on an MCP/Claude session would break the "runs headless via
                               cron/CLI" design every other Phase 9 script follows. So: these three
                               fields are read from the CSV and printed in a per-row "not stored"
                               note (see FOLLOW_UP_FIELDS below) so the data isn't silently lost, but
                               nothing writes them into Fibery. Flagging for Rachel -- an easy later
                               fix if a real field (or a working document-write path) gets added.
  Status, Duplicate Company Name -> ignored (Paradigm's own internal workflow fields)
  Relocation assistance    -> not stored (Rachel: abandoned data point, 2026-09-16)
New imports always get Fibery's own default Research Status (New) and Promotion Status (Not
Promoted) -- untouched by this script.

Usage: python3 src/paradigm_import.py path/to/export.csv [--dry-run]
"""
import argparse
import csv
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from . import fibery_client as fc
except ImportError:  # running as a script (python3 src/paradigm_import.py), not as a package module
    import fibery_client as fc

NAME_HEADERS = ["company name", "company", "name"]
WEBSITE_HEADERS = ["website", "url", "domain", "site"]
HEADCOUNT_HEADERS = ["employee count (range)", "employee count range", "employee count", "headcount"]
NYC_HIRING_HEADERS = ["hiring in nyc", "hiring in nyc?"]
STAGE_HEADERS = ["stage"]
MONEY_RAISED_HEADERS = ["money raised total", "money raised", "total raised"]
NYC_ADDRESS_HEADERS = ["nyc office address", "office address"]
# Read for the follow-up note only -- never stored (see module docstring).
FOLLOW_UP_FIELDS = [("Stage", STAGE_HEADERS), ("Money raised", MONEY_RAISED_HEADERS),
                     ("NYC office", NYC_ADDRESS_HEADERS)]


def _get(row: dict, keys: list) -> str:
    """Case-insensitive header lookup, first match wins. Mirrors main.py's NAME_KEYS/URL_KEYS
    pattern -- Paradigm's own header casing/spacing has drifted between exports before."""
    lower_map = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
    for key in keys:
        if key in lower_map and lower_map[key].strip():
            return lower_map[key].strip()
    return ""


def _headcount_from_range(raw: str):
    """'101-250' -> 175 (floor of midpoint). Blank/unparseable -> None (leave Headcount null)."""
    if not raw:
        return None
    nums = re.findall(r"\d+", raw.replace(",", ""))
    if len(nums) >= 2:
        lo, hi = int(nums[0]), int(nums[1])
        return (lo + hi) // 2
    if len(nums) == 1:
        return int(nums[0])
    return None


def _name_from_domain(domain: str) -> str:
    stem = domain.split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()


def _nyc_required_value(raw: str) -> str:
    """Every row in the real sample export said 'Actively' -> Yes. Anything else is flagged, not
    guessed -- the mapping only has one real data point to go on (see scope doc)."""
    if not raw:
        return "Unclear"
    low = raw.strip().lower()
    if low in ("actively", "yes", "actively hiring"):
        return "Yes"
    if low in ("no", "not currently", "not hiring"):
        return "No"
    return "Unclear"


def _follow_up_note(row: dict) -> str:
    parts = []
    for label, headers in FOLLOW_UP_FIELDS:
        val = _get(row, headers)
        if val and val.strip().lower() != "from ava's list":
            parts.append(f"{label}={val}")
    return "; ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", help="Path to a Paradigm CSV export")
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen, create nothing")
    args = ap.parse_args()

    token = fc.get_token()
    if not token:
        print("FIBERY_API_TOKEN not set in .env -- nothing to do.", file=sys.stderr)
        return 1

    path = Path(args.csv_path)
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    with path.open(newline="", encoding="utf-8-sig") as f:
        raw_rows = list(csv.DictReader(f))

    if not raw_rows:
        print("CSV has no data rows.")
        return 0

    print(f"Read {len(raw_rows)} rows from {path.name}.")
    print("Loading Fibery Companies + CRM Accounts for dedupe...")
    companies = fc.load_company_map(token)  # domain -> {id, name, research_status, promotion_status}
    crm_accounts = fc.load_crm_account_map(token)  # domain -> {id, name, state}
    # Name-collision safety net (stage 2b below) needs EVERY company, including ones with a blank
    # Domain -- `companies` above is domain-keyed and drops those entirely, which let a blank-domain
    # company silently get re-created under a second record instead of being flagged for review.
    # Fixed 2026-09-18; see load_company_name_index()'s docstring in fibery_client.py.
    name_index = fc.load_company_name_index(token)
    existing_names = {name: (row["domain"], row) for name, row in name_index.items()}

    created, skipped_crm, skipped_dup_domain, skipped_needs_review, skipped_unusable = 0, 0, 0, 0, 0
    follow_ups = []

    for i, row in enumerate(raw_rows, start=2):  # row 1 is the header
        name = _get(row, NAME_HEADERS)
        website = _get(row, WEBSITE_HEADERS)
        domain = fc.normalize_domain(website)

        if not name and not domain:
            print(f"[skip:unusable] row {i}: blank name and blank/unusable website")
            skipped_unusable += 1
            continue
        if not name:
            name = _name_from_domain(domain)

        # Stage 1: CRM cross-check -- skip if the matched Account is past Prospect.
        if domain and domain in crm_accounts:
            acct = crm_accounts[domain]
            if acct["state"] in fc.CRM_STATES_PAST_PROSPECT:
                print(f"[skip:crm] row {i} '{name}' ({domain}): CRM Account '{acct['name']}' is "
                      f"already at '{acct['state']}' -- not a fresh company")
                skipped_crm += 1
                continue

        # Stage 2a: GTM Pipeline/Companies dedupe by domain -- existing row left alone.
        if domain and domain in companies:
            existing = companies[domain]
            print(f"[skip:duplicate] row {i} '{name}' ({domain}): already in Fibery as "
                  f"'{existing['name']}' -- left alone")
            skipped_dup_domain += 1
            continue

        # Stage 2b: no domain match -- case-insensitive exact Name match safety net (the H1 case).
        name_key = name.strip().lower()
        if name_key in existing_names:
            match_dom, match_row = existing_names[name_key]
            print(f"[skip:needs_review] row {i} '{name}' ({domain or 'no domain'}): name matches "
                  f"existing Fibery company '{match_row['name']}' at domain '{match_dom or 'none'}' "
                  f"but domains don't match -- possible duplicate, needs manual review, not "
                  f"auto-created")
            skipped_needs_review += 1
            continue

        headcount = _headcount_from_range(_get(row, HEADCOUNT_HEADERS))
        nyc_required = _nyc_required_value(_get(row, NYC_HIRING_HEADERS))
        note = _follow_up_note(row)

        entity = {
            "GTM Pipeline/Name": name,
            "GTM Pipeline/NYC Required": {"fibery/id": fc.NYC_REQUIRED[nyc_required]},
        }
        if website:
            entity["GTM Pipeline/Website"] = website
        if domain:
            entity["GTM Pipeline/Domain"] = domain
        if headcount is not None:
            entity["GTM Pipeline/Headcount"] = headcount

        if args.dry_run:
            print(f"[would create] row {i}: '{name}' ({domain or 'no domain'}) headcount={headcount} "
                  f"nyc_required={nyc_required}")
            new_id = None
        else:
            try:
                result = fc.create(token, "GTM Pipeline/Companies", entity)
            except RuntimeError as e:
                print(f"[fail] row {i} '{name}': {e}")
                continue
            new_id = result.get("fibery/id")
            print(f"[created] row {i}: '{name}' ({domain or 'no domain'}) -> {new_id}")

        created += 1
        # Register so later duplicate rows within THIS file are still caught, dry-run or not.
        stub = {"id": new_id, "name": name, "research_status": "New", "promotion_status": "Not Promoted"}
        if domain:
            companies[domain] = stub
        existing_names[name_key] = (domain, stub)
        if note:
            follow_ups.append(f"  {name} ({domain or 'no domain'}): {note}")

    label = "would create" if args.dry_run else "created"
    print(
        f"\n=== PARADIGM IMPORT DONE ({'DRY RUN' if args.dry_run else 'LIVE'}) === "
        f"{label}={created} skipped_crm={skipped_crm} skipped_duplicate={skipped_dup_domain} "
        f"skipped_needs_review={skipped_needs_review} skipped_unusable={skipped_unusable}"
    )
    if follow_ups:
        print(
            f"\n{len(follow_ups)} row(s) had Stage/Money Raised/NYC Office Address data that isn't "
            f"stored anywhere in Fibery (see module docstring) -- add manually if you want it kept:"
        )
        for line in follow_ups:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
