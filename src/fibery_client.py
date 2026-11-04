"""
fibery_client.py — shared Fibery REST client for the nycbound-gtm scripts.

Talks to Fibery directly over its REST command API (no MCP, no Claude in the loop), so every script
that imports this module is deterministic and can run standalone via cron/CLI. FIBERY_API_TOKEN
lives in .env — call `from dotenv import load_dotenv; load_dotenv()` before get_token(), same as
every script already does.

Endpoint/payload shapes were verified live against the real workspace: signal_ingest.py (Phase 4)
verified query/create this way; Phase 9 verified update/delete the same way — created a throwaway
Company + linked Contact, updated a primitive field, a date field, and an enum field (each of the
three payload shapes below), read them back to confirm, then deleted both rows — before any of this
was written into the file.

Extracted out of signal_ingest.py (Phase 4) so paradigm_import.py, sync_watchlist.py and main.py
(Phase 9) aren't each copy-pasting Fibery auth/request code a third and fourth time.
signal_ingest.py now imports this module — its own behavior is unchanged.

Also centralizes the enum id maps every script needs (from mcp__Fibery__schema_detailed,
2026-09-16) in one place, so scripts compare against names/ids defined here rather than a hardcoded
literal buried in each file.
"""
import json
import sys
import os
import urllib.error
import urllib.request
import time
from typing import Optional

FIBERY_WORKSPACE = "pinpointe"
FIBERY_URL = f"https://{FIBERY_WORKSPACE}.fibery.io/api/commands"

# ---------------------------------------------------------------------------
# Enum id maps (name -> fibery/id). Re-pull via schema_detailed and update here if a value is ever
# renamed/added in Fibery — nothing outside this file should hardcode an enum id.

RESEARCH_STATUS = {  # GTM Pipeline/Research Status_GTM Pipeline/Companies
    "Needs Manual Review": "02db90e2-8feb-4edd-8617-01d2767f2cf8",
    "Disqualified": "170670f3-1046-4fc2-9e69-e5975f027db6",
    "Researching": "2b82496b-0699-4fb1-a020-e9fd749e8207",
    "Qualified": "de597964-ca8a-424d-b801-bdd95c62c8bb",
    "New": "fb677fd8-506b-4a87-b9f8-300d95f4c83c",
    # Added 2026-09-22 (Phase 14) -- honest "HyperSignal hasn't reached this company yet"
    # state, replacing the false Disqualified/Needs Manual Review writes that used to
    # happen when main.py's live-discovery fallback failed on a company simply not yet
    # covered by HyperSignal's rotation. See
    # claude/nycbound-gtm-phase14-tiered-chunking-scope.md (Sales & Marketing Project).
    "Pending Not Yet Scanned": "dc0de9cb-06f8-4030-8cdf-be7d104c003b",
}

PROMOTION_STATUS_COMPANIES = {  # GTM Pipeline/Promotion Status_GTM Pipeline/Companies
    "Promoted to CRM": "33a7977f-fbcd-4c5b-9b25-a3b9c763d41d",
    "Not Promoted": "f95b2373-07a9-4400-ac64-e029502ab1b8",
}

SIGNAL_TIER = {  # GTM Pipeline/Signal Tier_GTM Pipeline/Companies
    # Renamed Noise -> Tier 3 and re-created 2026-09-17 (prioritization build) --
    # ids below are new; the field was empty in Fibery at the time (nothing wrote
    # to it before this), so nothing was orphaned by the id change.
    "Tier 1": "ce170f92-043c-43f8-91c6-fc890ee3d007",
    "Tier 2": "d4556bf3-e60b-43e1-87df-72960d310abb",
    "Tier 3": "b25020ee-e317-45ce-9486-01716af5564f",
}

NYC_REQUIRED = {  # GTM Pipeline/NYC Required_GTM Pipeline/Companies
    "Unclear": "26fd1ff1-949f-4a5c-bf68-7c99e8ea22b8",
    "No": "774b3704-5ad5-46a2-a245-d2b83e9fe962",
    "Yes": "b38f1d77-7cc1-47f7-88f7-911896900cb8",
}

