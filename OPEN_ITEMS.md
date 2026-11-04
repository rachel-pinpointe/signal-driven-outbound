# Open Items — carried forward

Logged 2026-08-31 at the close of the 27-company batch. Not acted on.

---

## 1. LinkedIn-based QuickEnrich lookup never completed

Higher-ranked contacts surfaced by the `has_email` correction are still unresolved. Each has
a LinkedIn URL on their QuickEnrich record but no email on file, and the Email Search by
`linkedin_url` path was documented but never run.

Eight such contacts across three companies, at Head-of / Lead / Manager level in recruiting,
people operations and finance. (Names removed from the public copy.)

These outrank the contacts currently sitting in `quickenrich_contacts.csv` as primaries.
Until they're resolved, that file's primaries are the best *emailable* contact, not the best
contact.

## 2. Entry-level disqualifier never applied to Clay and Rillet

Both were scored on NYC role volume (Clay 46 of 64, Rillet 8 of 32) without checking that
volume against the entry-level disqualifier in the real Fibery Email Plays canon. If either
skews junior — SDR/BDR, coordinator, associate, intern, generally under $150K base — the
signal they were scored on is undercut and their tier should be revised down.

## 3. Messaging sent to Apollo is close to canon, not exact

Sequences already pushed to Apollo do not hit the real canon doc on:
- specific word-count targets
- case-study-to-role matching
- CTA-on-its-own-line formatting

Close, not exact. Needs a pass against the canon before the next batch reuses those templates.

## 4. Aug 28 2026 alternate — RESOLVED 2026-09-09

Verified directly against Email Plays (view 480). The change landed, but not as recorded:

- The **CFO/Finance play was rebuilt in September 2026**. The rebuilt version is live.
- The **old hidden-tax main framework** and the **Aug 28 2026 alternate** both sit under
  `# Archived Plays`, the alternate nested inside the archived CFO play. Neither is
  draftable. The archived alternate still carries the retired "30 days becomes about 5"
  turnaround promise.
- **There is no Recruiter/TA alternate**, live or archived. That half of this item's premise
  was never true.

CLAUDE.md's Personas & Plays section has been corrected accordingly (it previously told the
drafter to treat the archived alternate as live canon, and attributed the $701,404 figure to
CFO/Finance rather than Global Mobility).

Still open from this thread: `fibery_docs/email_plays.md` is stale — see item 6.

## 5. Live People Ops/HR "Aug 28 2026" alternate — undocumented, two defects unresolved

Found while verifying item 4. Not the same artifact as the archived CFO alternate.

Email Plays (view 480) carries a **live** `### Alternate — field-tested Aug 28 2026` under
the **People Ops / HR Leader** play. Neither CLAUDE.md nor any prior open item mentions it.

Its subject rotation contains two options the doc itself flags as broken:

| Subject option | Problem |
|---|---|
| "new hire flight risk" | 4 words, exceeds this persona's 3-word cap — annotated "flagged not resolved" |
| "new hire pulled out" | 4 words, same cap breach — annotated "flagged not resolved" |

Both are annotated in the doc as flagged-but-not-resolved, so this is a known gap upstream,
not a transcription error. Deliberately **not** fixed here — decide whether to shorten them,
drop them from the rotation, or lift the 3-word cap for this play.

## 6. fibery_docs/email_plays.md stale — RESOLVED 2026-09-09

Regenerated from the live Fibery doc (Email Plays, view 480) via Fibery MCP. The stale May 3
export was replaced entirely.

- 24,647 bytes -> 47,049 bytes (782 lines)
- Live **CFO / Finance Leader** (rebuilt) at line 276, in the main `# THE PLAYS` section
- `# Archived Plays` at line 682, containing the old hidden-tax main framework (line 686)
  and the **Aug 28 2026 alternate** nested under it (line 740)
- The separate live **Aug 28 2026 alternate** under People Ops / HR Leader is at line 228
  (see item 5 — still unresolved, deliberately not touched)

Verified after writing: heading order matches the live doc, no truncation, markdown escapes
(`\~80 words`, `\[Company\]`) and template placeholders (`{name}`, `${amount}`) all intact.

