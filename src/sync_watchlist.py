"""
sync_watchlist.py — Fibery active companies -> HyperSignal watchlist YAML.

Part of Phase 9 (company intake + paid-gate + Fibery write-back). Inserted as the first step of
hypersignal/run-cron.sh, before `npm run cycle`, so the watchlist is current before the day's scan
starts -- Fibery is the source of truth for "which companies are we watching," not a hand-maintained
YAML list (Phase 1's original design intent, actually wired up here).

Regenerates ONLY the `search.orgDomains` list inside hypersignal/clients/nycbound-calibration.yaml
by locating and replacing that block as text -- not a full YAML dump/rewrite -- so everything else in
the file (name, kind, useAts, refreshCron, signals, comments) stays exactly as a human left it. The
calibration file's original hand-annotated "# Qualified"/"# Disqualified" comments were a one-time
label for the frozen 20-company calibration batch (Phase 4); this script's own domain lines instead
carry "# <Company Name> (<Research Status>)" so the list stays self-describing without depending on
those original hand-written labels.

Active = Research Status != Disqualified AND Promotion Status != Promoted to CRM (the same filter
fibery_client.is_active_company() defines for every script that needs it -- main.py's --input fibery
mode uses the identical rule).

Phase 14 (2026-09-22): the block above used to carry EVERY active company, every day -- no chunking,
no time limit. Real runs showed that's unworkable (job fetch and the company signal scan both take
hours against the full ~1,445-company list, throttled hard by hiring.cafe/GDELT). This script now
writes only a rotating SLICE each day, tracked in hypersignal/data/rotation-state.json, so each daily
run's actual scan work fits inside the time-boxed budgets run-cron.sh enforces around `npm run cycle`
and the signal-scan step. See claude/nycbound-gtm-phase14-tiered-chunking-scope.md (Sales & Marketing
claude.ai Project) for the full design and the numbers behind it.

Rotation design, in short:
  - Every active company is ranked by Signal Tier + staleness, the same priority order main.py's
    own _company_priority_key() already uses for its persona-enrichment pass (duplicated here in
    _priority_key() rather than imported, to avoid coupling this script's import graph to main.py's).
  - Tier 1 companies not yet attempted THIS rotation cycle are always included, uncapped.
  - The rest of the daily batch (ROTATION_BATCH_SIZE, default 200, env-overridable) is filled from
    the highest-priority not-yet-attempted companies in the remaining pool.
  - Each pool (Tier 1, and everyone else) wraps independently: once every company in a pool has
    been attempted this cycle, that pool's "attempted" set resets and it starts over. Tier 1 is a
    small pool, so in practice it wraps and re-includes itself often -- daily once its own backlog
    clears -- while the much larger non-Tier-1 pool takes several days per full pass.
  - "Attempted" is committed AFTER the job-fetch step actually runs (see --commit-rotation below),
    not when this script merely writes the batch -- so a run that crashes before job fetch even
    starts doesn't silently mark today's batch as done. The one honest gap: if the job-fetch step
    hits its own time budget partway through the batch (a real, expected outcome, not a crash), the
    WHOLE batch still commits as attempted, since HyperSignal doesn't expose a per-domain "did this
    one actually get fetched" record for the hcorg job-fetch path (confirmed by inspecting
    signals.db's schema directly -- fetch_runs only tracks aggregate counts per client, not
    per-domain). The batch size is kept conservative specifically to keep this approximation close
    to true in practice; a company that loses out to this occasionally just rotates through again on
    the next pass rather than being lost.
  - Company signal-scan reuses today's job-fetch batch as its base slice (same "why scan news for a
    company whose jobs weren't refreshed today" reasoning as job fetch), but ADDS every active
    Tier 1 company regardless of today's job-fetch rotation status -- written separately to
    hypersignal/data/today-signal-scan-domains.txt for run-cron.sh to pick up as SIGNAL_SCAN_DOMAINS
    (an env knob run-signals-once.mts already supports). Tier 1 news/signal events move faster than
    job-posting refresh cycles and matter most for top-priority prospects, so they shouldn't have to
    wait on the job-fetch rotation to get checked.

Usage:
  python3 src/sync_watchlist.py [--dry-run]        # normal daily run: compute + write today's batch
  python3 src/sync_watchlist.py --commit-rotation  # run AFTER job fetch succeeds or times out (not
                                                    # after a hard crash) -- marks today's batch
                                                    # "attempted" so tomorrow's rotation moves on
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

try:
    from . import fibery_client as fc
except ImportError:  # running as a script (python3 src/sync_watchlist.py), not as a package module
    import fibery_client as fc

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "hypersignal" / "clients" / "nycbound-calibration.yaml"
ROTATION_STATE_PATH = REPO_ROOT / "hypersignal" / "data" / "rotation-state.json"
SIGNAL_SCAN_DOMAINS_PATH = REPO_ROOT / "hypersignal" / "data" / "today-signal-scan-domains.txt"

BLOCK_START = "  orgDomains:\n"
BLOCK_END_PREFIX = "  useAts:"

# Deliberately conservative starting point (2026-09-22) -- start low, raise once real runs show
# clean headroom under the job-fetch time budget, rather than guess high and get stuck with more
# long stalled runs. Override with the env var if you want to test a different number without a
# code change.
import os
ROTATION_BATCH_SIZE = max(1, int(os.environ.get("ROTATION_BATCH_SIZE") or 200))

# Mirrors main.py's _TIER_RANK / _company_priority_key exactly (2026-09-17 prioritization build) --
# duplicated rather than imported so this script doesn't need to import main.py (which pulls in
# company_research.py, providers.py, and every persona module just to sort a list of domains).
_TIER_RANK = {"Tier 1": 0, None: 1, "Tier 2": 2, "Tier 3": 3}


def _priority_key(c: dict) -> Tuple[int, str]:
    tier_rank = _TIER_RANK.get(c.get("signal_tier"), _TIER_RANK[None])
    last_checked = c.get("last_checked_date") or ""
    return (tier_rank, last_checked)


def _default_rotation_state() -> dict:
    return {
        "attempted": {"tier1": [], "rest": []},
        "cycles_completed": {"tier1": 0, "rest": 0},
        "pending_batch": [],
        "pending_wrapped": {"tier1": False, "rest": False},
        "last_computed_at": None,
        "last_committed_at": None,
    }


def load_rotation_state() -> dict:
    if not ROTATION_STATE_PATH.exists():
        return _default_rotation_state()
    try:
        state = json.loads(ROTATION_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Couldn't read {ROTATION_STATE_PATH} ({e}) -- starting a fresh rotation state.",
              file=sys.stderr)
        return _default_rotation_state()
    # Fill in anything missing rather than failing outright, so a hand-edited or older-shaped file
    # doesn't break the whole run.
    defaults = _default_rotation_state()
    for key, val in defaults.items():
        state.setdefault(key, val)
    state["attempted"].setdefault("tier1", [])
    state["attempted"].setdefault("rest", [])
    state["cycles_completed"].setdefault("tier1", 0)
    state["cycles_completed"].setdefault("rest", 0)
    state["pending_wrapped"].setdefault("tier1", False)
    state["pending_wrapped"].setdefault("rest", False)
    return state


def save_rotation_state(state: dict) -> None:
    ROTATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def compute_todays_batch(active_rows: List[Tuple[str, dict]], state: dict,
                         batch_size: int) -> dict:
    """active_rows: (domain, company) pairs, already filtered to is_active_company(). Returns
    {"batch": [(domain, company), ...], "signal_scan_domains": [domain, ...],
    "tier1_wrapped": bool, "rest_wrapped": bool} -- batch is what goes into orgDomains (and is the
    set --commit-rotation will mark attempted); signal_scan_domains additionally includes every
    active Tier 1 company regardless of rotation status (see module docstring)."""
    all_tier1 = [(d, c) for d, c in active_rows if c.get("signal_tier") == "Tier 1"]
    all_rest = [(d, c) for d, c in active_rows if c.get("signal_tier") != "Tier 1"]

    tier1_domains = {d for d, _ in all_tier1}
    attempted_tier1 = set(state["attempted"]["tier1"]) & tier1_domains
    tier1_pending = [(d, c) for d, c in all_tier1 if d not in attempted_tier1]
    tier1_wrapped = False
    if not tier1_pending and all_tier1:
        tier1_wrapped = True
        tier1_pending = list(all_tier1)  # whole Tier 1 pool cleared this cycle -- start over now

    rest_domains = {d for d, _ in all_rest}
    attempted_rest = set(state["attempted"]["rest"]) & rest_domains
    rest_pending_all = [(d, c) for d, c in all_rest if d not in attempted_rest]
    rest_wrapped = False
    fill_needed = max(0, batch_size - len(tier1_pending))
    rest_pending_sorted = sorted(rest_pending_all, key=lambda pair: _priority_key(pair[1]))
    filler = rest_pending_sorted[:fill_needed]
    if all_rest and len(rest_pending_all) <= fill_needed:
        # Today's fill either exactly exhausts or was already short of the remaining pool -- next
        # time there's nothing left in "rest" to pick from, so wrap now rather than serve an
        # empty/shrinking batch on the next run.
        rest_wrapped = True

    batch = tier1_pending + filler
    signal_scan_domains = sorted({d for d, _ in batch} | tier1_domains)

    return {
        "batch": batch,
        "signal_scan_domains": signal_scan_domains,
        "tier1_wrapped": tier1_wrapped,
        "rest_wrapped": rest_wrapped,
    }


def build_domain_block(active_rows: list) -> str:
    """active_rows: list of (domain, company_dict), already sorted. Returns the YAML lines for the
    orgDomains list, e.g. '    - baseten.co          # Baseten (Qualified)\\n...'."""
    if not active_rows:
        return "    []  # no active companies in Fibery right now\n"
    max_domain_len = max(len(dom) for dom, _ in active_rows)
    lines = []
    for dom, company in active_rows:
        pad = " " * (max_domain_len - len(dom) + 2)
        status = company.get("research_status") or "New"
        lines.append(f"    - {dom}{pad}# {company['name']} ({status})\n")
    return "".join(lines)


def _commit_rotation() -> int:
    """Run after run-cron.sh's job-fetch step finishes, whether it completed cleanly or hit its
    own time budget (both are 'we tried', not a crash) -- moves today's pending_batch into the
    attempted sets, resetting whichever pool wrapped this round. Does nothing to the watchlist YAML
    or Fibery; touches only rotation-state.json. Safe to call even if there's no pending batch
    (e.g. SKIP_SYNC was set) -- it's just a no-op then."""
    state = load_rotation_state()
    pending = state.get("pending_batch") or []
    if not pending:
        print("No pending rotation batch to commit -- nothing to do.")
        return 0

    wrapped = state.get("pending_wrapped") or {"tier1": False, "rest": False}
    # Re-derive which committed domains were Tier 1 vs rest from the CURRENT company map, since
    # rotation-state.json only stores domains, not tiers, and a company's tier can change between
    # when the batch was written and when it's committed (rare, but real -- a fresh signal could
    # promote a company to Tier 1 mid-day). Good enough: worst case a domain settles into whichever
    # pool's set it lands in today, self-correcting on the next pass either way.
    token = fc.get_token()
    tier1_domains_now = set()
    if token:
        try:
            companies = fc.load_company_map(token)
            tier1_domains_now = {d for d, c in companies.items() if c.get("signal_tier") == "Tier 1"}
        except RuntimeError as e:
            print(f"Couldn't re-check current Signal Tiers ({e}) -- committing the whole batch to "
                  f"'rest' rather than guessing.", file=sys.stderr)

    committed_tier1 = [d for d in pending if d in tier1_domains_now]
    committed_rest = [d for d in pending if d not in tier1_domains_now]

    attempted_tier1 = [] if wrapped.get("tier1") else list(state["attempted"]["tier1"])
    attempted_rest = [] if wrapped.get("rest") else list(state["attempted"]["rest"])
    state["attempted"]["tier1"] = sorted(set(attempted_tier1) | set(committed_tier1))
    state["attempted"]["rest"] = sorted(set(attempted_rest) | set(committed_rest))
    if wrapped.get("tier1"):
        state["cycles_completed"]["tier1"] = state["cycles_completed"].get("tier1", 0) + 1
    if wrapped.get("rest"):
        state["cycles_completed"]["rest"] = state["cycles_completed"].get("rest", 0) + 1

    state["pending_batch"] = []
    state["pending_wrapped"] = {"tier1": False, "rest": False}
    import datetime
    state["last_committed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    save_rotation_state(state)
    print(f"Committed {len(pending)} domain(s) as attempted this cycle "
          f"({len(committed_tier1)} tier1, {len(committed_rest)} rest)."
          + (" Tier 1 pool wrapped -- starts fresh next time." if wrapped.get("tier1") else "")
          + (" Rest pool wrapped -- starts fresh next time." if wrapped.get("rest") else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    ap.add_argument("--commit-rotation", action="store_true",
                    help="Mark today's already-written batch as attempted (run after job fetch "
                         "succeeds or times out, not after a hard crash) -- see module docstring")
    args = ap.parse_args()

    if args.commit_rotation:
        return _commit_rotation()

    token = fc.get_token()
    if not token:
        print("FIBERY_API_TOKEN not set in .env -- nothing to do.", file=sys.stderr)
        return 1
    if not WATCHLIST_PATH.exists():
        print(f"No watchlist file at {WATCHLIST_PATH}", file=sys.stderr)
        return 1

    companies = fc.load_company_map(token)
    active_rows = sorted(
        ((dom, c) for dom, c in companies.items() if fc.is_active_company(c)),
        key=lambda pair: pair[0],
    )

    state = load_rotation_state()
    computed = compute_todays_batch(active_rows, state, ROTATION_BATCH_SIZE)
    batch = computed["batch"]

    original = WATCHLIST_PATH.read_text()
    start_idx = original.find(BLOCK_START)
    if start_idx == -1:
        print(f"Couldn't find '{BLOCK_START.strip()}' in {WATCHLIST_PATH} -- refusing to touch it "
              f"blindly.", file=sys.stderr)
        return 1
    block_content_start = start_idx + len(BLOCK_START)
    end_idx = original.find(f"\n{BLOCK_END_PREFIX}", block_content_start)
    if end_idx == -1:
        print(f"Couldn't find the end of the orgDomains block ('{BLOCK_END_PREFIX}') in "
              f"{WATCHLIST_PATH} -- refusing to touch it blindly.", file=sys.stderr)
        return 1
    end_idx += 1  # include the newline before useAts

    old_block = original[block_content_start:end_idx]
    new_block = build_domain_block(batch)

    old_domains = {line.strip().lstrip("- ").split()[0] for line in old_block.splitlines() if line.strip().startswith("-")}
    new_domains = {dom for dom, _ in batch}
    added = sorted(new_domains - old_domains)
    removed = sorted(old_domains - new_domains)

    tier1_count = sum(1 for d, c in batch if c.get("signal_tier") == "Tier 1")
    print(f"Active companies in Fibery: {len(active_rows)} (of {len(companies)} total)")
    print(f"Today's rotation batch: {len(batch)} domain(s) -- {tier1_count} Tier 1 (uncapped) + "
          f"{len(batch) - tier1_count} from the rotation cursor (batch size {ROTATION_BATCH_SIZE})")
    print(f"Signal-scan slice: {len(computed['signal_scan_domains'])} domain(s) "
          f"(today's batch + every active Tier 1 company)")
    if computed["tier1_wrapped"]:
        print("  Tier 1 rotation pool wrapped -- every Tier 1 company had a turn, starting over today.")
    if computed["rest_wrapped"]:
        print("  Non-Tier-1 rotation pool wraps after today's batch -- starting over next time.")
    if added:
        print(f"  + adding to watchlist: {', '.join(added)}")
    if removed:
        print(f"  - removing from watchlist: {', '.join(removed)}")
    if not added and not removed:
        print("  no change to watchlist file")

    if args.dry_run:
        print("\n[dry run] watchlist file, rotation state, and signal-scan domain file not written.")
        return 0

    if old_block != new_block:
        updated = original[:block_content_start] + new_block + original[end_idx:]
        WATCHLIST_PATH.write_text(updated)
        print(f"\nWrote {len(batch)} domains to {WATCHLIST_PATH}.")
    else:
        print("\nWatchlist file already up to date -- not rewriting.")

    SIGNAL_SCAN_DOMAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_SCAN_DOMAINS_PATH.write_text("\n".join(computed["signal_scan_domains"]) + "\n")

    import datetime
    state["pending_batch"] = sorted(new_domains)
    state["pending_wrapped"] = {"tier1": computed["tier1_wrapped"], "rest": computed["rest_wrapped"]}
    state["last_computed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    save_rotation_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
