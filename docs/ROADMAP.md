# nycbound-gtm — Build Roadmap

Systemization plan for this pipeline, tracked in phases. Each phase is scoped before it's built
(goal, happy path, edge cases, approach) and gets explicit sign-off before implementation starts.
Full history and background research lives in the "Sales & Marketing" Claude project; this file is
the durable, in-repo summary — current state and status only, not the scoping narrative.

## Status

1. **Signal-watching architecture — LOCKED.** Fibery "GTM Pipeline" space built: Companies /
   Contacts / Signals databases, reply-gated manual promotion to the CRM (never automatic, never
   gated on "qualified" alone), 20-company calibration set drawn from the existing 83-company
   batch, 60-day contact re-sequence cooldown.
2. **GitHub repo setup — DONE.** Private repo, `.gitignore` hardened to catch loose top-level CSVs
   (several contained real contact data that the original `.gitignore` missed), roadmap written
   into the repo itself.
3. **Layer 1 / Layer 2 refactor + persistent research cache — DONE.** `company_research.py`
   (Layer 1, persona-agnostic) extracted from the old CFO-only `cfo_jobs.py`. `src/cache.py`: a
   SQLite cache split by layer (company research, contacts), 14/90-day freshness. Real second-hop
   navigation discovery. Scrubby wired as an opt-in standing verification step (`--scrub`). Fixed
   the "Associate" entry-level classifier bug and the domain-mismatch heuristic (both have a real
   regression suite, `tests/test_classifiers.py`). `src/personas.py`: a persona registry scaffold —
   CFO live, HR/People Ops (Ramp Risk) and Recruiter/Talent Acquisition as clean placeholders.
   **Superseded by Phase 8: all three personas are live.** The two non-CFO title ladders are drafts
   built from the Persona Profile PDFs and have never been reviewed by Rachel — see their module
   docstrings.
4. **Signal-watcher build — BUILT.** HyperSignal (vendored, `hypersignal/`) cloned and configured
   on Rachel's Mac, watching the 20-company calibration set. `signal_ingest.py` pushes new signals
   from `hypersignal/data/signals.db` into Fibery. Weekday 1pm ET scheduled task
   (`hypersignal/run-cron.sh`). Originally bound to Rachel's Mac via a Cowork scheduled task — not
   raw crontab. **As of Phase 13 (2026-09-18), moved off that bridge onto a native macOS launchd
   job** (`com.nycbound.hypersignal-cron.plist`), since the bridge capped every command at ~180
   seconds and ran inside an isolated Linux VM — neither fits a run against the full
   ~1,445-company watchlist. `src/promote_to_crm.py` (item 6) is unaffected and still runs as a
   Cowork scheduled task.