ENRICHMENT_SOURCE = {  # GTM Pipeline/Enrichment Source_GTM Pipeline/Contacts
    "Manual": "0339415f-5249-4384-a905-3814d0c85044",
    "QuickEnrich": "3463bf69-b575-4694-a463-0c309b7968c1",
    "Apollo": "8dd4537f-c45f-4de3-a173-2a330a796832",
    "Hunter": "d49a6040-59fb-4ef4-ac20-b353aa7b73d1",
}

PERSONA = {  # GTM Pipeline/Persona_GTM Pipeline/Contacts
    "Global Mobility": "01f26c92-161b-4d7e-9b06-c4e75fe3206f",
    "CEO/Founder": "0daa81d3-2bb1-401b-b17c-93a8bff4a4b0",
    "Recruiter/TA": "b2ec8af1-9b54-4e3a-a3a8-ae1ee340f811",
    "People Ops": "c8163f5c-e39a-49f9-9d49-61bf7156c60e",
    "CFO/Finance": "d3b0c6b7-7bd0-4916-9a4b-05648bbb0cfb",
}

PROMOTION_STATUS_CONTACTS = {  # GTM Pipeline/Promotion Status_GTM Pipeline/Contacts
    "Not Promoted": "4a7b337c-2285-4163-ae10-1f5c54fb0fb3",
    "Promoted to CRM": "97c9a884-0e16-422b-87c5-ec7e002d2149",
    # Added 2026-09-17 (Phase 6) via mcp__Fibery__update_single_select_fields -- the trigger value the
    # "Promote to CRM" button sets; promote_to_crm.py polls for this and flips it to Promoted to CRM.
    "Promote Requested": "347c9edb-bc62-475e-b5ba-2baebed6ccd4",
}

# --- Added 2026-09-17 (Phase 6, promote_to_crm.py) ------------------------------------------------

CRM_ACCOUNT_SOURCE = {  # 1 - Pinpointe CRM/Source_Pinpointe CRM/Accounts
    "Inbound": "6ffe2ff0-82bb-11f0-817c-91fd370bf605",
    "Outbound": "73f38601-82bb-11f0-817c-91fd370bf605",
    "Referral": "774018f1-82bb-11f0-817c-91fd370bf605",
    "Event": "83569471-82bb-11f0-817c-91fd370bf605",
    "LinkedIn": "7867ea70-9cc2-11f0-8ff6-3d8810eaca33",
}

CRM_ACCOUNT_TYPE = {  # 1 - Pinpointe CRM/Type_Pinpointe CRM/Accounts
    "Corporate Client": "922eab90-909e-11f0-a187-4d027796546b",
    "Landlord": "970c8ce1-909e-11f0-a187-4d027796546b",
    "Other": "9db2d221-909e-11f0-a187-4d027796546b",
    "Service Partner": "df78b510-a572-11f0-b8a4-217f128e2b11",
    "Referral Partner": "ea036071-a572-11f0-b8a4-217f128e2b11",
}

CRM_CONTACT_SOURCE = {  # 1 - Pinpointe CRM/Source_Real Estate CRM/Contacts
    "LinkedIn": "05a0d880-7d23-11f0-9c06-6d27cf41b19d",
    "Inbound": "9d01e160-9cc2-11f0-8ff6-3d8810eaca33",
    "Outbound": "a27ee8e0-9cc2-11f0-8ff6-3d8810eaca33",
    "Event": "b373a750-bf52-11f0-8d06-0943a2b0809d",
    "Website Chat": "525226ae-8399-4800-bce4-84fb8ea511f4",
    "Renthop": "17411530-2d20-11f0-8827-ad398e4dc577",
    "StreetEasy": "1ec53751-2d20-11f0-8827-ad398e4dc577",
    "Gay Real Estate": "216361d1-2d20-11f0-8827-ad398e4dc577",
    "Referral": "257085a1-2d20-11f0-8827-ad398e4dc577",
    "Website": "2c662360-2d20-11f0-8827-ad398e4dc577",
    "Instagram": "2df21a90-2d20-11f0-8827-ad398e4dc577",
    "Other": "34294a50-2d20-11f0-8827-ad398e4dc577",
    "SilkWay": "cbc81100-3d80-11f0-849f-63681bdc6e92",
}

