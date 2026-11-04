# nycbound-gtm — Claude Code Context

This is a public snapshot. Never sync from Fibery here.

---

> **GOVERNING SPEC:** this project is governed by **PIPELINE SCOPE (v2)** at the end of this
> file. Read it before any qualification, enrichment or messaging work. Every messaging pass
> must apply its **Messaging Quality Gate** per email. Where it disagrees with older sections
> below, the scope section wins.

## What This Project Does

Finds companies with real NYC hiring signal, qualifies them, finds a persona-appropriate contact,
writes all of it back to Fibery, and enrolls cleared contacts in an Apollo sequence with generated
copy.

**The unit of work is a COMPANY, not a prospect.** Two scripts, in order:

1. `main.py` — for each **company** (from Fibery, or a CSV with a name column + a URL column):
   qualifies it from HyperSignal's job data, runs each live persona's contact waterfall, researches
   the company's stated goal and each contact's Likely Priorities, and writes Research Status /
   Signal Tier / Stated Goal / Contact rows back to Fibery. Also emits three `leads_*.csv` files.
2. `apollo_enroll.py` — for each **contact** Fibery has already cleared (Outreach Status = Cleared
   to proceed, Sequence Status = Not Sequenced, has an email + Persona + Likely Priorities):
   generates the 3-email sequence and enrolls them in Apollo's Sales Outreach. Reads only what
   `main.py` already wrote; never triggers research of its own.

**Run it:**
```bash
source .venv/bin/activate
python -m src.main --input fibery          # the real path -- active companies live from Fibery
python -m src.main --input companies.csv   # or a CSV: needs a company-name column + a URL column
python -m src.main --input fibery --redact # mask contact names/emails/LinkedIn in terminal output (demos)
python -m src.apollo_enroll --dry-run      # every stage runs; prints the Apollo/Fibery writes it skipped
python -m src.apollo_enroll --redact       # mask contact names/emails in terminal output (demos)
```

> **Historical note.** An earlier, different pipeline took a CSV of individual *prospects*, did a
> `web_search` per prospect, and wrote sequences to a CSV. It was removed 2026-09-16. Anything in
> this file describing per-prospect input, per-prospect web search, or a sequence CSV is about that
> removed pipeline. `sample_prospects.csv` is a leftover from it -- its columns are contact-level
> (`name,first_name,title,company,email,linkedin_url`) and it will NOT work with `--input`, which
> expects companies.

---

## Tools Connected

| Tool | Auth | Used for |
|---|---|---|
| **String** | OAuth via MCP in Claude Code; `STRING_API_KEY` in `.env` for scripts | All live web fetching, search, JS rendering |
| **QuickEnrich** | `QUICKENRICH_API_KEY` in `.env` | Primary contact enrichment |
| **Apollo** | `APOLLO_API_KEY` in `.env` | Fallback enrichment — see below |
| **Hunter** | `HUNTER_API_KEY` in `.env` | Fallback enrichment — see below |
| **Scrubby** | `SCRUBBY_API_KEY` in `.env` | Verifying guessed emails only |
| **Fibery** | MCP for docs; `FIBERY_API_TOKEN` in `.env` for the REST client every script uses | Sales-intelligence canon (synced to `fibery_docs/`); GTM Pipeline + CRM read/write |
| **Anthropic** | `ANTHROPIC_API_KEY`, model from `ANTHROPIC_MODEL` (currently `claude-sonnet-4-6`) | Stated-goal research, Likely Priorities, Ghost Email copy, the cron preflight. Usage + estimated cost tracked per call site (Phase 19) |
| **exa** | `EXA_API_KEY` in `.env` | Second search index, only when the Anthropic web-search pass comes back empty |
| **jina** | none required (`JINA_API_KEY` optional, raises rate limits) | Free fetch rung, tried before paid String |

Scrubby is strictly for addresses constructed outside QuickEnrich. Anything QuickEnrich
returns already carries an `email_verification_date` and must not be re-verified.

**Enrichment order:** QuickEnrich first. Fall back to Apollo, then Hunter, only once
QuickEnrich credits are exhausted or the company isn't in its dataset (`EMPLOYEE_NOT_FOUND`).
Emails from Apollo or Hunter are *not* pre-verified the way QuickEnrich's are, so they go
through Scrubby before use. Every QuickEnrich response echoes `meta.remaining_credits` —
watch it rather than discovering the ceiling mid-batch.

---

## File Structure

```
nycbound-gtm/
├── src/
│   ├── company_research.py  # Layer 1 — careers page discovery, ATS resolution, NYC roles,
│   │                        #   stated-goal research (persona-agnostic)
│   ├── providers.py      # Provider API clients, CreditLedger (incl. Anthropic cost tracking and
│   │                     #   the editable ANTHROPIC_PRICING table), Scrubby, the shared Stage A/B
│   │                     #   waterfalls, same_person()/email_domain_matches_company() guards
│   ├── main.py           # Entry point — Layer 1 + Layer 2, output CSVs, Fibery write-back,
│   │                     #   internal deadline, spend log, SIGTERM handler
│   ├── apollo_enroll.py  # Layer 3 — Ghost Email generation + Sales Outreach enrollment (Phase 12)
│   ├── preflight_anthropic.py  # One cheap Anthropic call before the cron's research step
│   ├── cache.py              # SQLite persistent cache (company research, per-persona contacts,
│   │                         #   stated goals, daily_runs, enroll_daily_runs)
│   ├── personas.py           # Persona registry — ALL THREE live (cfo, hr_people_ops, recruiter_ta)
│   ├── persona_finance.py        # CFO/Finance title ladder (reviewed, from the source brief)
│   ├── persona_people_ops.py     # People Ops title ladder (DRAFT, never reviewed by Rachel)
│   ├── persona_recruiter_ta.py   # Recruiter/TA title ladder (DRAFT, never reviewed by Rachel)
│   ├── fibery_client.py      # Shared Fibery REST client + every enum id map
│   ├── paradigm_import.py    # Paradigm CSV → Fibery Companies, CRM cross-check + dedupe
│   ├── sync_watchlist.py     # Fibery active companies → HyperSignal watchlist (rotating slice)
│   ├── signal_ingest.py      # HyperSignal signals.db → Fibery Signals
│   └── promote_to_crm.py     # Reply-gated CRM promotion
├── fibery_docs/          # Local cache of Fibery Sales Intelligence docs. email_plays.md and
│                         #   case_studies.md are READ AT RUN TIME by apollo_enroll.py -- a stale
│                         #   cache ships stale copy. The other five are reference-only.
│   ├── icp_and_qualification.md
│   ├── buyer_personas.md
│   ├── messaging_and_positioning.md
│   ├── research_and_stats.md
│   ├── email_plays.md
│   ├── sales_call_patterns.md
│   └── case_studies.md
├── sync_fibery.py        # Fibery doc URL list. Kept for reference; never run in this snapshot
├── tests/                # 340 tests, 19 files. No network, no API keys, nothing mocked out that
│                         #   would let a real call through. `python -m unittest discover -s tests -t .`
├── docs/ROADMAP.md       # phased plan + status
├── OPEN_ITEMS.md         # carried-forward debt and session handoffs
├── README.md             # architecture map (Mermaid) + usage
├── hypersignal/          # vendored signal watcher. SEPARATE GIT REPO, gitignored by this one,
│                         #   with its own remote -- a change spanning both needs two commits.
│                         #   Owns run-cron.sh, the launchd plist, and signals.db
├── outputs/              # gitignored: leads_*.csv, spend_YYYYMMDD.log,
│                         #   manual_review_skips.log, enroll_failures_YYYYMMDD.log
├── .cache/gtm_cache.db   # gitignored SQLite cache (see src/cache.py for the tables)
├── sample_prospects.csv  # LEFTOVER from the removed per-prospect pipeline -- contact-level
│                         #   columns, incompatible with --input. Not used by anything.
├── .env                  # all provider keys (not in git)
└── requirements.txt
```