The MCP tool's metadata wrapper (`**Type:** / **Space:** / **Description:**`) was dropped so
the file matches the convention used by the other six cache files. All doc body content was
kept verbatim.

## 7. Six remaining fibery_docs/ cache files stale — RESOLVED 2026-09-10

All six were regenerated from live Fibery via MCP on 2026-09-10, and the session sync pause is
lifted. **They did not all close on the same footing** — four are reconciled, two are only
refreshed.

### Fully reconciled — Fibery checked against the skill docs, fixes applied live, then cached

Reconciled on 2026-09-10. Each of these had real corrections made *in Fibery* before the pull,
so the cache is current against a source that has itself been verified.

| File | Fibery view | Status |
|---|---|---|
| icp_and_qualification.md | 476 | reconciled + current |
| buyer_personas.md | 477 | reconciled + current |
| messaging_and_positioning.md | 478 | reconciled + current |
| research_and_stats.md | 479 | reconciled + current |

Fixes that landed in Fibery during the reconciliation: Enterprise ICP tier removed; CFO tone
and numbers corrected; DSP language pulled; the retired 5-day turnaround promise stripped
everywhere except the website hero example; Home First reframed as internal-only; and a
mislabeling bug in Research and Stats where the DIY-relo scenario was backwards-labeled as
NYCbound-supported.

### Refreshed but NOT reconciled — no skill counterpart to check against

| File | Fibery view | Status |
|---|---|---|
| case_studies.md | 487 | refreshed 2026-09-10, unverified |
| sales_call_patterns.md | 481 | refreshed 2026-09-10, unverified |

There is no skill reference file for either doc, so there was nothing to reconcile them
against. They are current *with Fibery* as of 2026-09-10 and nothing more. Do not describe
them as verified. If a source of truth for either ever exists, they need their own
reconciliation pass.

`email_plays.md` (view 480) remains current from the 2026-09-09 regeneration (item 6).

**Sync pause lifted.** The session-start sync was restored in all four places it had been
disabled: CLAUDE.md's START OF SESSION block, its Known Issues line, its file-structure tree
entry for `sync_fibery.py`, and the two pause banners in `sync_fibery.py`. The condition that
justified the pause — Fibery possibly being behind the skill docs — no longer holds for the
four reconciled files, and was never claimed for the two that have no counterpart.

### Residual discrepancies noticed during the pull (not fixed, decide separately)

Flagged only; nothing was edited in Fibery or in the cache to paper over these.

1. **A 5-day claim survives outside the website hero.** `messaging_and_positioning.md`
   Competitive Positioning table, DIY (StreetEasy) row: "\~5 days, expert navigation." The
   hero example on the same doc is the sanctioned exception; this table row is a second one.
2. **DSP language is still in Messaging and Positioning** — 8 mentions, including the full
   "Not Destination Service Providers (DSPs)" section and the DSP competitor row. If the
   intended pull was broader than the category-hook bullet, this doc still needs it.
3. **Enterprise tier removal is inconsistent across docs.** ICP and Qualification no longer
   defines an Enterprise tier, but Buyer Personas still opens with "**Enterprise:**
   2,001–10,000 employees (Secondary ICP, warm intro only)" and tags four personas to it.
4. **email_plays.md contradicts itself on the retired promise** (out of tonight's scope, view
   480). Line 342 records the company-wide retirement of the turnaround-time promise, while
   lines 436, 732, and 778 still use "30 days → 5 days."


---

# SESSION HANDOFF — 2026-09-11 (close-out)

## Delivered this session

- **`outputs/cfo_sequences_36_20260911.csv` is done.** Full 3-email CFO sequences for all 36
  qualified contacts. Columns: Full Name, Title, Company, URL, Email, Subject Line, Email 1,
  Email 2, Email 3. One row per contact. Every email passes the Messaging Quality Gate and the
  play's structural rules; every company's shorthand figures subtract to the stated gap.
  Rogo, Hex Technologies and Formation Bio are their approved finals, reproduced verbatim.