5. **Free-provider adoption + Layer 2 waterfall generalization — BUILT.** jina + HyperSignal folded
   into Layer 1 fetch; Layer 2 split into two shared, persona-agnostic waterfalls (contact
   discovery: QuickEnrich → web_research → Apollo; contact enrichment: QuickEnrich → Hunter →
   Apollo) with real vendor budget caps tracked in `CreditLedger`; stated-goal research built as
   verified search-and-cite (`research_stated_goal()` — quotes an excerpt, then actually fetches
   the cited URL to confirm it's really there before accepting the finding). `cfo_providers.py`
   renamed `providers.py` since it was already persona-agnostic. A bug found during this phase's
   build (`extract_salary()` grabbing the wrong dollar figure from a long job body) was fixed the
   next day, with its own regression suite (`tests/test_salary_extraction.py`).
6. **Reply-gated CRM promotion — BUILT.** `src/promote_to_crm.py`: for each Contact marked
   `Promote Requested` (a native Fibery button, Rachel's manual judgment call), resolves or creates
   the linked CRM Account (company-level dedupe so two contacts at one company in the same run
   never create two Accounts; flips `Prospect` → `Engaged`, leaves anything past that or
   `Dormant`/`Lost` untouched) and the linked CRM Contact (`Source=Outbound`, `Type=Corporate
   Client`, `workflow/state` never touched). Runs hourly, 9am-6pm weekdays, as its own Cowork
   scheduled task — deliberately separate from `run-cron.sh`. (Scoped for every 30 minutes; shipped
   hourly because the scheduling platform enforces a 1-hour minimum interval, discovered when
   wiring it up.) `tests/test_promote_to_crm.py` — 10 tests, no live Fibery calls.
7. **Apollo wiring audit — DONE.** Confirmed dry-run never reaches Apollo (by design, not a bug).
   Found and fixed a deprecated-endpoint issue (`/mixed_people/search` → `/mixed_people/api_search`)
   plus a key-scope gap; wired a precise id-based Stage A → Stage B handoff. All 3 Stage A rungs and
   all 3 Stage B rungs confirmed working against live calls.
8. **Ramp Risk + LinkedIn Play 5 copy finalization, plus Recruiter/TA play copy.** Not yet scoped —
   but note the personas themselves are **no longer placeholders**: all three run live every day
   (Phase 8), drafting from live plays in `fibery_docs/email_plays.md`. What is still outstanding is
   narrower and worth stating plainly: the People Ops and Recruiter/TA **title ladders** in
   `persona_people_ops.py` / `persona_recruiter_ta.py` were drafted from the Persona Profile PDFs
   and have never been reviewed by Rachel, while both personas have been picking real contacts for
   real sends since Phase 8.
9. **Company intake + paid-gate + Fibery write-back — BUILT.** Fibery's `GTM Pipeline/Companies` is
   now the two-way source of truth: `paradigm_import.py` imports and dedupes Paradigm exports (CRM
   cross-check, domain dedupe, Name-match safety net); `sync_watchlist.py` keeps HyperSignal's
   watchlist in sync with Fibery's active companies; `main.py --input fibery` queries live and
   writes research/contact results back. The paid gate (status + 60-day recent-outreach cooldown)
   runs before any company enters Stage A/B, regardless of input source. `src/fibery_client.py`
   extracted as the shared REST client every Fibery-talking script now uses.
10. **README / architecture map — DONE.** This file plus `README.md`'s Mermaid diagram, covering
    all three systems (signal watching, intake/enrichment, CRM promotion) and how they connect.
11. **Small-batch test run — DONE.** Every rung of both waterfalls, the paid gate, write-back and
    persona-agnosticism exercised against real batches (the staged Apollo rollout in Phase 12 was
    the narrowest of these: 1 contact, then 1 company, then several).
12. **Full end-to-end run + full Paradigm import — DONE.** The Paradigm list was bulk-imported
    (and the import's own dedupe bugs found and fixed in the process — 28 duplicate company records,
    see CLAUDE.md). The pipeline now runs the whole way through unattended on the weekday cron:
    the 2026-10-01 run processed **1,284 companies** against **1,464 tracked in Fibery**, through
    research, contact discovery, Fibery write-back and Apollo enrollment.
13. **HyperSignal cron migrated off the Cowork bridge onto native launchd — DONE (2026-09-18).**
    See item 4. Same weekday 1pm ET schedule, same step sequence and safety scaffolding (lock
    file, `caffeinate` tied to the script's own PID, 24h watchdog, Slack success/failure alerts).
    Also added: a `SKIP_RESEARCH` env-var gate so `run-cron.sh` can do a hiring.cafe-only
    signal-fetch pass (sync + job cycle + signal scan + Fibery push) without also triggering
    `main.py`'s research pipeline and Apollo enrollment in the same invocation — used for the
    initial full ~1,445-company watchlist catch-up run.
14. **Messaging-quality hardening pass — DONE (2026-09-19).** Found and fixed during dry-run
    review ahead of the first full-watchlist run: CFO emails could still leak exact dollar figures
    instead of the required shorthand; non-CFO plays (People Ops, Recruiter/TA) could fabricate a
    case study or fall back to vague "Director-level" language instead of naming the real driving
    role; `apollo_enroll.py` carried a leftover testing-only 12-contacts-per-run cap. All four
    fixed and verified against real dry-runs — full root-cause detail in `CLAUDE.md`'s Known
    Issues / History. `main.py`'s real per-run throttle (`--daily-cap`, default 25 companies) is
    unchanged; Apollo enrollment is no longer separately capped.

    *(Numbering follows phase numbers, so 15 and 16 are unused.)*
17. **Dedupe + write guards — DONE (2026-10-01/02).** `providers.same_person()` (nickname-, initial-
    and LinkedIn-aware) applied at four sites, because the real duplicate pairs carry *different*
    emails and an email check cannot see them. Deliberately conservative against merging: a missing
    last name is never enough, since a false merge silently drops a real person. Plus
    `email_domain_matches_company()` (never rejects on absent data), a cross-record email guard, a
    manual-review log for every declined write, and the Stage A waterfall stopping at the first
    rung that yields a qualifying candidate instead of paying later rungs to top up to three.
18. **Cron hardening — DONE (2026-10-02).** `main.py` internal deadline (`MAIN_PY_DEADLINE_SEC`,
    1500s, 300s under the outer time-box) checked before starting each company, SIGTERM handler,
    flushed per-company spend log, `PYTHONUNBUFFERED=1` for both python steps, and a fixed `STEP`
    label so a `main.py` timeout no longer reports the preflight's name. Also a 25-distinct-
    companies-per-day enrollment cap (`cache.enroll_daily_runs`) — companies, not contacts.
19. **Anthropic cost tracking — DONE (2026-10-02).** Every call tagged by site (research, extract,
    likely-priorities, ghost-email, preflight) with tokens and web-search count, estimated from an
    editable rates table in `providers.py`, reported per site in the end-of-run summary and the
    SIGTERM handler. Tracking only — no ceiling. **The rates are placeholders pending confirmation.**
20. **Enrollment-failure diagnostics — DONE (2026-10-02).** Apollo's own response body now travels
    out with a failed `add_contact_ids` into both the failure message and
    `enroll_failures_YYYYMMDD.log`, and a contact is skipped when `email_true_status` is
    "Potentially Invalid" (not "User Managed"). Came out of four failures where Apollo returned
    HTTP 200, silently declined, and the body was discarded — see CLAUDE.md.

## Working rules

- Real prospect/contact data never enters this repo. `.gitignore` excludes `.env`, `outputs/`, all
  `*.csv`, and the venv folders. Repo stays private indefinitely; if a portfolio/showcase version is
  ever wanted, it's built as a separate, deliberately-scrubbed repo, not by making this one public.
- Company research (Layer 1) is persona-agnostic and never changes based on which play is being
  run. Contact-finding and messaging (Layers 2/3) are persona-specific and always come after.
- A goal-tied play (CFO, Ramp Risk, or any future one) requires real pre-research on the company's
  stated goal for the hire before messaging — same discipline as the salary lookup, not a manual
  reminder.
- CRM promotion is reply-gated, never automatic and never triggered by qualification alone — a real
  human (Rachel) judging a real reply is what starts it, every time.
- Scheduled, recurring jobs stay visible and editable rather than living only in a terminal's
  crontab. **Two mechanisms as of Phase 13:** `hypersignal/run-cron.sh` runs as a native macOS
  **launchd** job (`com.nycbound.hypersignal-cron.plist`), having outgrown the Cowork bridge's
  ~180-second command cap; `promote_to_crm.py` is still a **Cowork scheduled task**. There is still
  no raw crontab entry for either.
- Cost/usage tracking never gates a run. Ledgers and logs record what happened; they do not decide
  anything. A bug in observability must not be able to fail a run that otherwise worked, which is
  why the Anthropic recorder is wrapped so it cannot raise.
