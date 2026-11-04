"""
sync_fibery.py — Fibery Sales Intelligence cache sync

Refreshes the local `fibery_docs/` cache from the 7 Fibery Sales Intelligence docs listed
below. Run it at the start of every session, before any drafting work.

Usage (in Claude Code):
    Run sync_fibery.py to refresh the Fibery docs cache before anything else.

This script is designed to be run BY Claude Code (which has Fibery MCP access
via OAuth). It cannot be run directly from the terminal — it has no API keys.
Instead, paste the instructions below into Claude Code.
"""

# ============================================================
# CLAUDE CODE INSTRUCTIONS — paste this into Claude Code:
# ============================================================
#
# Use the Fibery MCP fetch_by_url tool to fetch each of the following
# Sales Intelligence docs and save the full markdown content to the
# corresponding file in fibery_docs/. Do NOT summarize or truncate.
# Overwrite the file if it already exists.
#
# 1. https://pinpointe.fibery.io/Sales_Intelligence/476
#    → fibery_docs/icp_and_qualification.md
#
# 2. https://pinpointe.fibery.io/Sales_Intelligence/477
#    → fibery_docs/buyer_personas.md
#
# 3. https://pinpointe.fibery.io/Sales_Intelligence/478
#    → fibery_docs/messaging_and_positioning.md
#
# 4. https://pinpointe.fibery.io/Sales_Intelligence/479
#    → fibery_docs/research_and_stats.md
#
# 5. https://pinpointe.fibery.io/Sales_Intelligence/480
#    → fibery_docs/email_plays.md
#
# 6. https://pinpointe.fibery.io/Sales_Intelligence/481
#    → fibery_docs/sales_call_patterns.md
#
# 7. https://pinpointe.fibery.io/Sales_Intelligence/487
#    → fibery_docs/case_studies.md
#
# After writing all 7 files, confirm each one with its character count.
# Done when all 7 files exist and are non-empty.
# ============================================================

DOCS = {
    "icp_and_qualification":     "https://pinpointe.fibery.io/Sales_Intelligence/476",
    "buyer_personas":            "https://pinpointe.fibery.io/Sales_Intelligence/477",
    "messaging_and_positioning": "https://pinpointe.fibery.io/Sales_Intelligence/478",
    "research_and_stats":        "https://pinpointe.fibery.io/Sales_Intelligence/479",
    "email_plays":               "https://pinpointe.fibery.io/Sales_Intelligence/480",
    "sales_call_patterns":       "https://pinpointe.fibery.io/Sales_Intelligence/481",
    "case_studies":              "https://pinpointe.fibery.io/Sales_Intelligence/487",
}

if __name__ == "__main__":
    print(__doc__)
    print("\nDoc URLs:")
    for name, url in DOCS.items():
        print(f"  {name}: {url}")
