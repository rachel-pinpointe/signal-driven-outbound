"""
promote_to_crm.py -- Phase 6: reply-gated CRM promotion.

Rachel clicks "Promote to CRM" on a GTM Pipeline/Contacts row once that contact sends a positive
reply (her judgment call, not automated -- Sequence Status=Replied means a reply happened, not that
it was positive). That native Fibery button just sets Promotion Status = Promote Requested; this
script is the actual check-then-create logic, meant to run on a schedule (hourly, 9am-6pm weekdays --
a separate Cowork scheduled task bound to Rachel's Mac, NOT part of hypersignal/run-cron.sh; every-
30-min was the original design but the scheduling platform enforces a 1-hour minimum interval,
discovered when wiring it up) rather than live inside a native Fibery automation, because
none of Fibery's native button actions support an upsert (Add always creates, Update only works if
something's already linked) and this project keeps conditional logic in tested, git-tracked Python
rather than opaque in-app scripts. Full design/rationale:
claude/nycbound-gtm-phase6-crm-promotion-scope.md on the Sales & Marketing claude.ai Project.

For each Contact with Promotion Status = Promote Requested:
  1. Company side: if the linked Company is already Promoted to CRM, reuse its existing CRM Account
     link -- no Accounts lookup, no risk of a duplicate Account. This also covers two contacts at the
     SAME never-before-promoted company both queued in the same run: the first one to process that
     company updates the in-memory company record immediately (not just Fibery), so the second one
     in the same run also takes the fast path instead of creating a second Account.
     Otherwise, domain-match against 1 - Pinpointe CRM/Accounts; link if found, create if not. Either
     way, flip the Company to Promoted to CRM, and if the resolved Account's workflow/state is
     Prospect, advance it to Engaged (a brand-new Account always clears this bar; anything already
     past Engaged, or Dormant/Lost, is left untouched -- Rachel's call, 2026-09-17).
  2. Contact side: email-match against 1 - Pinpointe CRM/Contacts (Email (lower)); link if found,
     create if not. workflow/state is never written on the CRM Contact either way -- there's no
     "Engaged" value there, and Rachel confirmed leaving it alone rather than substituting one.
  3. Set the GTM Pipeline Contact's own CRM Contact link and Promotion Status = Promoted to CRM.

A Contact with no linked Company, or no Email, is skipped with a loud warning and left at
Promote Requested (not silently reverted) so it stays visible for Rachel to fix by hand.

No compiled-history write into CRM Notes -- Document field, same REST-write limitation Phase 9 found
on Description, deliberately skipped this phase (see the scope doc).

Usage: python3 src/promote_to_crm.py [--dry-run]
"""
import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from . import fibery_client as fc
except ImportError:  # running as a script (python3 src/promote_to_crm.py), not as a package module
    import fibery_client as fc


def _load_companies_by_id(token: str) -> dict:
    """id -> {name, domain, website, promotion_status, crm_account_id}. Local to this script --
    unlike fibery_client.load_company_map (keyed by domain, for the paradigm-import dedupe use case),
    a Contact links to its Company by id, so this script needs id-keyed access instead."""
    rows = fc.query(token, "GTM Pipeline/Companies", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Domain": "GTM Pipeline/Domain",
        "Website": "GTM Pipeline/Website",
        "PromotionStatus": ["GTM Pipeline/Promotion Status", "enum/name"],
        "CrmAccountId": ["GTM Pipeline/CRM Account", "fibery/id"],
    })
    out = {}
    for row in rows:
        out[row["id"]] = {
            "id": row["id"],
            "name": row["Name"],
            "domain": fc.normalize_domain(row.get("Domain") or ""),
            "website": row.get("Website"),
            "promotion_status": row.get("PromotionStatus"),
            "crm_account_id": row.get("CrmAccountId"),
        }
    return out


def _load_contacts_pending_promotion(token: str) -> list:
    """All GTM Pipeline/Contacts rows with Promotion Status = Promote Requested. Fetch-all-then-filter
    in Python, same as every other fibery_client.py-adjacent query in this repo -- the raw REST
    query command's q/where didn't accept the filter shape the MCP query tool documents when this was
    first tried in Phase 4, and the Contacts table is small enough that this is a non-issue."""
    rows = fc.query(token, "GTM Pipeline/Contacts", {
        "id": "fibery/id",
        "Name": "GTM Pipeline/Name",
        "Email": "GTM Pipeline/Email",
        "Title": "GTM Pipeline/Title",
        "LinkedIn": "GTM Pipeline/LinkedIn Profile",
        "CompanyId": ["GTM Pipeline/Contact Company", "fibery/id"],
        "PromotionStatus": ["GTM Pipeline/Promotion Status", "enum/name"],
    })
    return [r for r in rows if r.get("PromotionStatus") == "Promote Requested"]