CRM_CONTACT_TYPE = {  # 1 - Pinpointe CRM/Type_Real Estate CRM/Contacts
    "Corporate Client": "3a31eb50-7c85-11f0-8244-8fc67a04e2ae",
    "Investor": "ff3eed00-82b9-11f0-817c-91fd370bf605",
    "Service Partner": "5cf832f0-a572-11f0-b8a4-217f128e2b11",
    "Referral Partner": "6ade3e50-a572-11f0-b8a4-217f128e2b11",
    "Renter": "da899b20-2d20-11f0-8827-ad398e4dc577",
    "Buyer": "e18fb3a1-2d20-11f0-8827-ad398e4dc577",
    "Seller": "e265a140-2d20-11f0-8827-ad398e4dc577",
    "Landlord": "e34579f0-2d20-11f0-8827-ad398e4dc577",
}

# Note: 1 - Pinpointe CRM/Contacts' own workflow/state has no "Engaged" value (Partner, New Lead,
# Qualified Lead, Client, Meeting Scheduled, Contract Out, Lost) -- Rachel's call, 2026-09-17: never
# write it from promote_to_crm.py, so no enum map for it lives here.

SEQUENCE_STATUS = {  # GTM Pipeline/Sequence Status_GTM Pipeline/Contacts
    "Not Sequenced": "09d286b4-5e77-4b5c-99b7-59ef8795ed07",
    "Bounced": "b93f03af-cf7b-448d-bd62-3b7b1ef091a8",
    "Sequenced": "be4c5d50-cb3c-4470-8aee-75799d3db5c1",
    "Replied": "ce63ae1d-4c0e-4366-8c87-9925214dde3d",
    "Do Not Contact": "fc660429-4559-4c08-9d3f-ce73ad911154",
}

OUTREACH_STATUS = {  # GTM Pipeline/Outreach Status_GTM Pipeline/Contacts (added 2026-09-17, Phase 8)
    # Phase 8: the per-contact 60-day reconciliation flag -- distinct from Sequence Status, which
    # tracks send/reply state for a contact ALREADY being messaged. Outreach Status answers the
    # upstream question (is this contact even safe to message right now), set at discovery/write-
    # back time by main.py's reconciliation logic against Fibery + Apollo activity.
    "Cleared to proceed": "49bc68eb-4afd-4492-9c77-34a2602d643d",
    "Recently Contacted": "217b9792-6192-4b90-853d-71d31773dfeb",
}

CRM_ACCOUNT_STATE = {  # workflow/state_1 - Pinpointe CRM/Accounts
    "Dormant": "0427ae50-82bb-11f0-817c-91fd370bf605",
    "Lost": "089541a0-82bb-11f0-817c-91fd370bf605",
    "Prospect": "da5f4510-82ba-11f0-817c-91fd370bf605",
    "Engaged": "da5f4511-82ba-11f0-817c-91fd370bf605",
    "Customer/Partner": "da5f4512-82ba-11f0-817c-91fd370bf605",
    "Discovery Scheduled": "f2097780-82ba-11f0-817c-91fd370bf605",
    "Proposal/Pilot Sent": "f8cfeef0-82ba-11f0-817c-91fd370bf605",
    "Discovery Completed": "e6713390-8db9-11f0-b548-799032e3b50a",
    "Contract Sent": "f944b0a0-8db9-11f0-b548-799032e3b50a",
}

# CRM Account states that count as "past Prospect" for the paradigm_import.py cross-check (Rachel:
# "Skip only past Prospect" — a bare Prospect account is fine to still surface as a fresh company).
CRM_STATES_PAST_PROSPECT = {
    name for name in CRM_ACCOUNT_STATE if name != "Prospect"
}


