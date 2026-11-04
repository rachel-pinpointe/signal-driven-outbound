"""
signal_ingest.py — push new HyperSignal company signals into Fibery.

Part of Phase 4 (the signal watcher). Runs as the last step of hypersignal/run-cron.sh, after
`npm run cycle` (jobs) and scripts/run-signals-once.mts (company signals) have both run.

Talks to Fibery via src/fibery_client.py's shared REST client (no MCP, no Claude in the loop) so
this step is deterministic and doesn't depend on an LLM correctly following instructions every
single weekday. FIBERY_API_TOKEN lives in .env. This was the first script in the repo to talk to
Fibery directly — its query/create plumbing was extracted into fibery_client.py in Phase 9 so
paradigm_import.py, sync_watchlist.py and main.py could reuse it instead of copy-pasting it.

Reads new rows straight out of hypersignal/data/signals.db (SQLite) rather than via HyperSignal's
webhook delivery — a webhook needs something listening to receive the POST, and this whole pipeline
is a one-shot process that runs once a day and exits, so there's nothing to listen with. Reading the
DB after the run is simpler and just as complete: the CREATE TABLE signals in HyperSignal's own
src/db.ts includes a `delivered` flag for exactly this kind of consumer.

Usage: python3 src/signal_ingest.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from . import fibery_client as fc
except ImportError:  # running as a script (python3 src/signal_ingest.py), not as a package module
    import fibery_client as fc

REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DB = REPO_ROOT / "hypersignal" / "data" / "signals.db"
WATCHLIST_CLIENT = "nycbound-calibration"

# GTM Pipeline/Signal Type enum ids (from mcp__Fibery__schema_detailed on GTM Pipeline/Signals).
# Never rename/renumber — Signal Type_GTM Pipeline/Signals's values.id, not display order.
SIGNAL_TYPE_IDS = {
    "Funding": "2c986953-5724-4d84-ae83-a85de3da3ffd",
    "Hiring Velocity": "2cfdabbc-25c7-4269-a408-62797c777fed",
    "Tech Stack Change": "90bf7f55-f5c1-4160-8a04-efc256b54bc7",
    "Layoff or WARN": "97142054-ae8a-47ad-9aaa-1aca43bab2b8",
    "Other": "aa5535b5-742f-4a25-9ea5-65d32a029e65",
    "Job Posting": "aa59c737-b61c-497a-acb2-22c373fcc00b",
    "Meta Ads": "c3961c2c-bcc2-4fa4-83cb-9ead2a7f6d66",
    "News or GDELT": "c8b5fb9f-f623-4a9d-b22b-c296a2177e84",
}

# HyperSignal `type` (src/signals/taxonomy.ts, 27 types) -> Fibery GTM Pipeline/Signal Type label.
# Fibery's enum is coarser than HyperSignal's, so this collapses deliberately rather than leaving
# anything to fall through to a default that might hide a new/unmapped type silently.
TYPE_MAP = {
    "velocity-spike": "Hiring Velocity",
    "velocity-location": "Hiring Velocity",
    "velocity-repost": "Hiring Velocity",
    "dns-mx-changed": "Tech Stack Change",
    "dns-tool-added": "Tech Stack Change",
    "dns-tool-removed": "Tech Stack Change",
    "ats-switched": "Tech Stack Change",
    "tech-added": "Tech Stack Change",
    "tech-removed": "Tech Stack Change",
    "subdomain-new": "Tech Stack Change",
    "funding-form-d": "Funding",
    "filing-8k": "Other",
    "filing-s1": "Funding",
    "ch-share-allotment": "Funding",
    "gdelt-funding": "Funding",
    "app-published": "Other",
    "app-stale": "Other",
    "news-leadership": "News or GDELT",
    "news-funding": "Funding",
    "news-layoffs": "Layoff or WARN",
    "news-expansion": "News or GDELT",
    "news-partnership": "News or GDELT",
    "news-launch": "News or GDELT",
    "warn-layoff": "Layoff or WARN",
    "ph-launch": "News or GDELT",
    "ads-started": "Meta Ads",
    "ads-stopped": "Meta Ads",
}


def main() -> int:
    token = os.environ.get("FIBERY_API_TOKEN")
    if not token:
        print("FIBERY_API_TOKEN not set in .env — nothing to do.", file=sys.stderr)
        return 1
    if not SIGNALS_DB.exists():
        print(f"No signals.db at {SIGNALS_DB} yet — has HyperSignal run at least once?", file=sys.stderr)
        return 1

    db = sqlite3.connect(SIGNALS_DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, dedup_key, domain, company, type, title, url, client, observed_at, created_at "
        "FROM signals WHERE delivered = 0 AND client = ? ORDER BY id",
        (WATCHLIST_CLIENT,),
    ).fetchall()

    if not rows:
        print("No new signals to push.")
        return 0

    companies = fc.load_company_map(token)

    created, skipped_no_company, skipped_unmapped_type, failed = 0, 0, 0, 0
    for row in rows:
        dom = fc.normalize_domain(row["domain"])
        company = companies.get(dom)
        if not company:
            print(f"[skip] no Fibery company matches domain '{dom}' (signal #{row['id']}, {row['type']})")
            skipped_no_company += 1
            continue

        fibery_type = TYPE_MAP.get(row["type"])
        if not fibery_type:
            print(f"[skip] unmapped HyperSignal type '{row['type']}' (signal #{row['id']}) — add it to TYPE_MAP")
            skipped_unmapped_type += 1
            continue

        detected = (row["observed_at"] or row["created_at"] or "")[:10]
        if not detected:
            detected = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        name = f"{row['title']} — {company['name']}"[:250]
        entity = {
            "GTM Pipeline/Name": name,
            "GTM Pipeline/Source": row["url"] or "hypersignal",
            "GTM Pipeline/Detected Date": detected,
            "GTM Pipeline/Signal Type": {"fibery/id": SIGNAL_TYPE_IDS[fibery_type]},
            "GTM Pipeline/Signal Company": {"fibery/id": company["id"]},
        }
        try:
            fc.create(token, "GTM Pipeline/Signals", entity)
        except RuntimeError as e:
            print(f"[fail] signal #{row['id']} ({row['type']} @ {dom}): {e}")
            failed += 1
        else:
            db.execute("UPDATE signals SET delivered = 1 WHERE id = ?", (row["id"],))
            db.commit()
            created += 1

    db.close()
    print(
        f"\n=== SIGNAL INGEST DONE === created={created} "
        f"skipped_no_company={skipped_no_company} skipped_unmapped_type={skipped_unmapped_type} failed={failed}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