def resolve_crm_account(token: str, company: dict, crm_accounts_by_domain: dict, dry_run: bool):
    """Returns (account_id, created, state_changed). Mutates `company` in place (promotion_status,
    crm_account_id) so a second contact at the same company later in THIS SAME RUN takes the fast
    path instead of re-resolving/creating -- see module docstring."""
    if company["promotion_status"] == "Promoted to CRM" and company.get("crm_account_id"):
        account_id = company["crm_account_id"]
        created = False
        # Not necessarily in crm_accounts_by_domain (e.g. blank Website on the CRM Account) -- if we
        # can't determine its current state, err conservative and leave workflow/state untouched.
        current_state = None
        for row in crm_accounts_by_domain.values():
            if row["id"] == account_id:
                current_state = row["state"]
                break
    else:
        domain = company.get("domain") or ""
        match = crm_accounts_by_domain.get(domain) if domain else None
        if match:
            account_id, created, current_state = match["id"], False, match["state"]
        elif dry_run:
            account_id, created, current_state = "DRY-RUN-NEW-ACCOUNT", True, "Prospect"
        else:
            new_acct = fc.create(token, "1 - Pinpointe CRM/Accounts", {
                "1 - Pinpointe CRM/Name": company["name"],
                "1 - Pinpointe CRM/Website": company.get("website") or "",
                "1 - Pinpointe CRM/Source": {"fibery/id": fc.CRM_ACCOUNT_SOURCE["Outbound"]},
                "1 - Pinpointe CRM/Type": {"fibery/id": fc.CRM_ACCOUNT_TYPE["Corporate Client"]},
            })
            account_id, created, current_state = new_acct["fibery/id"], True, "Prospect"

        if not dry_run:
            fc.update(token, "GTM Pipeline/Companies", company["id"], {
                "GTM Pipeline/CRM Account": {"fibery/id": account_id},
                "GTM Pipeline/Promotion Status":
                    {"fibery/id": fc.PROMOTION_STATUS_COMPANIES["Promoted to CRM"]},
            })
        company["promotion_status"] = "Promoted to CRM"
        company["crm_account_id"] = account_id

    state_changed = current_state == "Prospect"
    if state_changed and not dry_run:
        fc.update(token, "1 - Pinpointe CRM/Accounts", account_id, {
            "workflow/state": {"fibery/id": fc.CRM_ACCOUNT_STATE["Engaged"]},
        })
    return account_id, created, state_changed


def resolve_crm_contact(token: str, contact: dict, account_id: str, crm_contacts_by_email: dict,
                         dry_run: bool):
    """Returns (contact_id, created). workflow/state is never written here -- see module docstring."""
    email = (contact.get("Email") or "").strip().lower()
    match = crm_contacts_by_email.get(email)
    if match:
        return match["id"], False
    if dry_run:
        return "DRY-RUN-NEW-CONTACT", True
    new_contact = fc.create(token, "1 - Pinpointe CRM/Contacts", {
        "1 - Pinpointe CRM/Name": contact["Name"],
        "1 - Pinpointe CRM/Email": contact.get("Email") or "",
        "1 - Pinpointe CRM/Job Title": contact.get("Title") or "",
        "1 - Pinpointe CRM/LinkedIn Profile": contact.get("LinkedIn") or "",
        "1 - Pinpointe CRM/Account": {"fibery/id": account_id},
        "1 - Pinpointe CRM/Source": {"fibery/id": fc.CRM_CONTACT_SOURCE["Outbound"]},
        "1 - Pinpointe CRM/Type": {"fibery/id": fc.CRM_CONTACT_TYPE["Corporate Client"]},
    })
    return new_contact["fibery/id"], True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen, write nothing")
    args = ap.parse_args()

    token = fc.get_token()
    if not token:
        print("FIBERY_API_TOKEN not set in .env -- nothing to do.", file=sys.stderr)
        return 1

    pending = _load_contacts_pending_promotion(token)
    if not pending:
        print("No contacts pending promotion.")
        return 0

    print(f"{len(pending)} contact(s) pending promotion. Loading Companies/CRM maps...")
    companies = _load_companies_by_id(token)
    crm_accounts_by_domain = fc.load_crm_account_map(token)
    crm_contacts_by_email = fc.load_crm_contact_map(token)

    promoted = skipped_no_company = skipped_no_email = failed = 0

    for contact in pending:
        name = contact.get("Name") or "(unnamed)"
        company_id = contact.get("CompanyId")
        if not company_id or company_id not in companies:
            print(f"[skip:no_company] '{name}': no linked Company -- left at Promote Requested, "
                  f"needs manual fix")
            skipped_no_company += 1
            continue
        if not (contact.get("Email") or "").strip():
            print(f"[skip:no_email] '{name}': no Email on file -- left at Promote Requested, "
                  f"needs manual fix")
            skipped_no_email += 1
            continue

        company = companies[company_id]
        try:
            account_id, acct_created, state_changed = resolve_crm_account(
                token, company, crm_accounts_by_domain, args.dry_run)
            contact_id, contact_created = resolve_crm_contact(
                token, contact, account_id, crm_contacts_by_email, args.dry_run)

            if not args.dry_run:
                fc.update(token, "GTM Pipeline/Contacts", contact["id"], {
                    "GTM Pipeline/CRM Contact": {"fibery/id": contact_id},
                    "GTM Pipeline/Promotion Status":
                        {"fibery/id": fc.PROMOTION_STATUS_CONTACTS["Promoted to CRM"]},
                })
                # Reused for later contacts at the same company within this run, same reason as the
                # Company-side mutation in resolve_crm_account.
                crm_contacts_by_email.setdefault((contact.get("Email") or "").strip().lower(),
                                                  {"id": contact_id, "name": name})

            acct_verb = "created" if acct_created else "linked existing"
            contact_verb = "created" if contact_created else "linked existing"
            state_note = " [Account Prospect->Engaged]" if state_changed else ""
            verb = "would promote" if args.dry_run else "promoted"
            print(f"[{verb}] '{name}' @ '{company['name']}': "
                  f"Account {acct_verb}{state_note}, Contact {contact_verb}")
            promoted += 1
        except RuntimeError as e:
            print(f"[fail] '{name}': {e}")
            failed += 1

    print(
        f"\n=== PROMOTE_TO_CRM DONE ({'DRY RUN' if args.dry_run else 'LIVE'}) === "
        f"promoted={promoted} skipped_no_company={skipped_no_company} "
        f"skipped_no_email={skipped_no_email} failed={failed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