def normalize_domain(d: str) -> str:
    d = (d or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def get_token() -> Optional[str]:
    return os.environ.get("FIBERY_API_TOKEN")


def call(token: str, body: list, max_retries: int = 3, retry_delay: float = 5.0) -> tuple:
    req = urllib.request.Request(
        FIBERY_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
    )
    last_exc = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # A real HTTP response with an error status -- not transient, don't retry.
            return e.code, e.read().decode()
        except urllib.error.URLError as e:
            # Transient network failure (DNS hiccup, connection refused, timeout) rather than a
            # real HTTP error response -- HTTPError above is actually a URLError subclass, so
            # it's already handled by the time we get here. Added 2026-09-22: a DNS resolution
            # failure here (urllib.error.URLError, previously uncaught) propagated straight past
            # every `except RuntimeError` guard in main.py's run() loop and killed an 18+ hour
            # run at company 873/1446, taking the not-yet-reached Apollo enrollment step down
            # with it even though Apollo had nothing to do with the failure. Retry a few times
            # with a short delay -- these blips are usually gone within seconds -- before
            # finally raising as a normal RuntimeError, which every caller already handles.
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    raise RuntimeError("Fibery request failed after {} attempts (network error): {}".format(
        max_retries, last_exc))


_QUERY_PAGE_SIZE = 1000  # Fibery's own per-request cap


def query(token: str, from_db: str, select: dict, where=None, limit: Optional[int] = None) -> list:
    """Runs a fibery.entity/query command. Returns the result rows. Raises RuntimeError on failure
    rather than returning a partial/garbage result silently.

    limit=None (the default) means "give me everything" -- paginates internally in
    _QUERY_PAGE_SIZE-row pages via q/offset until a page comes back short. This matters: every
    caller in this file (load_company_map, CRM/contact loaders, promote_to_crm.py) wants the WHOLE
    table for dedupe/lookup purposes, not an arbitrary first slice. Before 2026-09-18 this used a
    bare q/limit=1000 with no pagination, so once GTM Pipeline/Companies passed ~1000 rows (during
    the Paradigm bulk import) load_company_map() silently returned only a partial view -- dedupe
    couldn't see everything already in Fibery and created ~27 duplicate company records before this
    was caught and fixed. Pass an explicit limit only if you actually want a bounded/first-N read
    (no current caller does)."""
    if limit is not None:
        q = {"q/from": from_db, "q/select": select, "q/limit": limit}
        if where is not None:
            q["q/where"] = where
        status, res = call(token, [{"command": "fibery.entity/query", "args": {"query": q, "params": {}}}])
        if status != 200 or not res[0].get("success"):
            raise RuntimeError(f"Fibery query on {from_db} failed ({status}): {res}")
        return res[0]["result"]

    all_rows = []
    offset = 0
    while True:
        q = {"q/from": from_db, "q/select": select, "q/limit": _QUERY_PAGE_SIZE, "q/offset": offset}
        if where is not None:
            q["q/where"] = where
        status, res = call(token, [{"command": "fibery.entity/query", "args": {"query": q, "params": {}}}])
        if status != 200 or not res[0].get("success"):
            raise RuntimeError(f"Fibery query on {from_db} failed ({status}): {res}")
        page = res[0]["result"]
        all_rows.extend(page)
        if len(page) < _QUERY_PAGE_SIZE:
            break
        offset += _QUERY_PAGE_SIZE
    return all_rows


def create(token: str, entity_type: str, entity: dict) -> dict:
    """Creates one entity. `entity` values for single-select/link fields must be
    {"fibery/id": <target id>}, plain values for primitive fields. Returns the created entity dict
    (includes fibery/id) on success, raises RuntimeError on failure."""
    status, res = call(token, [{"command": "fibery.entity/create",
                                 "args": {"type": entity_type, "entity": entity}}])
    if status != 200 or not res[0].get("success"):
        raise RuntimeError(f"Fibery create on {entity_type} failed ({status}): {res}")
    return res[0]["result"]


def update(token: str, entity_type: str, entity_id: str, fields: dict) -> dict:
    """Updates one existing entity by fibery/id with the given field values (same value shapes as
    create — enum/link fields take {"fibery/id": ...}). Returns the updated entity dict on success,
    raises RuntimeError on failure."""
    entity = dict(fields)
    entity["fibery/id"] = entity_id
    status, res = call(token, [{"command": "fibery.entity/update",
                                 "args": {"type": entity_type, "entity": entity}}])
    if status != 200 or not res[0].get("success"):
        raise RuntimeError(f"Fibery update on {entity_type} ({entity_id}) failed ({status}): {res}")
    return res[0]["result"]


def delete(token: str, entity_type: str, entity_id: str) -> None:
    """Deletes one entity by fibery/id. Only used for throwaway verification rows in this repo —
    nothing in the real pipelines deletes Fibery data. Raises RuntimeError on failure."""
    status, res = call(token, [{"command": "fibery.entity/delete",
                                 "args": {"type": entity_type, "entity": {"fibery/id": entity_id}}}])
    if status != 200 or not res[0].get("success"):
        raise RuntimeError(f"Fibery delete on {entity_type} ({entity_id}) failed ({status}): {res}")


# Phase 21 (2026-10-05): read-side guard against COMBINED company records -- one Fibery Company whose
# Name and/or Domain is several companies joined by commas ("Baselayer,Confido,Corridor,Cyera,Spott" /
# "baselayer.com,confidotech.com,..."). Nine were created 2026-09-22..10-01 by a scheduled job outside
# this repo. main.py researched the joined Domain string as if it were one domain and attached contacts
# from several real companies to one record. Intake is not changed here; every reader refuses them.
# A legitimate name may contain a comma before a legal suffix, so names are allow-listed explicitly.
COMMA_NAME_ALLOWLIST = {"parallel worlds, inc."}


def combined_company_reason(name: Optional[str], domain: Optional[str]) -> Optional[str]:
    """Why this Company looks like several companies joined into one record, or None if it doesn't.
    A comma in the Domain is never legitimate. A comma in the Name is, only for allow-listed names."""
    name = (name or "").strip()
    domain = (domain or "").strip()
    if "," in domain:
        return "Domain '{}' contains a comma -- a combined record, not one company".format(domain)
    if "," in name and name.lower() not in COMMA_NAME_ALLOWLIST:
        return "Name '{}' contains a comma -- a combined record, not one company".format(name)
    return None


def load_company_map(token: str) -> dict:
    """domain (normalized) -> {id, name, website, research_status, promotion_status}. Fetches
    Companies client-side and filters in Python — same fetch-all-then-filter approach
    signal_ingest.py established (the Companies table is small; the raw REST query's q/where didn't
    accept the filter shape the MCP query tool documents when this was first tried in Phase 4).
    main.py's --input fibery mode derives its company list from this same map (see
    read_company_list_from_fibery in main.py) rather than a second live query."""
    rows = query(token, "GTM Pipeline/Companies", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Domain": "GTM Pipeline/Domain",
        "Website": "GTM Pipeline/Website",
        "ResearchStatus": ["GTM Pipeline/Research Status", "enum/name"],
        "PromotionStatus": ["GTM Pipeline/Promotion Status", "enum/name"],
        # Added 2026-09-17 (signal-tier prioritization build): needed so
        # main.py.read_company_list_from_fibery() can sort the active list by
        # tier + staleness before the daily cap consumes it.
        "SignalTier": ["GTM Pipeline/Signal Tier", "enum/name"],
        "LastChecked": "GTM Pipeline/Last Checked Date",
    })
    out = {}
    for row in rows:
        combined = combined_company_reason(row.get("Name"), row.get("Domain"))
        if combined:
            print("[skip:combined-company] {} ({})".format(combined, row.get("id")), file=sys.stderr)
            continue
        dom = normalize_domain(row.get("Domain") or "")
        if dom:
            out[dom] = {
                "id": row["id"],
                "name": row["Name"],
                "website": row.get("Website"),
                "research_status": row.get("ResearchStatus"),
                "promotion_status": row.get("PromotionStatus"),
                "signal_tier": row.get("SignalTier"),
                "last_checked_date": row.get("LastChecked"),
            }
    return out