- **The scope doc is now the governing spec.** See `# PIPELINE SCOPE (v2) — GOVERNING SPEC` at the
  end of CLAUDE.md, with the pointer near the top. It outranks older sections where they disagree.
  **Every messaging pass must apply its Messaging Quality Gate, per email.**

## Dataset state (frozen batch of 83)

- 57 qualified. 36 with a contact, 21 without.
- Of the 36: 26 QuickEnrich pre-verified, 10 Apollo/Hunter **Scrubby-verified 2026-09-11** (all 10
  passed, all 10 domains confirmed non-catch-all by control test).
- Cityblock Health and Cryptio were removed from the qualified set on request. Backups of the
  pre-removal and pre-merge states are in `outputs/final_v2_backup_20260911*/`.
- Contact-only enrichment on the 21 no-contact companies resolved 0 and cost 0. They are absent
  from QuickEnrich's finance dataset; Hunter only had sub-tier titles (Controller, Bookkeeper,
  Accountant).
- Provider balances at close: QuickEnrich 164 credits, Apollo 421 lead credits, Hunter 30 searches,
  **Scrubby 0 credits** (20 spent, needs a top-up before any further verification).

## Next up: infrastructure, in this order

1. **Persistent cache first.** Date-stamped, split by layer (company research vs contact-per-persona).
   **Build and validate it alone, before touching anything else.**
2. **Navigation-based discovery** replacing guessed URLs. **Validate against the known hand-verified
   sample before going live** (Legora, Polymarket, ElevenLabs, Encord, Rogo, Method Security, Stuut,
   Cityblock, Nimble, Qodo, Truemed, GitGuardian, and the disqualified set).
