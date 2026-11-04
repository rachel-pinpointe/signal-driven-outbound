"""People Ops / HR persona config + thin wrapper (Phase 8).

Title tier ladder below is NEW, drafted for this build. Unlike Finance
(which had a real source brief, cfo-lead-enrichment-brief.md, with a
title priority Rachel had already reviewed and confirmed), no equivalent
brief exists for People Ops. This ladder is constructed from the Persona
Profile PDFs already in the Sales & Marketing claude.ai Project
("Persona Profile  People Ops Enterprise.pdf", "Persona Profile  People
Ops Startups.pdf" -- director/manager-level People Ops leadership at
both enterprise and startup company sizes) plus standard HR title
conventions. NOT yet reviewed or confirmed by Rachel -- flag this for
her review; treat the exact wording/ranking as a first draft, not a
locked spec the way Finance's is.
"""

import re
from typing import Any, Dict, Optional

from .providers import CreditLedger, classify_by_tiers, find_persona_contacts_generic

PERSONA_KEY = "hr_people_ops"

TITLE_QUERY = [
    "CPO", "Chief People Officer", "Chief HR Officer", "Chief Human Resources Officer",
    "Head of People", "Head of HR", "Head of People Operations",
    "VP People", "VP of People", "VP HR", "VP of HR", "VP Human Resources",
    "Director of People", "Director of People Operations", "Director of HR",
    "People Operations Manager", "HR Manager",
]

# Draft, unreviewed -- see module docstring. Rank 1 is most senior.
TITLE_TIERS = [
    (1, "CPO / CHRO", re.compile(
        r"\bcpo\b|chief people officer|chief hr officer|chief human resources officer", re.I)),
    (2, "Head of People/HR", re.compile(
        r"head of (?:global |corporate )?(?:people(?: operations)?|hr|human resources)", re.I)),
    (3, "VP People/HR", re.compile(
        r"\b(?:vp|v\.p\.|vice president)(?:\s+of)?,?\s+"
        r"(?:global\s+|corporate\s+)?(?:people(?: operations)?|hr|human resources)"
        r"|(?:people(?: operations)?|hr|human resources),? (?:vp|vice president)", re.I)),
    (4, "Director of People/HR", re.compile(
        r"director(?:,)? (?:of )?(?:people(?: operations)?|hr|human resources)"
        r"|people(?: operations)? director", re.I)),
    (5, "People Ops/HR Manager", re.compile(
        r"(?:people operations|people ops|hr|human resources) manager"
        r"|manager,? (?:people operations|hr)", re.I)),
    (6, "People Ops/HR", re.compile(
        r"^\s*(?:people operations|people ops|hr|human resources)\s*$", re.I)),
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