def load_company_name_index(token: str) -> dict:
    """lowercased/stripped Name -> {id, name, domain}, for EVERY company regardless of whether it
    has a Domain set. load_company_map() above is keyed by domain and silently drops any company
    with a blank Domain (many do -- Paradigm-import rows with an unresolved website, manually
    added companies, etc.) -- paradigm_import.py's needs_review name-collision safety net was built
    on top of load_company_map()'s output (`existing_names`, derived from a domain-keyed dict) and
    so could never see a blank-domain company as "already existing", regardless of query pagination.
    That gap let it re-create real companies under a second blank-domain record instead of flagging
    them for review, discovered/fixed 2026-09-18 during the Paradigm bulk import post-mortem
    (28 duplicate records total that day: ~27 from the query()-pagination bug fixed above, plus a
    further crop of blank-domain re-creates from this gap once that fix was verified). Two companies
    can legitimately share an exact name (rare, but seen: 'Wint' one review-flagged import row
    exists alongside a real different company) -- on a collision this keeps the first one seen."""
    rows = query(token, "GTM Pipeline/Companies", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Domain": "GTM Pipeline/Domain",
    })
    out = {}
    for row in rows:
        key = (row.get("Name") or "").strip().lower()
        if key and key not in out:
            out[key] = {"id": row["id"], "name": row["Name"], "domain": row.get("Domain")}
    return out


