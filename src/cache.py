"""Persistent research cache (Phase 3).

SQLite-backed, split by layer: company_research (Layer 1) and contacts
(Layer 2), so a repeat run doesn't re-pay for the same String fetches or
QuickEnrich/Apollo/Hunter lookups it already has an answer for.

Cache key is always a normalized domain, never a company name -- name
spelling drifts ("Acme" vs "Acme Inc." vs "Acme, Inc"), domains don't.

A cached result only ever comes from a completed fetch: nothing is written
until fetch_fn() actually returns, so an interrupted or failed fetch is
never cached as "done" -- the next run just retries it clean.

Two kinds of fetch result are deliberately NOT cached the same way a
confirmed finding is:

  - company_research: a qualification of "... MANUAL CHECK NEEDED" means
    the research was inconclusive (extraction failure, unreachable page),
    not a confirmed absence of signal. Cached for SHORT_TTL_DAYS instead of
    the full window, so it stops blocking on-the-day reruns without
    pretending an unresolved result is a durable fact.

  - contacts: a status of "halted - QuickEnrich budget cap reached" is a
    fact about THIS RUN hitting its credit cap, not a fact about the
    company. Never cached at all -- the next run, with a fresh cap, should
    try again rather than silently inheriting the previous run's cutoff.
"""

import json
import time
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "gtm_cache.db"

COMPANY_RESEARCH_TTL_DAYS = 14
CONTACTS_TTL_DAYS = 90
SHORT_TTL_DAYS = 1  # for a fetch result that isn't really a durable finding
# Phase 5: stated-goal research. A verified finding is as durable as company
# research generally is (a job posting/funding announcement doesn't change
# meaning week to week) -- same TTL. An unverified/not-found result is worth
# retrying sooner, same SHORT_TTL_DAYS reasoning as "MANUAL CHECK NEEDED"
# below: it's an inconclusive search, not a confirmed absence of a reason.
STATED_GOAL_TTL_DAYS = 14

# Phase 8: hardcoded here rather than imported from personas.py's PERSONAS
# registry -- importing personas.py from cache.py would create a real
# import cycle (personas.py -> persona_finance.py etc. -> providers.py ->
# cache.py). Keep this in sync by hand if a persona is ever added/removed;
# it's a 3-item list, not worth restructuring the package layout to avoid.
PERSONA_KEYS = ("cfo", "hr_people_ops", "recruiter_ta")


def persona_contacts_table(persona_key: str) -> str:
    """contacts_<persona_key>, e.g. contacts_cfo, contacts_hr_people_ops,
    contacts_recruiter_ta -- one cache table per persona (Phase 8), so a
    company's CFO contacts and People Ops contacts don't collide under one
    shared payload the way the old singular "contacts" table did (its
    primary key was domain alone -- no room for "one payload per company
    per persona"). Each table is still keyed on domain exactly like
    before; only the persona axis is new.
    """
    return "contacts_{}".format(persona_key)


TABLES = ("company_research", "contacts", "stated_goal") + tuple(
    persona_contacts_table(k) for k in PERSONA_KEYS)