3. **Then the smaller items:** the "Associate" entry-level classifier bug, the domain-mismatch
   heuristic, Scrubby as a standing pipeline step, the Apollo wiring audit (dry-run never reaches
   Apollo, so verify against a real run), and the title-classifier regression suite.

   **Apollo wiring audit — RESOLVED 2026-09-16.** Verified live, not just read from code. The
   dry-run path was confirmed to never reach Apollo by design (both Stage A discovery and Stage B
   enrichment return before any Apollo call when `dry_run=True`) -- that was always correct
   behavior, not a bug. Apollo itself had never actually been exercised: a real test call confirmed
   `/people/match` (email reveal) works, and `/mixed_people/search` (the newer discovery-mode call
   added the same day) failed with HTTP 403 -- the key's scope didn't include it.

   **Same day, Rachel upgraded the API key to master access.** Re-tested live: the 403 cleared, but
   Apollo has since deprecated `/mixed_people/search` (HTTP 422, "please use mixed_people/api_search
   instead"). Fixed and shipped: `apollo_people_search()` now hits the real replacement endpoint. Its
   response shape also turned out different from what was assumed -- it's search, not reveal, with an
   obfuscated last name and no email/LinkedIn -- but it does return Apollo's own internal person `id`,
   now wired as a precise handoff into Stage B's reveal (`apollo_match(apollo_id=...)`) instead of a
   fuzzy name+domain match. Verified live end to end against microsoft.com: discovery found 10
   candidates, the first resolved to a real verified email with its correct unobfuscated name.
   Apollo discovery is now fully working -- this line item is closed, nothing further pending.

## Open item to revisit next session

**Imprint's value factor is the loosest call in the set.** It is mapped 5.4x on "Head of Risk Product
Strategy," extended from the Formation Bio VP-Product precedent. Product leadership at Formation Bio
is a direct match to the scope doc's "senior product leadership" row; a *risk* product strategy lead
is a longer analogy and could reasonably sit at 3.0x. At 3.0x, Imprint's figures fall from
$1.282M / $1.125M / $157K / $338K to roughly $712K / $605K / $107K / $71K. **Treat 5.4x as provisional,
not settled.** Two adjacent calls made the same way and worth the same second look: Sales Engineer
(Valon) and Solutions Engineer (Lithic) at 5.0x rather than 5.4x.

## Where things live

- Final CSV: `outputs/cfo_sequences_36_20260911.csv`
- Draft history, v1 through v6: `outputs/cfo_sequences_test_batch_20260911*.md` (v6 is current for
  the three calibration companies)
- Generators and intermediates copied out of the session scratchpad:
  `outputs/generators/` (`copy36.py` is the pre-patch template set, the CSV is authoritative)
- Contact-only enrichment run: `outputs/contact_only_20260911/`
- Fibery cache: `fibery_docs/`, all 7 files re-synced from live Fibery at session start

---

# SESSION HANDOFF — 2026-10-02 (close-out)

Phases 17-20 built and committed. **340 tests passing** (from 119, of which 11 were erroring in a
suite believed green). 14 commits in `nycbound-gtm` and 3 in `hypersignal`, **none pushed**.
Full root-cause detail for each phase is in `CLAUDE.md`'s Known Issues / History.

## Newly open — nothing here was acted on

1. **The Anthropic pricing table is unconfirmed.** `ANTHROPIC_PRICING` in `providers.py` is seeded
   with Sonnet at 3.00/15.00 per Mtok and web search at $0.01/request. Plausible, not verified
   against current pricing. Every cost figure the pipeline reports is only as right as that table,
   and it is deliberately plain editable values for exactly this reason.
2. **Unattributed Anthropic traffic on this key.** Haiku + Sonnet requests at ~3:00pm and ~3:29pm
   ET on 2026-10-02, after the cron ended at 2:27pm. Ruled out comprehensively inside this repo and
   on this machine (see CLAUDE.md) — nothing here can even emit a Haiku call, since the model is
   pinned to Sonnet. **Needs the Anthropic Console request log** (per-request model/workspace/key),
   and a check of whether the key is installed elsewhere: another machine, a server, a connector or
   an IDE extension.
3. **Two of the four failed enrollments will likely fail again.** Contacts C and D
   are `email_true_status: "User Managed"`, which is deliberately *not* skipped — a
   human set those addresses on purpose. They will be retried and may be declined again. The
   difference is that Apollo's response body now lands in `enroll_failures_YYYYMMDD.log`, so the
   next failure is diagnosable instead of a guess. **Read that log after the next run.**
4. **`fibery_docs/` was NOT refreshed this session.** CLAUDE.md's START OF SESSION sync never ran —
   the session went straight into implementation. The cache is still at its 2026-09-09/09-10 state.
   Anything drafted from it is drafting from that snapshot, not from live Fibery.
5. **The two non-CFO persona title ladders remain unreviewed.** `persona_people_ops.py` and
   `persona_recruiter_ta.py` are drafts built from the Persona Profile PDFs, flagged for Rachel's
   review since Phase 8 and still unreviewed — while both personas run live every day.
6. **Repo cruft, untouched on purpose.** `_to_delete/`, `_to_delete_gitlock_1789851789` and
   `Claude outputs/` are untracked and were left alone all session. `hypersignal` also carries
   three unrelated dirty files (`.gitignore`, `package-lock.json`, `data/webappanalyzer.json`) that
   were deliberately kept out of every commit.

## Worth knowing about how this session went

- **A stale `.git/index.lock` + `HEAD.lock` blocked the first commit attempt in both repos** (dated
  Sep 19 and Sep 21, zero bytes, no git process running — the leftover `_to_delete_gitlock_*` file
  suggests a recurring pattern). Cleared after confirming staleness. Expect it again.
- **Two commits' worth of pre-existing uncommitted work had to be landed as baselines first**
  (Phase 14 python, and the Phase 14 cron work in `hypersignal`) because the new work edited the
  same files. Their messages say so.
- **A guard caught two red suites before they could be committed.** Both were my own test defects,
  not source bugs. Note that `grep -E "FAILED"` exits 0 and so does *not* gate a commit — the
  commit chain now gates on `unittest`'s real exit code.
- **A test was writing into the real spend log.** `test_preflight_anthropic` stubs its response with
  a `MagicMock`, and `int(MagicMock())` is 1, so it wrote six `in=1 out=1 searches=1` lines of
  invented cost into `outputs/spend_20261002.log` before being caught and fixed (`5ce7b68`).
