# CFO / Head of Finance Lead Enrichment Pipeline
**Project:** `nycbound-gtm` (Claude Code) — builds on the existing String test in this project, not a new project.

## Purpose

Given a list of target companies, produce a reviewed spreadsheet of Head of Finance / CFO contacts with verified emails, plus job-description-derived personalization data, feeding the CFO / Finance Leader play (Fibery Sales Intelligence, Email Plays doc). Output is a review layer, not an auto-send pipeline.

## Input

A list of companies (name + domain), supplied per run. Not hardcoded, this script needs to accept a new list each time it's run.

## Step 1 — Contact discovery: Head of Finance / CFO

For each company, find the most senior finance contact, title priority in this order: **CFO → Head of Finance → VP Finance → Finance Lead**. Don't substitute a non-finance contact if none of these exist, flag and skip that company instead.

**Enrichment waterfall, stop at first successful match:**

1. **QuickEnrich** (primary). 234 credits available as of this run, track remaining balance as the script runs and warn before it would exhaust the pool.
2. **Apollo**, via Apollo MCP (`apollo_mixed_people_api_search` to find the person, `apollo_people_match` to enrich). Apollo enrichment consumes credits per match, confirm total projected cost against the company list size before running the batch, not after.
3. **Hunter**, via API key. Check `.env` for the Hunter key before assuming it's available, don't hardcode an assumption it exists.

**Output per contact:** name, title, verified email, LinkedIn URL if available, which tool found it, verification/confidence status.

## Step 2 — Job description research

**Goal:** find the company's actual open NYC-relocatable roles and salary, so Email 2 of the CFO play can cite a real number instead of the generic $165K SWE default. This is the play's own required pre-research step, not a nice-to-have.

**Method:**

Go directly to the company's website and find their actual careers/jobs page. Don't guess or probe across ATS platforms. Use String's **raw API** for this, not the MCP tool, this is a deterministic batch job across a fixed company list, and the established learning from this project's own String test is MCP for exploratory discovery, raw API for repeatable batch scripts.

1. Fetch the company's website.
2. Locate the careers/jobs page from site navigation (common patterns: `/careers`, `/jobs`, `/join-us`, or a nav link labeled "Careers" or "Jobs").
3. Extract open NYC-based roles and salary directly from that page. If the careers page happens to be hosted on Greenhouse, Lever, or Ashby, that's fine, it was found by following the company's own site, not by testing each ATS platform against every company blind.
4. No careers page found, or the page lists no NYC roles: flag as "no signal" per the gating rule below, same as before.

**Extract:** open NYC-based roles, role title, salary if posted publicly. If no salary is posted, map the role to the nearest Research and Stats role band instead of leaving it blank:

| Role | Base | 
|---|---|
| Software Engineer | $165K |
| GTM Lead | $111K |
| Chief of Staff | $157.5K |
| Finance Lead | $152K |

If no NYC-based open roles are found at all, flag the account as "no signal" rather than forcing the calculator numbers. That account isn't a fit for this play regardless of contact quality.

**Also extract the company's stated goal for the hire** — same required pre-research discipline as the salary lookup, not optional. Pull whatever real signal exists: job-posting language (new function, team expansion, backfill vs. growth), recent funding news, or a product launch driving the need. This feeds the CFO play, Ramp Risk, and any future goal-tied play — it can't just live as a manual reminder in a Fibery doc. No goal signal found after a real check: note it plainly ("no stated-goal signal found") rather than leaving the field blank or inventing one.

## Step 3 — Output

**A) Spreadsheet — always produced, this is the review gate.**

One row per contact:

| Company | Contact Name | Title | Email | Email Source/Confidence | LinkedIn | Open NYC Role(s) | Role Band Used | Signal Notes |

**B) Apollo — only after Rachel reviews and approves the spreadsheet. Never automatic.**

Once approved: `apollo_contacts_bulk_create` to add contacts, then `apollo_emailer_campaigns_add_contact_ids` to enroll in the target CFO sequence. This is a distinct, explicitly-triggered step, not something that fires automatically at the end of Steps 1–2.

## Non-negotiable safety notes

- **Finding and researching contacts is safe to automate. Enrolling them in a live Apollo sequence is not**, that's a real send action once the sequence fires. Steps 1–2 can run unattended on a batch. Step 3B requires an explicit go-ahead after the spreadsheet is reviewed.
- Check credit balances (QuickEnrich, Apollo, Hunter) before running a batch, report projected cost before running, not after. Don't burn through the 234 QuickEnrich credits on an unbounded first test list.
- Skip and flag companies with no finance-function contact, no substitutions.
- Skip and flag companies with no NYC hiring signal, don't force the calculator numbers to apply anyway.

## Open items to confirm before build starts

- Confirm what's actually in `nycbound-gtm`'s `.env` (Apollo, Hunter, String, QuickEnrich, Scrubby keys, whatever's really there vs. assumed).
- Confirm the target Apollo sequence exists, or needs to be created first with the CFO Recover play's copy loaded into it.
- Confirm the format and size of the first test company list before running Step 1 at volume.
