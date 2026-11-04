"""Finance / CFO persona config + thin wrapper (Phase 8).

Replaces cfo_enrich.py, deleted this phase. Title data below is
unchanged from cfo_enrich.py's original FINANCE_TITLE_QUERY/TITLE_TIERS
(the brief's title priority: CFO -> Head of Finance -> VP Finance ->
Finance Lead -> Finance -> Finance Manager, from cfo-lead-enrichment-
brief.md) -- only the orchestration moved, to
providers.find_persona_contacts_generic(), now shared by all 3 personas.
"""

import re
from typing import Any, Dict, Optional

from .providers import CreditLedger, classify_by_tiers, find_persona_contacts_generic

PERSONA_KEY = "cfo"

# Sent to QuickEnrich/web_research/Apollo as the search net -- deliberately
# wider than what qualifies, so ranking and rejection happen locally where
# they're auditable.
TITLE_QUERY = [
    "CFO", "Chief Financial Officer", "Head of Finance", "VP Finance",
    "VP of Finance", "Vice President of Finance", "Vice President Finance",
    "Finance Lead", "Head of Finance & Operations", "Head of Finance and Operations",
]

# Rank 1 is most senior. Only these six tiers qualify. Unchanged from the
# original brief -- see cfo-lead-enrichment-brief.md for the sourcing and
# git history on cfo_enrich.py (this file's predecessor) for the tier-by-
# tier rationale comments (the comma in tier 3, the anchoring on tier 5,
# etc.) that shaped these patterns.
TITLE_TIERS = [
    (1, "CFO", re.compile(r"\bcfo\b|chief financial officer", re.I)),
    (2, "Head of Finance", re.compile(r"head of (?:global |corporate |strategic )?finance", re.I)),
    (3, "VP Finance", re.compile(r"\b(?:vp|v\.p\.|vice president)(?:\s+of)?,?\s+"
                                 r"(?:global\s+|corporate\s+|strategic\s+)?finance"
                                 r"|finance,? (?:vp|vice president)", re.I)),
    (4, "Finance Lead", re.compile(r"finance lead|lead,? finance", re.I)),
    (5, "Finance", re.compile(r"^\s*finance\s*$", re.I)),
    (6, "Finance Manager", re.compile(r"finance manager|manager of finance"
                                      r"|manager,? finance", re.I)),
]


def classify_title(title: str) -> Optional[Dict[str, Any]]:
    return classify_by_tiers(title, TITLE_TIERS)


def find_contacts(company: str, website: str, ledger: CreditLedger,
                  dry_run: bool = False, no_cache: bool = False) -> Dict[str, Any]:
    """Cache-aware public entry point. Returns the multi-contact envelope
    -- see providers.find_persona_contacts_generic()'s docstring for the
    exact shape and cache-write discipline."""
    return find_persona_contacts_generic(
        PERSONA_KEY, company, website, TITLE_QUERY, classify_title, ledger,
        dry_run=dry_run, no_cache=no_cache, max_candidates=3)
