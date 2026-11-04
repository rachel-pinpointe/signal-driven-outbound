"""Persona registry (Phase 8: all three personas live).

One entry per outbound persona: which persona module actually searches
for contacts (up to 3, ranked by seniority), and which play/messaging
(Layer 3, always last) it maps to. Layer 1 (company_research.py) never
touches this file -- company qualification is identical regardless of
which persona is asking; only contact-finding and messaging are
persona-specific.

Phase 8: all three personas are now live. Multi-persona contact
discovery (see claude/nycbound-gtm-multi-persona-contact-scope.md, Sales
& Marketing claude.ai Project) replaces the old CFO-only,
single-contact-per-company flow. find_contacts (plural -- used to be
find_contact, singular) now returns an envelope of up to 3 ranked
contacts per company, not one; see
providers.find_persona_contacts_generic()'s docstring for the exact
shape.
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from . import persona_finance, persona_people_ops, persona_recruiter_ta


@dataclass(frozen=True)
class Persona:
    key: str
    label: str
    find_contacts: Callable  # (company, website, ledger, dry_run=False, no_cache=False) -> envelope dict
    play: Optional[str]
    status: str  # "live" or "placeholder"


PERSONAS: Dict[str, Persona] = {
    "cfo": Persona(
        key="cfo", label="CFO / Head of Finance",
        find_contacts=persona_finance.find_contacts,
        play="CFO/Finance - Arrival Ops",
        status="live",
    ),
    "hr_people_ops": Persona(
        key="hr_people_ops", label="HR / People Ops",
        find_contacts=persona_people_ops.find_contacts,
        play="People Ops - Arrival Ops",
        status="live",
    ),
    "recruiter_ta": Persona(
        key="recruiter_ta", label="Recruiter / Talent Acquisition",
        find_contacts=persona_recruiter_ta.find_contacts,
        play="Recruiters/TA - Arrival Ops",
        status="live",
    ),
}


def get_persona(key: str) -> Persona:
    if key not in PERSONAS:
        raise KeyError(
            "Unknown persona '{}'. Known personas: {}".format(
                key, ", ".join(sorted(PERSONAS)))
        )
    return PERSONAS[key]