def is_active_company(company_row: dict) -> bool:
    """The one filter definition used everywhere a script needs to know whether a Fibery Company is
    still fair game for research/outreach: Research Status != Disqualified AND Promotion Status !=
    Promoted to CRM. `company_row` is one value from load_company_map()."""
    return (
        company_row.get("research_status") != "Disqualified"
        and company_row.get("promotion_status") != "Promoted to CRM"
    )


def load_crm_account_map(token: str) -> dict:
    """domain (normalized) -> {id, name, state}. Same fetch-all-then-filter approach as
    load_company_map — used by paradigm_import.py's CRM cross-check
    (1 - Pinpointe CRM/Accounts, not GTM Pipeline/Companies)."""
    rows = query(token, "1 - Pinpointe CRM/Accounts", {
        "id": "fibery/id",
        "Name": "1 - Pinpointe CRM/Name",
        "Website": "1 - Pinpointe CRM/Website",
        "State": ["workflow/state", "enum/name"],
    })
    out = {}
    for row in rows:
        dom = normalize_domain(row.get("Website") or "")
        if dom:
            out[dom] = {"id": row["id"], "name": row["Name"], "state": row.get("State")}
    return out


def load_crm_contact_map(token: str) -> dict:
    """email (lowercased) -> {id, name}. Added 2026-09-17 (Phase 6) alongside load_crm_account_map --
    same fetch-all-then-filter approach. Keyed on `1 - Pinpointe CRM/Email (lower)`, a formula field
    already built for exactly this kind of case-insensitive match, so no normalization needed here
    beyond a defensive .strip().lower() in case a row's formula hasn't recalculated yet."""
    rows = query(token, "1 - Pinpointe CRM/Contacts", {
        "id": "fibery/id",
        "Name": "1 - Pinpointe CRM/Name",
        "EmailLower": "1 - Pinpointe CRM/Email (lower)",
    })
    out = {}
    for row in rows:
        email = (row.get("EmailLower") or "").strip().lower()
        if email:
            out[email] = {"id": row["id"], "name": row["Name"]}
    return out


def load_recently_sequenced_company_ids(token: str, since_iso_date: str) -> set:
    """Set of GTM Pipeline/Companies ids that have at least one Contact with
    Last Sequenced Date >= since_iso_date. Fetch-all-then-filter (Contacts is a small table) — the
    60-day recent-outreach half of main.py's defense-in-depth gate."""
    rows = query(token, "GTM Pipeline/Contacts", {
        "CompanyId": ["GTM Pipeline/Contact Company", "fibery/id"],
        "LastSequenced": "GTM Pipeline/Last Sequenced Date",
    })
    out = set()
    for row in rows:
        last = row.get("LastSequenced")
        company_id = row.get("CompanyId")
        if last and company_id and last >= since_iso_date:
            out.add(company_id)
    return out