def normalize_domain(url_or_domain: str) -> str:
    """Cache key: scheme, www, path, and trailing slash stripped, lowercased."""
    s = (url_or_domain or "").strip().lower()
    if "://" not in s:
        s = "https://" + s
    host = urlparse(s).netloc or urlparse(s).path
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_PATH))
    for table in TABLES:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS {} (".format(table) +
            "domain TEXT PRIMARY KEY, company_name TEXT, "
            "payload TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
    return conn


def _is_fresh(fetched_at: float, ttl_days: float) -> bool:
    return (time.time() - fetched_at) < ttl_days * 86400


def _company_research_write_rule(payload: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
    qualification = payload.get("qualification") or ""
    if "MANUAL CHECK NEEDED" in qualification:
        return True, SHORT_TTL_DAYS
    return True, COMPANY_RESEARCH_TTL_DAYS


def _contacts_write_rule(payload: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
    status = payload.get("status") or ""
    if status.startswith("halted"):
        return False, None
    return True, CONTACTS_TTL_DAYS


def _stated_goal_write_rule(payload: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
    if payload.get("found"):
        return True, STATED_GOAL_TTL_DAYS
    return True, SHORT_TTL_DAYS


def _write_rule_for(table: str):
    """Phase 8: was a flat {table: rule} dict -- replaced with a resolver
    so every contacts_<persona> table (plus the pre-Phase-8 "contacts"
    table, kept readable for old cached rows) routes to the same
    _contacts_write_rule without listing all of them by name."""
    if table == "company_research":
        return _company_research_write_rule
    if table == "stated_goal":
        return _stated_goal_write_rule
    if table == "contacts" or table.startswith("contacts_"):
        return _contacts_write_rule
    raise KeyError("no write rule for cache table: {}".format(table))


def get_or_fetch(table: str, company: str, homepage: str,
                  fetch_fn: Callable[[], Dict[str, Any]],
                  no_cache: bool = False,
                  allow_write: bool = True,
                  expected_version: Optional[str] = None) -> Dict[str, Any]:
    """Look up `homepage`'s domain in `table`. A fresh hit returns the
    cached payload and never calls fetch_fn. A miss, a stale row, or
    no_cache=True calls fetch_fn() live.

    allow_write=False still reads the cache normally but never writes a
    fresh result back. Used for dry-run contact lookups: the result is a
    cost-free preview, not a real finding, and must never overwrite a real
    cached result or poison the cache with an incomplete one.

    expected_version (added 2026-09-18, after a real incident -- see
    company_research._STATED_GOAL_PROMPT_VERSION): when a caller passes
    this, a cached row only counts as fresh if its stored "_cache_version"
    matches. This is how a prompt change invalidates old cached answers
    automatically. Without it, a prompt fix that changes what a cached
    "found: true" result even means (as happened 2026-09-17 -- see the
    tracker) leaves the old, lower-quality answer sitting in cache
    indefinitely, silently re-shipped -- and in main.py's case, silently
    re-overwriting an already-corrected Fibery record -- on every future
    run until the TTL happens to expire on its own. A table that never
    passes this argument behaves exactly as before (version_ok always
    True) -- this is opt-in per call site, not a schema-wide requirement.

    Adds a "_cache": "hit"/"miss" key to the returned dict so a caller can
    report cache effectiveness. Harmless to existing consumers that only
    read specific known keys.
    """
    assert table in TABLES, "unknown cache table: {}".format(table)
    domain = normalize_domain(homepage)
    conn = _connect()
    try:
        if not no_cache:
            row = conn.execute(
                "SELECT payload, fetched_at FROM {} WHERE domain = ?".format(table),
                (domain,),
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                _, ttl = _write_rule_for(table)(payload)
                version_ok = (expected_version is None or
                             payload.get("_cache_version") == expected_version)
                if ttl is not None and _is_fresh(row[1], ttl) and version_ok:
                    result = dict(payload)
                    result.pop("_cache_version", None)
                    result["_cache"] = "hit"
                    return result

        result = fetch_fn()

        if allow_write:
            should_write, _ = _write_rule_for(table)(result)
            if should_write:
                payload_to_store = dict(result)
                if expected_version is not None:
                    payload_to_store["_cache_version"] = expected_version
                conn.execute(
                    "INSERT INTO {} (domain, company_name, payload, fetched_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(domain) DO UPDATE SET "
                    "company_name=excluded.company_name, payload=excluded.payload, "
                    "fetched_at=excluded.fetched_at".format(table),
                    (domain, company, json.dumps(payload_to_store), time.time()),
                )
                conn.commit()

        out = dict(result)
        out["_cache"] = "miss"
        return out
    finally:
        conn.close()


def bust(table: str, homepage: str) -> bool:
    """Manually remove one domain's cached entry. Returns True if a row was deleted."""
    assert table in TABLES, "unknown cache table: {}".format(table)
    domain = normalize_domain(homepage)
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM {} WHERE domain = ?".format(table), (domain,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()



def is_fresh_cached(table: str, homepage: str) -> bool:
    """Peek at whether `homepage`'s domain has a FRESH cached row in
    `table`, without calling fetch_fn -- never writes, never spends
    anything. Phase 8: main.py's daily company-processing cap uses this
    to skip counting a company against the cap when every persona it
    would run is already served entirely from cache (no live provider
    calls about to happen), so the cap only meters real work.
    """
    assert table in TABLES, "unknown cache table: {}".format(table)
    domain = normalize_domain(homepage)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM {} WHERE domain = ?".format(table),
            (domain,),
        ).fetchone()
        if not row:
            return False
        payload = json.loads(row[0])
        _, ttl = _write_rule_for(table)(payload)
        return ttl is not None and _is_fresh(row[1], ttl)
    finally:
        conn.close()


def _connect_daily() -> sqlite3.Connection:
    """Separate tiny table for the daily company-processing cap (Phase
    8) -- date/count, not the domain/company_name/payload/fetched_at
    shape every other cache table uses, so it gets its own CREATE TABLE
    rather than being folded into TABLES/_connect()."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_runs ("
        "date TEXT PRIMARY KEY, count INTEGER NOT NULL)"
    )
    return conn


def get_daily_count(date_str: str) -> int:
    """How many companies have already entered Stage A/B persona
    discovery today (date_str, "YYYY-MM-DD"). Persistent across however
    many times main.py is invoked on that date -- it has no fixed
    schedule of its own (confirmed 2026-09-17: only the HyperSignal
    signal-watcher and promote_to_crm.py run on a Cowork schedule;
    main.py is run manually/on-demand), so the cap has to be tracked in
    storage, not in an in-memory counter that resets every invocation.
    """
    conn = _connect_daily()
    try:
        row = conn.execute(
            "SELECT count FROM daily_runs WHERE date = ?", (date_str,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def increment_daily_count(date_str: str, by: int = 1) -> int:
    """Add `by` to today's company-processing count and return the new
    total. Called once per company that's about to enter live Stage A/B
    persona discovery (not once per persona -- the cap bounds companies
    entering the fan-out, not the fan-out itself)."""
    conn = _connect_daily()
    try:
        conn.execute(
            "INSERT INTO daily_runs (date, count) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET count = count + excluded.count",
            (date_str, by),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM daily_runs WHERE date = ?", (date_str,)
        ).fetchone()
        return row[0] if row else by
    finally:
        conn.close()


def _connect_enroll_daily() -> sqlite3.Connection:
    """Separate table for the daily Apollo-enrollment cap (Phase 18), same
    persistence reasoning as _connect_daily above: the cron can be invoked
    more than once a day (a manual catch-up run after a timeout), so an
    in-memory counter would not actually bound a calendar day's sends.

    Deliberately NOT the (date, count) shape daily_runs uses. The enrollment
    cap counts DISTINCT COMPANIES, and the question asked of it is "has this
    particular company already started today" -- which a bare integer cannot
    answer. So one row per (date, company), and the count is the number of
    rows. That also makes recording idempotent for free: a second contact
    enrolled at the same company re-inserts the same primary key and leaves
    the count alone.
    """
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS enroll_daily_runs ("
        "date TEXT NOT NULL, company_id TEXT NOT NULL, "
        "PRIMARY KEY (date, company_id))"
    )
    return conn


def get_enrolled_company_count(date_str: str) -> int:
    """How many DISTINCT companies have had at least one contact enrolled in
    Apollo on `date_str` ("YYYY-MM-DD"), across every run that day."""
    conn = _connect_enroll_daily()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM enroll_daily_runs WHERE date = ?", (date_str,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def has_company_enrolled_today(date_str: str, company_id: str) -> bool:
    """Whether this company already enrolled at least one contact today, i.e.
    whether it is a company already in progress rather than a new one. Every
    remaining eligible contact at such a company is allowed through the cap --
    the cap bounds how many companies get STARTED per day, not how many
    contacts each of them yields."""
    if not company_id:
        return False
    conn = _connect_enroll_daily()
    try:
        row = conn.execute(
            "SELECT 1 FROM enroll_daily_runs WHERE date = ? AND company_id = ?",
            (date_str, company_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_enrolled_company(date_str: str, company_id: str) -> int:
    """Mark this company as having enrolled a contact today and return the new
    distinct-company count. Idempotent by primary key, so calling it for every
    enrolled contact at one company is safe and counts that company once."""
    if not company_id:
        return get_enrolled_company_count(date_str)
    conn = _connect_enroll_daily()
    try:
        conn.execute(
            "INSERT INTO enroll_daily_runs (date, company_id) VALUES (?, ?) "
            "ON CONFLICT(date, company_id) DO NOTHING",
            (date_str, company_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) FROM enroll_daily_runs WHERE date = ?", (date_str,)
        ).fetchone()
        return row[0] if row else 1
    finally:
        conn.close()
