"""Recruiter / Talent Acquisition persona config + thin wrapper (Phase 8).

Structurally different from Finance and People Ops: per the Persona
Profile PDF ("Persona Profile  RecruitersTalent Partners.pdf"), the
persona IS the individual-contributor recruiter/talent partner, not just
leadership -- so unlike the other two personas' tier ladders, tiers 4-6
here are real, valid targets on their own, not just a fallback when no
one senior exists.

Title tier ladder below is NEW, drafted for this build -- no source
brief exists (unlike Finance's cfo-lead-enrichment-brief.md). Grounded
in the Persona Profile PDF plus the two real examples already surfaced
in this repo's signal research (two recruiting leaders at one prospect
company, titled "Head of Talent" and "Talent Acquisition Lead") and standard recruiting title conventions. NOT yet
reviewed or confirmed by Rachel -- flag this for her review; treat the
exact wording/ranking as a first draft, not a locked spec.
"""

import re
from typing import Any, Dict, Optional

from .providers import CreditLedger, classify_by_tiers, find_persona_contacts_generic

PERSONA_KEY = "recruiter_ta"

TITLE_QUERY = [
    "Head of Talent", "Head of Talent Acquisition", "Chief Talent Officer",
    "VP Talent Acquisition", "VP of Talent Acquisition", "VP Talent",
    "Director of Talent Acquisition", "Director of Recruiting",
    "Talent Acquisition Manager", "Recruiting Manager",
    "Senior Recruiter", "Senior Talent Partner", "Talent Acquisition Lead",
    "Recruiter", "Talent Partner", "Talent Acquisition Partner", "Technical Recruiter",
]

# Draft, unreviewed -- see module docstring. Rank 1 is most senior; tiers
# 4-6 are individual-contributor titles and are valid targets for this
# persona, not a fallback the way a bare "Finance" or "HR" title is for
# the other two personas.
TITLE_TIERS = [
    (1, "Head of Talent", re.compile(
        r"head of talent(?: acquisition)?|chief talent officer", re.I)),
    (2, "VP Talent Acquisition", re.compile(
        r"\b(?:vp|v\.p\.|vice president)(?:\s+of)?,?\s+talent(?: acquisition)?"
        r"|talent(?: acquisition)?,? (?:vp|vice president)", re.I)),
    (3, "Director of Talent Acquisition", re.compile(
        r"director(?:,)? (?:of )?(?:talent acquisition|recruiting|recruitment)"
        r"|talent acquisition director", re.I)),
    (4, "TA/Recruiting Manager", re.compile(
        r"(?:talent acquisition|recruiting) manager|manager,? (?:talent acquisition|recruiting)", re.I)),
    (5, "Senior Recruiter / TA Lead", re.compile(
        r"senior (?:recruiter|talent partner)|talent acquisition lead|lead recruiter", re.I)),
    (6, "Recruiter / Talent Partner", re.compile(
        r"\brecruiter\b|talent partner|talent acquisition partner|technical recruiter", re.I)),
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