def load_contacts_by_company(token: str) -> dict:
    """GTM Pipeline/Companies id -> list of {id, name, email, persona,
    last_sequenced} for every existing Contact row. Phase 8: the Fibery
    half of the per-contact 60-day reconciliation in main.py -- loaded
    once per run and reused across every company, same fetch-all-then-
    filter approach as load_company_map/load_recently_sequenced_company_ids.
    Superseded load_recently_sequenced_company_ids for the enrichment
    gate (that function is kept, unused by main.py now, in case it's
    useful again -- the old company-level 60-day block it powered is
    replaced by this function's per-contact version)."""
    rows = query(token, "GTM Pipeline/Contacts", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Email": "GTM Pipeline/Email",
        "CompanyId": ["GTM Pipeline/Contact Company", "fibery/id"],
        "Persona": ["GTM Pipeline/Persona", "enum/name"],
        "LastSequenced": "GTM Pipeline/Last Sequenced Date",
    })
    out: dict = {}
    for row in rows:
        company_id = row.get("CompanyId")
        if not company_id:
            continue
        out.setdefault(company_id, []).append({
            "id": row["id"], "name": row.get("Name"), "email": row.get("Email"),
            "persona": row.get("Persona"), "last_sequenced": row.get("LastSequenced"),
        })
    return out


def load_company_context_map(token: str) -> dict:
    """GTM Pipeline/Companies id -> {name, website, stated_goal, signal_tier, nyc_required,
    headcount}. Added 2026-09-18 (Phase 12, apollo_enroll.py) -- load_company_map() above is
    domain-keyed and doesn't fetch Stated Goal (nothing needed it until now); this is a separate,
    id-keyed loader so apollo_enroll.py can pull a contact's Company context by the
    `GTM Pipeline/Contact Company` id already on the Contact row, without touching
    load_company_map()'s existing shape/callers (main.py's read_company_list_from_fibery depends
    on that shape exactly as it is)."""
    rows = query(token, "GTM Pipeline/Companies", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        # Added 2026-10-01 (Phase 17): apollo_enroll.py's pre-enrollment domain
        # guard needs the company's own Domain to check a contact's email
        # against. load_company_map() has it but is keyed by domain, which is
        # the wrong direction here -- this loader is keyed by company id, which
        # is what a Contact row actually carries.
        "Domain": "GTM Pipeline/Domain",
        "Website": "GTM Pipeline/Website",
        "StatedGoal": "GTM Pipeline/Stated Goal",
        "SignalTier": ["GTM Pipeline/Signal Tier", "enum/name"],
        "NYCRequired": ["GTM Pipeline/NYC Required", "enum/name"],
        "Headcount": "GTM Pipeline/Headcount",
    })
    out = {}
    for row in rows:
        out[row["id"]] = {
            "name": row.get("Name"),
            "domain": row.get("Domain"),
            "website": row.get("Website"),
            "stated_goal": row.get("StatedGoal"),
            "signal_tier": row.get("SignalTier"),
            "nyc_required": row.get("NYCRequired"),
            "headcount": row.get("Headcount"),
        }
    return out