---

## Architecture Notes

- **Layers 1 and 2 are deterministic Python; Layer 3 and two research steps are not.**
  Layer 1 (`company_research.py`) and Layer 2 (the `persona_*.py` waterfalls via `providers.py`)
  hit real provider APIs (HyperSignal's signals.db, QuickEnrich/Apollo/Hunter, optionally Scrubby)
  and are cached in `.cache/gtm_cache.db` (see `cache.py`). **But `main.py` does make Anthropic
  calls**: `research_stated_goal()` (with the server-side `web_search` tool) and
  `research_likely_priorities()` per resolved contact, and `apollo_enroll.py` generates all copy
  with an LLM. All of it is guardrailed to skip rather than ship under the bar, and every call is
  cost-tracked per call site (Phase 19). What is *not* true, and used to be: a `web_search` call
  per prospect driving qualification. That pipeline was removed 2026-09-16.
- **fibery_docs/ is a frozen snapshot in this repo** and is never refreshed from Fibery here.
  Two of its seven files are read at run time by `apollo_enroll.py`
  (`email_plays.md`, `case_studies.md`) and directly shape sent email; the rest are reference.

---

## Company Discovery Workflow

> **NOT CURRENTLY REACHED BY THE PIPELINE (since Phase 14, 2026-09-22).** `research_company()` now
> takes its roles from HyperSignal's local `signals.db` only; when there's no fresh data for a
> domain it returns `PENDING - not yet scanned by HyperSignal` instead of falling through to live
> discovery. `discover_careers_page()` still exists in `company_research.py` (line ~867) but
> **nothing calls it** — verified 2026-10-02. Everything below is therefore reference material: it
> documents how discovery worked, and how to do it by hand or if the fallback is ever restored. It
> is NOT how a company gets qualified today. Do not debug a qualification result against it.

Order matters — each rung costs more than the last.

1. **Fetch the homepage with a plain `url` first.** No `executeJS`, no `actions`.
2. **Read the page and pick the real careers/about links yourself.** Judgment, not keyword
   matching. A URL containing "job" is not automatically the right link — `clay.com/job-board`
   is Clay's *community* board of GTM roles at other companies, not their own openings.
3. **If a page comes back empty, escalate to `executeJS: true` before giving up.** Plenty of
   sites return HTTP 200 with a zero-length body on a plain fetch.
4. **Once a Greenhouse/Lever/Ashby board link is confirmed, leave String and hit the
   provider's public API directly.** Faster, free, and returns full job bodies rather than
   rendered text:
   - Greenhouse — `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
   - Lever — `api.lever.co/v0/postings/{slug}?mode=json`
   - Ashby — `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`
5. **Careers page but no board link?** Follow 2-3 individual postings and read them directly
   for relocation/visa language.

Confirm a board slug actually belongs to the company before trusting it — namesake collisions
are common (`ashby/onyx` is a San Francisco AI-search company, not Onyx Security).

---

## Relocation Signal Tiers

Not a single yes/no. Three outcomes, and the pitch changes with the tier — so record which
one fired, not just that something did.

- **Tier 1 — explicit employer-paid relocation or visa language.** "We provide relocation
  reimbursement," "Relocation support, visa support." Strongest signal.
- **Tier 2 — a physical NYC presence requirement with no relocation support mentioned.**
  Must be based in NYC, onsite N days/week, "this is an in-person role." Still a real
  prospect and arguably the better one: they have the need and no solution in place. Pitch
  it as **"no existing solution,"** never as "you already offer this."
- **Noise — a bare NYC/New York mention with neither pattern nearby.** An office address, a
  location field on its own. Discard.

### Entry-level disqualifier

SDR/BDR, coordinator, associate, and intern roles — generally under $150K base — don't get a
relocation budget. Downgrade or skip these as the *driving* signal for a company. This is the
"entry-level" half of the pre-research guardrails listed under What This Project Does.

---

## Contact Enrichment

**Do not set `has_email: true` on the first pass of QuickEnrich `dataset-search`.** It
silently drops senior people with no email on file. In one run it hid 357 of Clay's 465
records — including senior recruiting leaders — and produced three "empty" buckets that were
not empty at all. Pull the full roster first, then filter locally.

- **Rank by seniority before picking a primary.** VP/Chief/Head-of leads; Director/Manager is
  the backup.
- For anyone with no email but a LinkedIn URL, try **Email Search by `linkedin_url`** before
  concluding there's no data on them.
- Flag records whose email sits on an unrelated domain — those are agency contractors or
  stale rows, not employees.

### Scrubby API

Auth is the **`X-API-Key` header, not `Authorization: Bearer`.** Bearer returns
`401 {"detail":"Invalid API key"}` even with a valid key.

```
POST https://api.scrubby.io/validate_email
X-API-Key: $SCRUBBY_API_KEY
Body: {"email": "address@domain.com"}
```

Response fields: `email`, `status`, `result`, `quick_status`, `credits_used`,
`remaining_credits`, `retry_after_seconds`.

**This is not the same field set as the web UI.** No `overall_score`, no `is_catch_all`, no
`mx_records`. Don't expect API parity with a manual UI check, and don't plan a workflow around
fields the API never returns.

### Email pattern discovery

**Don't default to `first.last@`.** When a company's real pattern is unknown, test in this
order:

1. Any already-confirmed address, as a baseline
2. Short first-name-only (`joe@`) — the form the person actually goes by
3. Full `first.last@`
4. Full `firstname@` (`joseph@`)

**One resolved address does not confirm the pattern for the rest of a roster.** This is
especially true when that person has more than one working address — a co-founder with both
`first.last@` and `first@` is a sign the pattern *isn't* uniform company-wide, not proof
of what it is. At one prospect company, generalizing from that one person produced five straight hard
bounces; the real pattern was short-first-name-only.

Once a pattern is confirmed, re-test a contact held for a separate unresolved detail only if
the confirmed pattern doesn't require that detail. If it does (e.g. the pattern needs a
surname and the surname is what's unconfirmed), the hold stands until that's resolved on its
own.

### Always run a catch-all control

Before trusting any "valid" result on a domain, test one deliberately fake address
(`nonsense123@domain.com`). A hard bounce there confirms the domain isn't catch-all, so the
other results mean something. Skip it and every "valid" is ambiguous.

---

## Pre-Send Check

> **Manual guidance, not a pipeline step — and largely superseded.** No code in `src/` touches
> Gmail (verified 2026-10-02). The automated equivalents now run on every contact:
> `main.py`'s Phase 8 reconciliation (Fibery Last Sequenced Date, then Apollo
> `last_activity_date`, 60 days) and `apollo_enroll.py`'s Phase 18/20 pre-checks (Apollo activity
> inside 60 days, a mailbox/`email_import` source, a linked CRM Contact, `email_true_status`
> "Potentially Invalid"). The one thing those still cannot see is a thread sent from Rachel's inbox
> outside Apollo — which is the remaining reason to do the check below by hand.

Before any cold send, search Gmail for prior threads with the contact's email or their domain.

- **No thread found, or nothing newer than 60 days** — clear to send the cold sequence.
- **Anything inside 60 days** — this is a warm contact. It needs a reconnect note, not the
  cold sequence.

---

## Editorial Judgment Rules

Plays in `fibery_docs/email_plays.md` are **frameworks, not checklists.**

- Cut elements that don't flow naturally in context
- Never include every bullet from a play just because it's listed
- Judgment beats compliance — a shorter email that flows beats a complete one that doesn't
- Never say "quick one" or any variation
- If opening with a question: ask the question first, then name
  e.g. "Across the NYC engineering relos Acme runs this year, do you have the actual
  math on what a failed one costs, Jordan?" — NOT "Jordan, quick question..."
- No em dashes
- No filler openers ("I wanted to reach out...", "Hope this finds you well...")
- Short paragraphs — 1-2 sentences max
- Target word counts: Email 1 ~100 words, Email 2 ~80 words, Email 3 ~50 words

---

## Personas & Plays

Full detail in `fibery_docs/buyer_personas.md` and `fibery_docs/email_plays.md`.

Quick reference:
- **Global Mobility** — pitch as NYC execution layer inside their existing RMC program.
  This is the play that leads with the **$701,404** figure, not CFO/Finance
- **People Ops** — bandwidth + candidate experience angle
- **CEO/Founder** — protect the first 30 days / cost of a failed hire
- **CFO/Finance** — protect the forecast, not "save money" in the abstract. Email 2 leads
  with the **$117K** local-vs-DIY gap ($952,547 vs $835,647 on a $165K SWE), then escalates
  to the 40% DIY failure rate and **$251,144**. Never name the company anywhere in the body
- **Recruiter/TA** — protect your placements

### Naming: NYCbound vs Arrival Ops

Canon lives in **Fibery Email Plays (view 480)**. These are *not* split by persona:

- **Founders get plain language only** — never say "Arrival Ops" to a founder.
- **Every other persona uses both together** — *Arrival Ops* as the category, *NYCbound* as
  the named program.

### CFO/Finance play — rebuilt Sept 2026

Verified against Email Plays (view 480) on 2026-09-09.

The CFO/Finance play was **rebuilt in September 2026**. Draft from the live version only.
Two older CFO artifacts sit under **Archived Plays** in that same doc and must not be used:

1. The original hidden-tax main framework ("CFO / Finance Leader (archived, replaced Sept
   2026)") — ~$12K on fire, sarcastic tone, "willing to take that bet?"
2. The field-tested **Aug 28 2026 alternate**, nested inside that archived play.

The archived alternate still promises "30 days becomes about 5" — a turnaround-time claim the
live play explicitly retires company-wide. That alone disqualifies it.

**There is no Recruiter/TA alternate**, live or archived. Earlier notes claiming one were
wrong; nothing in the doc matches.

The live CFO play requires real pre-research: the actual posted salary for the role the
company is hiring into NYC, run through the Ramp Calculator. Fall back to the $165K SWE
default only when no real salary is findable. The CFO lead pipeline
(`python -m src.main`) produces exactly this in its Role Band Used column.

The play also requires a second required pre-research step, same discipline as the salary lookup:
the company's actual **stated goal for the hire** — what the job posting itself says about it (new
function, team expansion, backfill), recent funding news, or a product launch driving the headcount
need. This is not optional and not a manual reminder living only in a Fibery doc — a goal-tied play
(this one, **Ramp Risk**, and any future one) doesn't actually work without it. Skip only when
genuinely no signal exists after a real look; never fall back to generic framing when a real one
exists but wasn't checked.

### Running the CFO pipeline (hardened 2026-09-10)

One command for the routine case. No `--estimate` / `--dry-run` / `--max-credits`
sequencing needed:

```bash
python -m src.main --input companies.csv
```

It qualifies every company first and **only spends credits on the ones that
qualify**, under a built-in QuickEnrich cap (2 credits per company, floor 25,
ceiling 120). Overrides: `--estimate` (spends nothing), `--dry-run` (no paid
lookups, still pays String for job research), `--max-credits N`, `--enrich-all`.

Three output CSVs, identical headers, all Apollo-import-ready:

| File | Contents |
|---|---|
| `cfo_leads_ready_<stamp>.csv` | QuickEnrich emails — pre-verified, send-ready |
| `cfo_leads_needs_verification_<stamp>.csv` | Apollo/Hunter emails — **run Scrubby first** |
| `cfo_leads_no_contact_<stamp>.csv` | Qualified or not, no email found |

**Qualification constants** live in `company_research.py`: `MIN_SALARY_FLOOR` (150000,
disqualifies only when *every* NYC role is priced and all are under it),
`ROLE_REMOTE_PATTERN` (per-role — a separate remote role elsewhere on the board
does not disqualify an NYC one), and `NYC_PATTERN` (all five boroughs).
A company with no board resolved reports **`NO BOARD DETECTED - MANUAL CHECK
NEEDED`**, never a bare no-signal.

### Throttles, deadlines and logs (current as of 2026-10-02)

Four separate limits, each bounding a different axis. Know which one you're hitting:

| Limit | Where | What it bounds |
|---|---|---|
| `--daily-cap` (25) | `main.py`, `cache.daily_runs` | Companies entering **research** per day |
| `DAILY_ENROLL_COMPANY_CAP` (25) | `apollo_enroll.py`, `cache.enroll_daily_runs` | **Distinct companies** starting **enrollment** per day |
| `MAIN_PY_DEADLINE_SEC` (1500) | `main.py`, env-overridable | Wall-clock before `main.py` stops starting new companies |
| `MAIN_BUDGET_SECONDS` (1800) | `run-cron.sh` | Outer time-box; SIGTERM then SIGKILL 5s later |

The enrollment cap counts **companies, not contacts**. A per-contact cap was tried and removed as a
mistake (`MAX_CONTACTS_PER_RUN`, 69f8a5c) — it throttled the wrong axis, starving an already-started
company while doing nothing about how wide the day's outreach reached. Every eligible contact at a
company already in progress still goes out.

Three append-only logs under `outputs/` (gitignored), all flushed per write so a killed run still
leaves a current record:

- `spend_YYYYMMDD.log` — per-company ledger snapshots **and** per-call Anthropic usage/cost
- `manual_review_skips.log` — every contact the Fibery write guards declined to create, with reason
- `enroll_failures_YYYYMMDD.log` — every failed enrollment, with Apollo's own response body

---

## Known Issues / History

- ~~Per-prospect cost ~$0.33 (web search token bloat) — paused pending Paradigm comparison~~
  **Obsolete.** This measured the per-prospect LLM pipeline removed 2026-09-16. Anthropic cost is
  now tracked per call site by `CreditLedger` (Phase 19) and reported in the end-of-run summary;
  see `ANTHROPIC_PRICING` in `providers.py` for the (still unconfirmed) rates.

### extract_salary() can grab the wrong dollar figure from a long job body (found 2026-09-16, FIXED 2026-09-17)

Discovered while smoke-testing the new HyperSignal integration (Phase 5) against real data:
AssemblyAI's postings all resolved to a **"$500K" salary band**, but the actual posted range for
its Commercial Counsel role (confirmed by reading the raw description) is **$190,000-$225,000**.
The company's own boilerplate intro paragraph says "...under 100 people generating roughly **$500K
ARR per employee**..." — that number sits inside `extract_salary()`'s believable-base-salary window
(`50_000 <= candidate <= 700_000`) and appears earlier in the text than the real salary line, and the
old logic took the *first* in-window match regardless.

**Fixed:** `extract_salary()` now prefers the first in-window match that's immediately preceded
(within 100 chars) by a salary label — "salary", "compensation", "comp", "pay range", "pay band",
"base pay", "base salary" (`SALARY_CONTEXT_PATTERN`) — over an earlier unlabeled one. Falls back to
the old first-in-window-match behavior when nothing in the text is labeled at all, so postings that
never label their range (most of them) are unaffected. Verified against the real AssemblyAI posting
text pulled from `hypersignal/data/signals.db` (its real "Salary range: $190,000 - $225,000" line
now wins over the ARR figure) and against the Hightouch multi-zone case the function's docstring
already documented (first range's top still wins over a later, larger zone's max). Regression suite:
`tests/test_salary_extraction.py` (7 tests, `python -m unittest tests.test_salary_extraction`).
### dbt Labs — flagged for manual review (2026-09-10, no action taken)

Possible post-merger entity change. Its careers page at `getdbt.com/about-us/careers`
now serves job links pointing at **`fivetran.com/careers`**, and roles are tagged
`USA - New York (dbt)` alongside plain `USA - New York`. Two consequences to
check before any outreach:

* **ICP eligibility** — if the hiring entity is now Fivetran, the 200-2,000
  employee band and the Series C+ framing may no longer apply.
* **Contact validity** — enrichment returned a finance contact whose email sat on
  an unrelated company's domain. The `_agency_domain_flag` guard fired correctly
  and the row was rejected. QuickEnrich held a different person of the same name
  at dbt Labs and Apollo resolved the name to the wrong person, so treat any dbt Labs finance contact as unverified until the entity is settled.

Do not enrich or sequence dbt Labs until someone confirms which company is hiring.

### Detection-gap evidence base (probed 2026-09-10, `detection_gap_probe.py`)

Every rung the pipeline can see was exercised per company: homepage markdown,
careers-page plain markdown, JS-rendered markdown, raw HTML, JS-rendered raw
HTML. Result: **8 of 10 known-Ashby companies expose no `ashby` string
anywhere.** Their slug is simply the company name. These are the regression
cases for any future hardened-`ats_belongs_to` + bounded-guessing change —
each must resolve without page inspection:

| Company | Confirmed slug | careers page probed |
|---|---|---|
| Valon | `Valon` | valon.com/careers |
| Sardine | `sardine` | sardine.ai/careers#top |
| Graphite | `graphite` | graphite.com/careers |
| Stedi | `Stedi` | stedi.com/careers |
| TigerData | `TigerData` | tigerdata.com/careers |
| Venn | `Venn` | venn.com/careers/ |
| Spade | `Spade` | spade.com/careers/ |
| Mintlify | `Mintlify` | mintlify.com/careers |

The other 2 were **reachable all along** and failed for two unrelated,
already-fixable reasons. Neither needs guessing:

1. **Normal Computing — the markdown-length gate discards a candidate before
   raw HTML is ever read.** `careers.normalcomputing.com` returns **0 chars** of
   markdown (plain and JS-rendered) but 27KB of raw HTML carrying
   `jobs.ashbyhq.com/NormalComputing`, which `ATS_SLUG_IN_HTML` extracts
   correctly. In `discover_careers_page` the `if not page or len(page.strip())
   < 200: continue` check runs *before* `resolve_ats`, so the raw rung never
   fires. CLAUDE.md's own discovery workflow says to escalate on an empty page,
   but the escalation only goes to executeJS markdown, never to raw.
2. **Truemed — a `.com` in the CSV company name makes `ats_belongs_to` reject
   the correct board.** Its careers page raw HTML carries `<div
   id="ashby_embed">`, the marker matches, and `_slug_candidates` produces
   `truemed` as candidate #2, whose board returns 11 jobs. But the input CSV
   names the company `Truemed.com`, so token extraction yields the single token
   `truemedcom`, which never appears in the board JSON →
   `ats_belongs_to` returns **False** and a correct board is thrown away.
   Verified: same board with name `Truemed` verifies True. So this function
   fails **closed** here while failing **open** on `ashby/onyx` — broken in
   both directions. Strip TLD-ish suffixes before tokenising.

- **`ats_belongs_to` fails open — it does not do what its docstring claims.**
  It accepts any company-name token longer than 2 chars appearing anywhere in the
  board JSON. Verified 2026-09-10: `ats_belongs_to(ashby/onyx, "Onyx Security")`
  returns **True** — the exact namesake collision its docstring cites as the
  reason it exists. Harmless only because nothing currently blind-probes slugs.
  Must be strengthened (require the company's own domain in the board's apply
  URLs, not just a name token) **before** any slug-guessing is enabled.
- **"No NYC roles" on a big employer is usually a landing page, not an empty
  board.** Verified 2026-09-10 on ElevenLabs and Polymarket: the careers URL
  found from homepage nav is a marketing page whose listings sit one level
  deeper behind a "See open positions" link that discovery never follows.
  ElevenLabs' page yields 116 markdown links of which 94 pass `_looks_like_role`
  — all nav/CTA/image-alt noise ("Skip to content", "Sign up"). Polymarket's is
  4,778 chars with zero "New York" mentions. Their real Ashby boards are live
  (248/11 and 76/70 jobs/NYC) but no page text names the provider, so the
  current no-blind-probing policy cannot reach them. The Aug 2026
  `string_test_results_v4.csv` numbers came from `discovery_method = "slug
  guess"`, which is why they looked better.
- **Per-role location is not persisted to output — fix before the next full
  batch.** `OUTPUT_COLUMNS` writes `Open NYC Role(s)` as title + salary +
  entry/senior tag only, so the location string that `research_company` matched on
  is discarded. Downstream you can tell *that* a role matched NYC but not
  whether NYC was **required** or merely **one of several options** ("New York
  or London or Remote"). That makes the "roles don't actually require NYC"
  disqualifier unverifiable after the fact: the 2026-09-10 reclassification had
  to leave it unassessed across all 48 qualified companies. Also note
  `Open NYC Role(s)` truncates at `scored[:8]`, so per-role checks are only
  fully decidable when a company has <= 8 NYC roles. Both are cheap to fix
  (carry `location` through and stop truncating, or add a `Locations` column) and
  need no extra fetches. Not urgent for a single paid pass; do it before the
  next 83-company run.
- **Second-order, open:** `_jobs_from_page_text` only counts a role when the NYC
  match falls inside the markdown link text or href. Careers pages that put the
  location in a *sibling* element (adjacent table cell or div) report zero NYC
  roles even when the page rendered fine and the roles are real. Fixing ATS-board
  detection covers most cases, since roles then come from the provider API where
  location is a typed field. Needs a set of real failing examples before being
  widened — matching bare non-link lines was tried and counted office addresses
  and location-filter controls as jobs.
- **Open, needs a decision:** the signal tier is computed from the concatenated
  text of *all* NYC roles (`company_research.py`, `blob` in `research_company`), so
  relocation language inside an entry-level posting can still produce Tier 1.
  The entry-level rule only excludes a role from being the *driving salary*, not
  from setting the tier. Lithic is the live example: Tier 1 off a $75-100K
  Business Operations Associate.
- **Open:** `is_entry_level` short-circuits on title keyword before checking
  salary, so `\bassociate\b` tags senior compound titles ("Associate Director",
  "Associate General Counsel" at $260K) as entry-level regardless of posted comp.
- Fibery MCP cannot be called from the Python script directly (OAuth-only, no API token support)
- fibery_docs/ cache is the workaround (frozen in this public snapshot)

### RAMP CALC block leaked exact dollar figures into CFO emails instead of shorthand (found + FIXED 2026-09-19)

`compute_ramp_calc()` in `company_research.py` returned only exact-integer dollar fields
(`local_total`, `diy_total`, `gap`, etc.), and `main.py`'s CFO-only RAMP CALC block formatted them
with raw `${:,}`. The Messaging Quality Gate calls for **shorthand** figures ("~$950K") in the
email body, but with an exact integer sitting right there in the block, the model had nothing to
round from and kept citing it verbatim -- e.g. Polymarket's CFO emails going out with
`$2,244,303` / `$1,968,873` instead of shorthand.

**Fixed:** `compute_ramp_calc()` now also returns a matching `_fmt` shorthand string for every
dollar field (via a new `_shorthand_dollars()` helper), and `main.py`'s CFO RAMP CALC block
construction was switched to consume the `_fmt` fields instead of the raw integers, so the model
is never shown an exact number to begin with. Verified via diff against a pre-edit backup (only
the intended lines changed) and a clean `ast.parse`. Committed `c75ba72`.

**Operational note that came out of verifying this fix:** the fix only changes what *new*
research produces. A company's Likely Priorities text (including any RAMP CALC block) is only
regenerated when that company is re-processed through `main.py`; a company not included in a
given `--no-cache` run keeps whatever was written by an earlier, possibly pre-fix, pass.
Polymarket's exact figures reappeared *after* this fix had already shipped simply because
Polymarket wasn't in the batch that was re-run -- not a regression. This came up twice in one
session (here, and with the driving-role fix below): **when a generated email looks wrong, check
whether that company was actually re-processed since the fix landed before concluding the fix
didn't work.**

### Fabricated case study in non-CFO plays -- `case_studies.md` was never loaded into the prompt (found + FIXED 2026-09-19)

`apollo_enroll.py`'s `_GHOST_EMAIL_PROMPT` loaded the per-persona play section from
`fibery_docs/email_plays.md`, but never loaded `fibery_docs/case_studies.md` (Rachel's 6 real,
approved case studies) at all. CFO's play spells its case study out inline, so CFO emails were
never affected. People Ops' play instead just instructs the drafter to "segue into a real,
approved case study" without embedding the story -- so when the model was told to use a real case
study and given none, it invented one wholesale (a fictional "VP relocating from out of market...
in under two weeks" story, in a People Ops Email 2).

**Fixed:** `case_studies.md` is now loaded in full and injected into the prompt as a labeled
"APPROVED CASE STUDY LIBRARY" section, with a new hard rule forbidding invented case studies and
requiring the model to omit a case study entirely rather than fabricate one if nothing in the
library fits reasonably well. Verified on the next dry-run: every case study cited across all
contacts (People Ops, Recruiter/TA and CFO alike) matched an entry in the real
library. Committed `ca754b3`.

### Non-CFO plays fell back to vague "Director-level" language -- `driving_role` was never threaded through (found + FIXED 2026-09-19)

`_summarize_signals_for_likely_priorities()` in `company_research.py` passed the generic
`role_band_used` category and `band_base` salary into the prompt that generates Likely Priorities
narrative, but never `jobs.get("driving_role")` -- the actual specific job title (e.g. "Director,
Data Analytics"). Only CFO's separate RAMP CALC block in `main.py` threaded that title through,
so only CFO emails could cite a specific role; People Ops and Recruiter/TA emails fell back to
vague generic level language ("Director-level roles," "senior director level").

**Fixed:** `driving_role`, when present, is now passed into the same prompt as "Specific role
driving this signal," with a new hard rule requiring the model to name that exact title rather
than soften it to a generic level description. Verified on the next dry-run (a People Ops
email correctly cited "Dir. Data Analytics"). One case needed a second check rather than a clean
pass: an email cited "Sr. Software Engineers", which doesn't match the CFO persona's
`driving_role` for that company -- cross-checked against the real "Open NYC Role(s)" data and
confirmed it's a second, genuinely open role at the same company, not a fabrication. Committed `ca754b3`
together with the case-study fix above.

### apollo_enroll.py's MAX_CONTACTS_PER_RUN=12 was a leftover testing value, not a real cap (found + FIXED 2026-09-19)

`apollo_enroll.py` capped every invocation at 12 contacts. This had been assumed to be an
intentional staged-rollout safety limit -- it was not. Per Rachel: it was a testing-only value
that leaked into the real default. The one real, intended per-run throttle is `main.py`'s
`--daily-cap` (default 25 companies/run, **unchanged**) upstream of Apollo enrollment; Apollo
enrollment itself should not be separately capped.

**Fixed:** `MAX_CONTACTS_PER_RUN` removed entirely. `--limit` is now an optional, uncapped-by-
default flag for one-off testing invocations only -- the cron invocation never passes it, so a
normal run enrolls every eligible contact. Verified `grep -rn "MAX_CONTACTS_PER_RUN" src/` returns
nothing. Committed `69f8a5c`.

### Phases 17-20: dedupe guards, cron hardening, cost tracking (2026-10-01 / 2026-10-02)

Suite went from 119 tests (11 of them erroring, in a suite believed green) to **340 passing**.

**Phase 17 - duplicate people and wrong-domain emails.** The duplicates that reached Fibery and
Apollo were never catchable by email: two providers spell one person differently (a "Sam" /
"Samantha" nickname pair, or Apollo's masked "Robert G" beside Hunter's full surname) and usually resolve
*different* emails for them, so an email-equality check is blind by construction.
`providers.same_person()` is nickname-aware (curated **disjoint** groups -- a name in two groups
would chain them transitively, so a test asserts disjointness), initial-aware (Apollo's "Ka***n"
and a bare "G" both reduce to a first letter) and LinkedIn-slug aware. **A missing last name is
never enough on its own**: two different people at one company can share a first name, and a false
merge silently drops one of them from outreach -- which is worse than the duplicate it prevents.
Applied at four sites: `find_contact_generic`, `find_persona_contacts_generic` (before Stage B, so
one human costs one email resolution), `main.py` before a Fibery create (first persona wins), and
`apollo_enroll.py` before enrollment. Alongside it, `email_domain_matches_company()` rejects an
agency/stale address (another company's domain on a prospect's record) but **never rejects on absent data** -- a
blank domain is missing information, not evidence. Also: the Stage A waterfall now stops at the
first rung yielding a qualifying candidate, so a company QuickEnrich already has a CFO for no
longer costs an LLM call plus a paid Apollo search. `max_candidates` is a cap *within* a rung.

**Phase 18 - cron hardening.** The outer time-box kills the process group (SIGTERM, SIGKILL 5s
later), which used to end a run mid-company with no CSVs, no write-back and no spend record.
`main.py` now has its own 1500s deadline (300s of headroom) checked *before* starting each company,
so it finishes cleanly; plus a SIGTERM handler and a flushed per-company spend line.
`PYTHONUNBUFFERED=1` for both python steps, because this script's stdout is a pipe and a killed run
was losing its last ~8KB block -- exactly the stretch needed to diagnose it. Also fixed a label bug
I introduced in `5a753ea`: the preflight block set `STEP` and never restored it, so a `main.py`
timeout reported "Anthropic preflight ... timed out", pointing at the one step that had succeeded.

**Phase 19 - Anthropic cost tracking.** Every call tagged by site (research, extract,
likely-priorities, ghost-email, preflight), with input/output tokens and web-search count, estimated
from the **editable** `ANTHROPIC_PRICING` table at the top of `providers.py`. **Those rates are
placeholders pending confirmation against current pricing.** No ceiling, by design; two tests pin
that (no budget attribute, and a huge recorded cost changes no existing verdict).

**Phase 20 - enrollment failures.** `_apollo_add_to_sequence` returns `(ok, detail)` so Apollo's own
response body (first 500 chars, body only -- headers are never logged) reaches the failure message
and `enroll_failures_YYYYMMDD.log`. Plus a skip on `email_true_status == "Potentially Invalid"`.

### Apollo declines a "verified" email: email_status is not email_true_status (2026-10-02)

Four enrollments failed (contacts A, B, C and D), all with Apollo returning **HTTP 200 and then silently not adding the
contact**. All four carried `email_status: "verified"` -- the only email field this code read -- while
Apollo's own `email_true_status` disagreed: "Potentially Invalid" for A and B, "User Managed"
for C and D. Three of four also carried a `contact_job_change_event` (A's and B's
stamped the same second the contact was created, Apollo disagreeing about which org the person is
at) and three of four were on catch-all domains with verdict `warn`. The sequence itself was healthy
(active, 3 steps, 141 delivered).

**This is circumstantial, not Apollo's stated reason** -- the body was discarded at the time, which
is exactly why nobody could say why. Phase 20 captures it now. Only "Potentially Invalid" is
skipped: **"User Managed" means a human set the address deliberately**, which is a reason to trust
it, so C and D will be attempted again and may fail again -- but the body will now explain
it. Diagnosis was read-only; re-issuing `add_contact_ids` *is* the enrollment, so it was not done.

### Unattributed Anthropic traffic on this key (open, 2026-10-02)

Haiku and Sonnet requests appeared on the nycbound-gtm key at ~3:00pm and ~3:29pm ET, after the
cron ended at 2:27pm, with the Haiku calls at ~400 input / 4-18 output tokens -- the shape of a
classifier. **Nothing in this repo can produce a Haiku call**: `.env` pins
`ANTHROPIC_MODEL=claude-sonnet-4-6`, all three call sites read that variable, and no Haiku string
exists in the Python. Also ruled out: no other script calls an LLM; every test mocks the LLM path;
no user crontab and only the one launchd job; no lingering cron children; no `@anthropic-ai` in
hypersignal's dependency tree and no hypersignal `.env`; `.zshrc` does not export the key;
`~/.claude/settings.json` sets no `env`/`model`/`apiKeyHelper` (so Claude Code is on subscription
auth, not this key); the only other project `.env` carries a different key. Token shapes match
nothing here either. **Next step is the Anthropic Console request log**, which shows per-request
model, workspace and key -- and checking whether the key is installed on another machine, a server,
or a connector/IDE extension.


---

# PIPELINE SCOPE (v2) — GOVERNING SPEC

**This section is the standing, perennial reference for this project.** Merged 2026-09-11 from
`nycbound-gtm-cfo-pipeline-scope.md`. It is not a status update. Architecture, settled policy, and
the Messaging Quality Gate below govern every future pass, for this persona or any future one.

**Precedence:** where this section and an older section of CLAUDE.md disagree (qualification
thresholds, salary policy, attribution rules), **this section wins**. Older text is kept for history.

**Every messaging pass must read and apply the Messaging Quality Gate before presenting output.**

This is a general company-research and outreach pipeline, not a CFO-only tool. CFO is the first
persona built on top of a shared core.

## Architecture: three separable layers

**1. Company research (persona-agnostic).** Headcount/ICP screening, careers-page discovery, roles
found, comp, NYC-required status, and the stated goal behind the hire (job-posting language, funding
news, product launch — same required-pre-research discipline as salary). This doesn't change based
on which persona is being targeted. A
company's real roles and comp are facts about the company, not about who we're emailing. **Cached
with a date stamp**, so running a different persona (HR/People Ops), or the same persona again
later, against the same or overlapping company list reuses this layer instead of re-researching.

**2. Contact finding (persona-specific).** Title-tier matching changes per persona. The CFO play
looks for CFO/Head of Finance/VP Finance titles; an HR play would look for People Ops/HR Leader
titles. Cached separately, keyed by company + persona + date.

**3. Messaging (persona-specific, last step).** Ramp Calculator math plus the finalized email play
for that persona. Runs only after a company has cleared qualification, has a real ATS-confirmed
role, and has a contact with a validated email. Never before.

## Happy path

1. **Intake** - company list (name + URL) in.
2. **Data-quality gate** - headcount checked against the 200-2,000 ICP floor before any discovery spend.
3. **Discovery (real navigation, not guessing)** - fetch the company's own homepage from the list
   URL. Follow the actual careers/jobs nav link found on the page (not a guessed URL pattern). If
   roles sit behind a "see open roles" interaction, follow it. Click into one role to capture the
   real ATS board URL directly from the JD link. Pull the full roles list from that confirmed board.
4. **Qualification** - a company qualifies with **at least 3 NYC-required roles** (not
   remote-optional on that specific role). **Missing salary never disqualifies** - the role stays
   in, flagged comp-unproven.
5. **Contact enrichment** - QuickEnrich -> Apollo -> Hunter, verified end to end. **Scrubby verifies**
   any Apollo- or Hunter-sourced contact before it is considered ready.
6. **Messaging** - last step only.
7. **Export** - CSV for now.

## Settled policy

- **Salary point for calculator math: band minimum, not midpoint.**
- **Salary-range qualification rule:** a posted range counts as a comp signal, and uses the range
  MINIMUM for math, as long as its MAXIMUM meets or exceeds the $150K floor. Only a range entirely
  below $150K fails as a comp signal for that role. The company is never disqualified for this alone.
- **Total compensation, when that is what a posting quotes, is used as-is.** No attempt to back out
  an estimated base. Applies to every such company, not just Formation Bio.
- **"Our research" vs market data is a scope distinction, not a conflict.** "Our research" is
  accurate for the Ramp Calculator's own output (Year 1 values, the gap). It is never accurate for
  third-party data such as the 40% DIY-relocation-failure rate, which is market data and must be
  framed as such. No Fibery edit needed.
- **No-posted-band wording locked:** "No salary on the JDs, so based it off market rate for the
  role" for all no-posted-band companies.
- **NYC-required threshold: >=3 qualifying roles.**
- **Missing salary: never a disqualifier.**
- **Stated-goal research is a required pre-research step for every goal-tied play** (CFO, Ramp Risk,
  and beyond) — same discipline as the salary lookup, not a manual reminder that only lives in a
  Fibery doc. Pull the company's actual reason for the hire before messaging: job-posting language,
  funding news, or a product launch. A genuinely absent stated-goal signal never disqualifies, same
  as missing salary — but skipping the check itself is not allowed.
  **Built (Phase 5, 2026-09-16):** `company_research.research_stated_goal(company, domain)`. This is
  search-and-cite, not inference — the prompt requires a verbatim quote and returns `found: false` on
  anything ambiguous, so what comes back is a real citation of what the company itself said, safe to
  use directly in outreach copy ("saw you're expanding into X because [quote]"), not just internal
  context. Pipeline: a direct Anthropic API call with the native web-search tool on (`found`, `goal`,
  `source_type`, `excerpt`, `url`), escalating to `exa` as a second search index only on a miss.
  **Citation verification is mandatory and automatic**, not just a human-review hope: every `excerpt`
  is checked against the live page (`providers.verify_excerpt_on_page` — jina, then String) before
  the finding is accepted; an excerpt that doesn't verify is treated exactly like `found: false`,
  never kept as a plausible-looking but unconfirmed quote. Cached per-domain, 14-day TTL on a verified
  finding, 1-day TTL on a miss (cache.py's `stated_goal` table). Gated the same as contact enrichment
  in `main.py::run()` — only runs for qualifying companies, skipped entirely on `--dry-run` (an LLM/exa
  call, no free discovery rung of its own). Verification confirms the quote is *real*, not that it's
  the *right* one to lead with if a company has multiple signals — that judgment, and calibration
  spot-checks against the `goal` categorization, are still a human review step (Phase 8).
- **Value-factor mapping** for roles outside the 4 documented bands, by closest analogy:

  | Role type | Maps to | Factor |
  |---|---|---|
  | Software engineer, designer, data/technical | SWE | 5.4x |
  | Account exec, RevOps, marketing/demand-gen | GTM Lead | 5.0x |
  | Chief of Staff, ops/strategy, HR/People Ops, recruiting | Ops | 3.0x |
  | Product Manager, senior product leadership (Director/VP Product) | SWE-adjacent | 5.4x |

  **Map by what the role's source population actually does, never by process of elimination.** "Not
  engineering and not sales, so the leftover bucket" is invalid reasoning. The ops bucket is for
  genuinely generalist coordination and support functions, not senior strategic leadership.
  Seniority and leverage raise the factor, never default it downward.
- **Multiple tiers in one posting:** pick the single title matching whichever tier's salary is used
  in the math, and use that title consistently. Never an "or" construction in copy.
- **Company merges/rebrands mid-list:** the surviving entity is fully requalified from scratch.
- **Messaging is the last phase**, strictly after qualification, role confirmation, validated contact.
- **Cadence:** ongoing, reusable system. This is why the dated cache matters.

## Still needs a real fix (not yet built)

1. **Persistent cache, date-stamped, split by layer** (company research vs contact-per-persona). Top priority.
2. **Real navigation-based discovery** replacing guessed URLs.
3. **Apollo wiring** - confirm the waterfall calls Apollo on every QuickEnrich miss. Dry-run never
   reaches Apollo, so verify against a real run.
4. **Scrubby as an explicit verification step** for Apollo/Hunter contacts.
5. **"Associate" entry-level classifier bug** - salary ceiling must outrank the keyword match.
6. **Domain-mismatch verification** - string-similarity heuristic first, human glance for the rest.
7. **Standing regression test for the title-classifier bug class.**

## Decision: the 83-company batch is frozen

No further credit spend re-qualifying that run. Qualification stands as determined. The net-new
infrastructure still gets built, but applies to the *next* list or persona, not retroactively.

---

## MESSAGING QUALITY GATE (every persona, every generation pass)

Before any drafted sequence is presented as finished, self-check **per email**, not once per sequence:

1. **Does line one sound genuinely from inside the company**, matching this persona's voice (the CFO
   play reads like an internal FP&A note), **and does it earn the read?** A generic "category of
   spend" framing is a recurring failure mode and reads as sales copy dressed as internal.
2. **Is there real value in the body**, not restated pain the reader already knows?
3. **Does the mechanism land as stated fact**, not something needing external validation? (This is
   where attribution errors and naming our own tools leak in.)
4. **Would a real [persona title] forward or reply**, not read and archive?

This runs automatically before output is shown. Structural compliance (word count, no em dashes, CTA
placement) is **not** evidence of passing this gate. They are two different bars.

### Mechanical rules for "sounds internal"

- **No rhetorical antithesis** ("X sees A. It doesn't see B", "Not X, but Y").
- **No repeated-verb-for-rhythm** ("stretches, and the revenue stretches with it").
- **No abstract categorical description of the hire** ("every engineer who moves to New York").
  Refer concretely: "the new hire", "this role", "the [specific title]".
- **No fully-formed explanatory clauses where a person would state the fact plainly.** "No salary on
  the JD, so based it off market rate" beats "so that's the market benchmark rather than your own number".
- **Casual connectors** ("but if", "so", "that's"), not formal ones.
- **Short and choppy over smooth.** If it reads polished, that is the tell.
- **Never assert relocation as fact in Email 1.** Hedge it ("if they're coming from out of market").
- **Gap/difference recovery needs an explicit "you" as actor.** "You can get that $104K difference
  back", never "that difference comes back" or "is recoverable".
- **Naturalize JD-formatted titles.** "Senior Director, Product" -> "Product Sr. Director". Applies to
  whichever title reaches the copy, not just the driving role.
- **Abbreviation test: would a real person say it?** "SWE", "Eng", "Sr. SWE" are fine; one choice per
  email, don't mix. "SWE Dir" is not. "Data Analytics Director" is fine. The test is recognizability,
  not a count of what is abbreviated.
