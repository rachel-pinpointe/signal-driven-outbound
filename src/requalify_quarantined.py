"""
requalify_quarantined.py -- Phase 21: make main.py re-qualify companies whose job data came (partly)
from a quarantined, wrong-company ATS board.

Quarantine (hypersignal/scripts/phase21-quarantine.mts) closes the wrong board's raw_jobs rows, but
main.py serves a company's research verdict from the company_research cache for up to 14 days without
re-reading signals.db. This deletes those cache rows for the affected domains, so the next main.py pass
recomputes qualification, Signal Tier and the driving role from what is left -- and writes the corrected
Research Status back to Fibery itself. Nothing else is touched: no Fibery write, no contact change, no
other cache table (contacts, stated goal) is cleared.

Run it only AFTER the quarantine has been applied to the live signals.db: clearing the cache first would
make main.py re-qualify from the very rows the quarantine is about to close.

    python -m src.requalify_quarantined                 # list what would be cleared (default, no writes)
    python -m src.requalify_quarantined --apply         # delete those company_research rows
    python -m src.requalify_quarantined --domains a.com,b.io [--apply]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

try:
    from . import cache
    from .fibery_client import normalize_domain
except ImportError:  # run as a plain script
    import cache  # type: ignore
    from fibery_client import normalize_domain  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = REPO_ROOT / "hypersignal" / "scripts" / "phase21-quarantine-seed.json"


def domains_from_seed(path: Path) -> List[str]:
    """The distinct domains in the seed's `quarantine` list. Overrides and UNSURE rows are not included:
    an override keeps its board, and an UNSURE board is deliberately left alone."""
    seed = json.loads(Path(path).read_text())
    out: List[str] = []
    for row in seed.get("quarantine") or []:
        d = normalize_domain(row.get("domain") or "")
        if d and d not in out:
            out.append(d)
    return out


def requalify(domains: List[str], apply: bool) -> List[dict]:
    """One entry per domain: whether a company_research row exists, its cached qualification, and
    whether it was deleted. Read-only unless apply=True."""
    conn = cache._connect()
    report = []
    try:
        for d in domains:
            row = conn.execute("SELECT payload, fetched_at FROM company_research WHERE domain = ?", (d,)).fetchone()
            entry = {"domain": d, "cached": bool(row), "qualification": None, "deleted": False}
            if row:
                try:
                    entry["qualification"] = json.loads(row[0]).get("qualification")
                except ValueError:
                    entry["qualification"] = "(unreadable payload)"
                if apply:
                    conn.execute("DELETE FROM company_research WHERE domain = ?", (d,))
                    entry["deleted"] = True
            report.append(entry)
        if apply:
            conn.commit()
    finally:
        conn.close()
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", default=str(DEFAULT_SEED), help="quarantine seed JSON (default: the Phase 21 seed)")
    ap.add_argument("--domains", default="", help="comma-separated domains instead of the seed")
    ap.add_argument("--apply", action="store_true", help="actually delete the cache rows (default: list only)")
    args = ap.parse_args(argv)
    domains = ([normalize_domain(d) for d in args.domains.split(",") if d.strip()]
               if args.domains else domains_from_seed(Path(args.seed)))
    report = requalify(domains, apply=args.apply)
    for e in report:
        state = "deleted" if e["deleted"] else ("would delete" if e["cached"] else "not cached")
        print("{:<28} {:<13} {}".format(e["domain"], state, e["qualification"] or ""))
    n = sum(1 for e in report if e["cached"])
    print("\n{} domain(s); {} cached research row(s) {}.".format(
        len(report), n, "deleted" if args.apply else "would be deleted (dry run -- pass --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