def load_enrollable_contacts(token: str) -> list:
    """Every Contact row eligible for Phase 12's Apollo enrollment: Outreach Status == "Cleared
    to proceed" (the per-contact 60-day gate, Phase 8) AND Sequence Status == "Not Sequenced"
    (never previously enrolled by this pipeline) AND a real email AND a resolved Persona AND a
    non-blank Likely Priorities writeup (apollo_enroll.py's generation grounds itself in that
    field plus the Company's Stated Goal -- see claude/nycbound-gtm-phase12-apollo-hypersignal-
    scope.md, Sales & Marketing claude.ai Project). Fetch-all-then-filter, same approach as every
    other loader in this file -- the Contacts table is small.

    Returns a list of dicts: {id, name, title, email, linkedin, persona, likely_priorities,
    company_id}. Filtering happens in Python, not q/where, matching every other loader here."""
    rows = query(token, "GTM Pipeline/Contacts", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Title": "GTM Pipeline/Title",
        "Email": "GTM Pipeline/Email",
        "LinkedIn": "GTM Pipeline/LinkedIn Profile",
        "Persona": ["GTM Pipeline/Persona", "enum/name"],
        "LikelyPriorities": "GTM Pipeline/Likely Priorities",
        "OutreachStatus": ["GTM Pipeline/Outreach Status", "enum/name"],
        "SequenceStatus": ["GTM Pipeline/Sequence Status", "enum/name"],
        "CompanyId": ["GTM Pipeline/Contact Company", "fibery/id"],
        # Added 2026-10-02 (Phase 18): apollo_enroll.py refuses to cold-sequence anyone already
        # promoted to the CRM. promote_to_crm.py sets this link when Rachel promotes a contact
        # after a positive reply, so its presence means a real conversation is underway -- the
        # worst possible person to drop a cold sequence on.
        "CrmContactId": ["GTM Pipeline/CRM Contact", "fibery/id"],
    })
    out = []
    for row in rows:
        if row.get("OutreachStatus") != "Cleared to proceed":
            continue
        if row.get("SequenceStatus") != "Not Sequenced":
            continue
        if not (row.get("Email") or "").strip():
            continue
        if not row.get("Persona"):
            continue
        if not (row.get("LikelyPriorities") or "").strip():
            continue
        if not row.get("CompanyId"):
            continue
        out.append({
            "id": row["id"],
            "name": row.get("Name") or "",
            "title": row.get("Title") or "",
            "email": row["Email"].strip(),
            "linkedin": row.get("LinkedIn") or "",
            "persona": row["Persona"],
            "likely_priorities": row["LikelyPriorities"].strip(),
            "company_id": row["CompanyId"],
            "crm_contact_id": row.get("CrmContactId") or "",
        })
    return out


def load_all_contacts(token: str) -> list:
    """Every GTM Pipeline/Contacts row, flat, carrying the fields the Phase 17
    guards need: identity (name, email, LinkedIn), which Company it hangs off,
    and both status fields that mean "this person is already being messaged"
    (Sequence Status and Outreach Status).

    Deliberately a separate loader rather than a widening of
    load_contacts_by_company(): that function's {company_id -> [...]} shape and
    exact keys are what main.py's 60-day reconciliation reads, and it drops any
    row with no Contact Company set. The guards here need the whole table,
    company-less rows included -- a cross-record email check has to see every
    email that exists, wherever it hangs -- so widening it in place would mean
    changing behavior main.py depends on. Fetch-all-then-filter, same approach
    as every other loader in this file.

    Returns a list of dicts: {id, name, title, email, linkedin, company_id,
    persona, sequence_status, outreach_status, last_sequenced}.
    """
    rows = query(token, "GTM Pipeline/Contacts", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Title": "GTM Pipeline/Title",
        "Email": "GTM Pipeline/Email",
        "LinkedIn": "GTM Pipeline/LinkedIn Profile",
        "CompanyId": ["GTM Pipeline/Contact Company", "fibery/id"],
        "Persona": ["GTM Pipeline/Persona", "enum/name"],
        "SequenceStatus": ["GTM Pipeline/Sequence Status", "enum/name"],
        "OutreachStatus": ["GTM Pipeline/Outreach Status", "enum/name"],
        "LastSequenced": "GTM Pipeline/Last Sequenced Date",
    })
    return [{
        "id": row["id"],
        "name": row.get("Name") or "",
        "title": row.get("Title") or "",
        "email": (row.get("Email") or "").strip(),
        "linkedin": row.get("LinkedIn") or "",
        "company_id": row.get("CompanyId") or "",
        "persona": row.get("Persona") or "",
        "sequence_status": row.get("SequenceStatus") or "",
        "outreach_status": row.get("OutreachStatus") or "",
        "last_sequenced": row.get("LastSequenced") or "",
    } for row in rows]
