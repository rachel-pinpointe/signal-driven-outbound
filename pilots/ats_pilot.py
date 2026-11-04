"""Phase 21 ATS-coverage pilot (2026-10-02). Measurement only -- writes nothing to Fibery, never
touches the live hypersignal/data/signals.db, and is not wired into any pipeline.

Per sampled company, stops at the first VERIFIED board, in this order:
  (a) candidate generator v2 -- Fibery company name + domain stem, with prefix/suffix and hyphen
      variants, probed against Greenhouse, Lever, Ashby, Workable and SmartRecruiters;
  (b) careers-page extraction -- ATS links/embeds read from the company's own homepage and the
      careers pages it links to (plain HTTP, then free jina; never paid String);
  (c) web-search lookup via the existing providers._llm_web_search wrapper, hard $2 ceiling
      enforced against the existing CreditLedger Anthropic cost tracking.
Every candidate board, whatever its source, is verified by HyperSignal's own verifyBoard through
pilots/ats_pilot_verify.mts, run against a COPY of signals.db.

    source .venv/bin/activate
    python pilots/ats_pilot.py --signals-copy /path/to/signals_copy.db [--search-ceiling 2.00]

Outputs: outputs/ats_pilot_20261002.csv (one row per company) and
outputs/ats_pilot_20261002_probes.csv (every probe that reached a live board: verified, rejected,
or empty).
"""

import argparse
import csv
import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src import fibery_client as fc  # noqa: E402
from src import providers  # noqa: E402

SEED = 20261002
N_PENDING = 50
N_TIER1 = 10
VENDORS = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
# Workable 429'd at 2-concurrent on the first run (2026-10-02), so it is probed only in a separate,
# paced final pass (1 request every 3s, stop Workable alone after three 429s in a row).
MAIN_VENDORS = [v for v in VENDORS if v != "workable"]
LIVE_DB = ROOT / "hypersignal" / "data" / "signals.db"
BRIDGE = ROOT / "pilots" / "ats_pilot_verify.mts"
TSX = ROOT / "hypersignal" / "node_modules" / ".bin" / "tsx"
OUT = ROOT / "outputs" / "ats_pilot_20261002.csv"
OUT_PROBES = ROOT / "outputs" / "ats_pilot_20261002_probes.csv"
CALL_SITE = "ats-pilot-search"
PROBE_CACHE = ROOT / "outputs" / "ats_pilot_20261002_probe_cache.jsonl"     # resume: reused vendor answers
SEARCH_CACHE = ROOT / "outputs" / "ats_pilot_20261002_search_cache.json"    # resume: never re-pay a search

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# --------------------------------------------------------------------------------------------
# (a) candidate generator v2

LEGAL = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc|technologies|technology)\.?$", re.I)
PREFIXES = ("get", "try", "join", "use", "with", "go", "hey", "meet")
SUFFIXES = ("ai", "hq", "inc", "labs", "app", "io", "co")


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def candidate_slugs_v2(name: str, domain: str, cap: int = 30) -> List[str]:
    stem, _, tld = domain.lower().partition(".")
    tld = tld.split(".")[0]
    name = LEGAL.sub("", (name or "").strip()).strip(" ,.")
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    out: List[str] = []

    def add(s: str) -> None:
        s = s.strip("-")
        if len(s) >= 3 and s not in out:
            out.append(s)

    # 1. the bases: domain stem, its de-hyphenated form, the name joined and hyphenated
    bases = [stem, stem.replace("-", ""), "".join(words), "-".join(words)]
    for b in bases:
        add(b)
    # 2. the TLD folded in ("sola.ai" -> "solaai", "sola-ai")
    if tld in SUFFIXES + ("xyz", "so", "dev", "us", "health", "bio"):
        add(stem + tld)
        add(stem + "-" + tld)
    # 3. strip a marketing prefix/suffix the domain carries ("withwisdom" -> "wisdom")
    core = {_fold(stem), "".join(words)}
    for b in list(core):
        for p in PREFIXES:
            if b.startswith(p) and len(b) - len(p) >= 4:
                add(b[len(p):]); core.add(b[len(p):])
        for x in SUFFIXES:
            if b.endswith(x) and len(b) - len(x) >= 4:
                add(b[:-len(x)]); core.add(b[:-len(x)])
    # 4. add the common prefixes/suffixes to each core form, joined and hyphenated
    for b in [c for c in core if c]:
        for x in ("ai", "hq", "inc", "labs", "app"):
            add(b + x)
        for p in ("get", "try", "join"):
            add(p + b)
        for x in ("ai", "hq", "labs", "app", "inc"):
            add(b + "-" + x)
    return out[:cap]


# --------------------------------------------------------------------------------------------
# (b) careers-page extraction

ATS_URL_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("ashby", re.compile(r"posting-api/job-board/([A-Za-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"\b([A-Za-z0-9-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)),
]
EMBED_MARKERS = [("ashby", re.compile(r"ashby_embed", re.I)), ("greenhouse", re.compile(r"grnhse_app|grnhse_iframe", re.I)),
                 ("lever", re.compile(r"lever-jobs-container", re.I))]
SLUG_JUNK = {"embed", "api", "v1", "jobs", "job", "apply", "www", "careers", "j", "o", "static", "assets", "js", "css"}
CAREERS_LINK = re.compile(r"career|jobs|join|work-with-us|workwithus|hiring|open-roles|openings|positions", re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def _get(url: str, timeout: int = 12) -> Tuple[Optional[str], str]:
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        return (r.text if r.ok else None), (str(r.status_code) if not r.ok else r.url)
    except requests.RequestException as e:
        return None, type(e).__name__


def extract_ats(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    found: List[Tuple[str, str]] = []
    for vendor, pat in ATS_URL_PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).strip(".")
            if slug.lower() in SLUG_JUNK or (vendor == "workable" and slug.lower() in ("apply", "www", "jobs")):
                continue
            if (vendor, slug) not in found:
                found.append((vendor, slug))
    markers = [v for v, p in EMBED_MARKERS if p.search(text)]
    return found, markers


def careers_page_candidates(domain: str, ledger) -> Dict[str, Any]:
    """Real navigation: homepage -> its own careers-ish links (max 3) -> any ATS link/embed found."""
    log: List[str] = []
    pages: List[Tuple[str, str]] = []
    home, info = _get("https://" + domain)
    if home is None:
        home, info = _get("https://www." + domain)
    log.append("home:" + ("ok" if home else info))
    if home:
        pages.append((info, home))
        base = info
        links = []
        for href in HREF.findall(home):
            u = urljoin(base, href)
            host = urlparse(u).hostname or ""
            if CAREERS_LINK.search(u) and (host.endswith(domain) or any(v in host for v in ("greenhouse", "lever.co", "ashbyhq", "workable", "smartrecruiters"))):
                if u not in links:
                    links.append(u)
        for u in links[:3]:
            if any(v in (urlparse(u).hostname or "") for v in ("greenhouse", "lever.co", "ashbyhq", "workable", "smartrecruiters")):
                pages.append((u, u))  # the link itself is the evidence; no need to fetch it
                continue
            html, i2 = _get(u)
            log.append("{}:{}".format(urlparse(u).path or "/", "ok" if html else i2))
            if html:
                pages.append((u, html))
    found: List[Tuple[str, str]] = []
    markers: List[str] = []
    for _, text in pages:
        f, m = extract_ats(text)
        found += [x for x in f if x not in found]
        markers += [x for x in m if x not in markers]
    if not found:
        # Free fallback: jina renders JS-built pages a plain GET misses. Never String (paid).
        for path in ("", "/careers"):
            md = providers.jina_fetch("https://{}{}".format(domain, path), context="ats-pilot", ledger=ledger)
            log.append("jina{}:{}".format(path or "/", "ok" if md else "none"))
            if md:
                f, m = extract_ats(md)
                found += [x for x in f if x not in found]
                markers += [x for x in m if x not in markers]
            if found:
                break
    return {"candidates": found, "markers": markers, "log": " ".join(log)}


# --------------------------------------------------------------------------------------------
# (c) web search

SEARCH_PROMPT = (
    "Find the public job board that {name} (website: {domain}) uses for its own open roles, on one of: "
    "Greenhouse (boards.greenhouse.io or job-boards.greenhouse.io), Lever (jobs.lever.co), Ashby "
    "(jobs.ashbyhq.com), Workable (apply.workable.com) or SmartRecruiters (jobs.smartrecruiters.com). "
    "Only report a board URL you actually saw in a search result or on the company's own site, and only "
    "if it belongs to this company (not a namesake). If you cannot find one, say so.\n\n"
    'Respond with JSON only: {{"found": bool, "url": string|null}}'
)


def web_search_candidates(name: str, domain: str, ledger) -> Dict[str, Any]:
    text = providers._llm_web_search(SEARCH_PROMPT.format(name=name, domain=domain), ledger=ledger,
                                     max_tokens=300, call_site=CALL_SITE)
    parsed = providers._parse_json_response(text) or {}
    url = parsed.get("url") or ""
    found, _ = extract_ats(url) if url else ([], [])
    return {"candidates": found, "url": url, "raw": (text or "")[:200]}


# --------------------------------------------------------------------------------------------
# plumbing

def run_bridge(db_copy: Path, items: List[Dict[str, Any]], paced: bool = False) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    if not items:
        return {}, {}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(items, f)
    proc = subprocess.run([str(TSX), str(BRIDGE), f.name], cwd=str(ROOT), capture_output=True, text=True,
                          env={**__import__("os").environ, "SIGNAL_DB": str(db_copy), "ATS_TIMEOUT_MS": "10000",
                               "PROBE_CACHE": str(PROBE_CACHE), "PACED": "1" if paced else "0"})
    results, stats = {}, {}
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if "stats" in obj:
            stats = obj["stats"]
        else:
            results[obj["id"]] = obj
    if proc.returncode not in (0, 3):
        raise RuntimeError("bridge failed ({}): {}".format(proc.returncode, proc.stderr[-2000:]))
    if proc.returncode == 3 or stats.get("aborted"):
        stats["aborted"] = stats.get("aborted") or "vendor 429 limit"
    return results, stats


def sample(companies: Dict[str, Dict[str, Any]], signals_copy: Path) -> List[Dict[str, Any]]:
    db = sqlite3.connect(str(signals_copy))
    boards = {d: (f, c, n) for d, f, c, n in db.execute(
        "SELECT domain, MAX(found), MAX(checked_at), COUNT(*) FROM ats_boards GROUP BY domain")}
    db.close()
    now = datetime.utcnow()

    def prior(d: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        if d not in boards:
            return None, None, None
        f, c, n = boards[d]
        age = (now - datetime.strptime(c, "%Y-%m-%d %H:%M:%S")).days
        return f, age, n

    pend = sorted(d for d, v in companies.items() if v["research_status"] == "Pending Not Yet Scanned"
                  and v.get("signal_tier") != "Tier 1" and prior(d)[0] != 1)
    t1 = sorted(d for d, v in companies.items() if v.get("signal_tier") == "Tier 1" and prior(d)[0] != 1)
    rng = random.Random(SEED)
    picked = [(d, "pending_non_tier1") for d in rng.sample(pend, min(N_PENDING, len(pend)))]
    picked += [(d, "tier1_no_board") for d in rng.sample(t1, min(N_TIER1, len(t1)))]
    print("sample frames: {} pending w/o a found board, {} Tier 1 w/o a found board".format(len(pend), len(t1)))
    out = []
    for d, grp in picked:
        f, age, n = prior(d)
        out.append({"domain": d, "group": grp, "name": companies[d]["name"], "fibery_status": companies[d]["research_status"],
                    "tier": companies[d].get("signal_tier") or "", "prior_ats_rows": n or 0,
                    "prior_newest_row_age_days": "" if age is None else age})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-copy", required=True, type=Path)
    ap.add_argument("--search-ceiling", type=float, default=2.00)
    args = ap.parse_args()
    src = args.signals_copy.resolve()
    if src == LIVE_DB.resolve():
        sys.exit("REFUSING: --signals-copy is the live signals.db")
    # The bridge's db.ts import migrates its SIGNAL_DB, so give it its own throwaway copy of the copy.
    work = Path(tempfile.mkdtemp(prefix="ats_pilot_")) / "bridge.db"
    shutil.copyfile(src, work)

    t0 = time.time()
    ledger = providers.CreditLedger()
    companies = fc.load_company_map(fc.get_token())
    rows = sample(companies, src)
    by_id = {r["domain"]: r for r in rows}
    for r in rows:
        r.update(result="miss", method="", vendor="", slug="", jobs="", names_on_board="", apply_url_hosts="",
                 accept_basis="", sample_titles="", slugs_tried=0, careers_log="", embed_markers="", search_url="", notes="")
    probe_rows: List[Dict[str, Any]] = []
    deferred_wk: Dict[str, List[List[str]]] = {r["domain"]: [] for r in rows}
    stage_stats: Dict[str, Any] = {}

    def absorb(stage: str, results: Dict[str, Dict[str, Any]]) -> None:
        for d, res in results.items():
            r = by_id[d]
            r["slugs_tried"] += len({p["slug"] for p in res["probes"]})
            for p in res["probes"]:
                if p["outcome"] in ("verified", "rejected", "empty-200") or p["outcome"].endswith("-skipped"):
                    probe_rows.append({"domain": d, "stage": stage, "vendor": p["vendor"], "slug": p["slug"],
                                       "outcome": p["outcome"], "jobs": p.get("jobs", 0),
                                       "names_on_board": "; ".join(p.get("names", [])),
                                       "apply_url_hosts": "; ".join(p.get("applyHosts", [])),
                                       "basis_or_reason": p.get("basis") or p.get("reason") or ""})
            h = res.get("hit")
            if h:
                r.update(result="hit", method=stage, vendor=h["vendor"], slug=h["slug"], jobs=h["jobs"],
                         names_on_board="; ".join(h.get("names", [])),
                         apply_url_hosts="; ".join(h.get("applyHosts", [])), accept_basis=h.get("basis", ""),
                         sample_titles=" | ".join(h.get("sampleTitles", [])))

    def stop_if_aborted(stats: Dict[str, Any]) -> bool:
        if stats.get("aborted"):
            print("!! STOPPING: " + stats["aborted"])
            return True
        return False

    # Preflight: the documented namesake collisions must stay rejected, and two known-good boards
    # must still pass, under the exact acceptance rule the pilot uses. Aborts the run otherwise.
    reg = [("eli.edu", "ELI", [[v, "eli"] for v in MAIN_VENDORS], False), ("cic.edu", "CIC", [[v, "cic"] for v in MAIN_VENDORS], False),
           ("ohio.edu", "Ohio University", [[v, "ohio"] for v in MAIN_VENDORS], False),
           ("scale.com", "Scale AI", [["greenhouse", "scale"]], False), ("onyx.security", "Onyx Security", [["ashby", "onyx"]], False),
           ("ramp.com", "Ramp", [["ashby", "ramp"]], True), ("stripe.com", "Stripe", [["greenhouse", "stripe"]], True)]
    rres, _ = run_bridge(work, [{"id": "reg:" + d + ":" + c[0][1], "domain": d, "name": n, "candidates": c} for d, n, c, _ in reg])
    regression = {}
    for d, n, c, want in reg:
        got = bool(rres.get("reg:" + d + ":" + c[0][1], {}).get("hit"))
        regression[d + "/" + c[0][1]] = "ok" if got == want else "FAIL (got hit={})".format(got)
    print("regression:", regression)
    if any(v != "ok" for v in regression.values()):
        sys.exit("ABORT: a namesake regression case did not behave -- see above")

    # (a)
    items = [{"id": r["domain"], "domain": r["domain"], "name": r["name"],
              "candidates": [[v, s] for s in candidate_slugs_v2(r["name"], r["domain"]) for v in MAIN_VENDORS]}
             for r in rows]
    res, stage_stats["a"] = run_bridge(work, items)
    absorb("a_candidates_v2", res)
    print("(a) done: {} hits, stats {}".format(sum(r["result"] == "hit" for r in rows), stage_stats["a"]))
    aborted = stop_if_aborted(stage_stats["a"])

    # (b)
    if not aborted:
        items = []
        for r in rows:
            if r["result"] == "hit":
                continue
            cp = careers_page_candidates(r["domain"], ledger)
            r["careers_log"], r["embed_markers"] = cp["log"], ",".join(cp["markers"])
            deferred_wk[r["domain"]] += [list(c) for c in cp["candidates"] if c[0] == "workable"]
            main_c = [list(c) for c in cp["candidates"] if c[0] != "workable"]
            if main_c:
                items.append({"id": r["domain"], "domain": r["domain"], "name": r["name"], "candidates": main_c})
            print("  (b) {}: {} candidate(s) {} markers={}".format(r["domain"], len(cp["candidates"]), cp["candidates"][:4], cp["markers"]))
        res, stage_stats["b"] = run_bridge(work, items)
        absorb("b_careers_page", res)
        print("(b) done: {} hits total".format(sum(r["result"] == "hit" for r in rows)))
        aborted = stop_if_aborted(stage_stats["b"])

    search_cache: Dict[str, Any] = json.loads(SEARCH_CACHE.read_text()) if SEARCH_CACHE.exists() else {}

    # (c) -- hard ceiling: never START a call that could push spend past it (worst seen call as margin)
    ceiling_hit = False
    if not aborted:
        items, worst = [], 0.0
        for r in rows:
            if r["result"] == "hit":
                continue
            spent = ledger.anthropic_total_cost()
            if spent + max(worst, 0.10) > args.search_ceiling:
                ceiling_hit = True
                r["notes"] = "web search skipped: ${:.2f} ceiling reached".format(args.search_ceiling)
                continue
            if r["domain"] in search_cache:
                ws = search_cache[r["domain"]]
                r["notes"] = "web search reused from cache (no new spend)"
            else:
                ws = web_search_candidates(r["name"], r["domain"], ledger)
                search_cache[r["domain"]] = ws
                SEARCH_CACHE.write_text(json.dumps(search_cache))
            worst = max(worst, ledger.anthropic_total_cost() - spent)
            r["search_url"] = ws["url"]
            deferred_wk[r["domain"]] += [list(c) for c in ws["candidates"] if c[0] == "workable"]
            main_c = [list(c) for c in ws["candidates"] if c[0] != "workable"]
            if main_c:
                items.append({"id": r["domain"], "domain": r["domain"], "name": r["name"], "candidates": main_c})
            elif ws["url"]:
                r["notes"] = "search returned a non-ATS URL"
            print("  (c) {}: {} -> {} (spent ${:.3f})".format(r["domain"], ws["url"] or "none", ws["candidates"], ledger.anthropic_total_cost()))
        res, stage_stats["c"] = run_bridge(work, items)
        absorb("c_web_search", res)
        aborted = stop_if_aborted(stage_stats["c"])

    # (w) separate, final, paced Workable pass for companies still without a verified board:
    # Workable links found by (b)/(c) first, then the v2 candidates. Never aborts the pilot.
    wk_probed: List[str] = []
    if not aborted:
        items = []
        for r in rows:
            if r["result"] == "hit":
                continue
            cands = deferred_wk[r["domain"]] + [["workable", s] for s in candidate_slugs_v2(r["name"], r["domain"])]
            seen, uniq = set(), []
            for c in cands:
                if c[1].lower() not in seen:
                    seen.add(c[1].lower()); uniq.append(c)
            items.append({"id": r["domain"], "domain": r["domain"], "name": r["name"], "candidates": uniq})
            wk_probed.append(r["domain"])
        res, stage_stats["w"] = run_bridge(work, items, paced=True)
        absorb("w_workable_paced", res)
        for d in wk_probed:
            pr = res.get(d, {}).get("probes", [])
            if not res.get(d, {}).get("hit") and (not pr or any(p["outcome"].endswith("-skipped") for p in pr)):
                by_id[d]["notes"] = (by_id[d]["notes"] + "; " if by_id[d]["notes"] else "") + "workable-skipped"

    OUT.parent.mkdir(exist_ok=True)
    cols = list(rows[0].keys())
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    with open(OUT_PROBES, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "stage", "vendor", "slug", "outcome", "jobs",
                                          "names_on_board", "apply_url_hosts", "basis_or_reason"])
        w.writeheader(); w.writerows(probe_rows)
    shutil.rmtree(work.parent, ignore_errors=True)

    n = len(rows)
    hits = [r for r in rows if r["result"] == "hit"]
    no_wk = [r for r in hits if r["vendor"] != "workable"]
    gate = {"companies": n,
            "verified_with_workable": "{}/{} = {:.0%}".format(len(hits), n, len(hits) / n),
            "verified_without_workable": "{}/{} = {:.0%}".format(len(no_wk), n, len(no_wk) / n),
            "by_method": {m: sum(r["method"] == m for r in hits) for m in sorted({r["method"] for r in hits})},
            "by_vendor": {v: sum(r["vendor"] == v for r in hits) for v in VENDORS},
            "by_group": {g: "{}/{}".format(sum(r["result"] == "hit" for r in rows if r["group"] == g), sum(r["group"] == g for r in rows))
                         for g in sorted({r["group"] for r in rows})},
            "workable_pass": {"companies_probed": len(wk_probed), "hits": sum(r["vendor"] == "workable" for r in hits),
                              "skipped": sum("workable-skipped" in r["notes"] for r in rows)}}
    summary = {"gate": gate, "regression": regression, "runtime_s": round(time.time() - t0, 1), "stage_stats": stage_stats,
               "anthropic": ledger.anthropic, "anthropic_total_usd": round(ledger.anthropic_total_cost(), 4),
               "search_ceiling_hit": ceiling_hit, "spent": ledger.spent, "attempts": ledger.attempts,
               "warnings": ledger.warnings}
    print(json.dumps(summary, indent=2, default=str))
    (OUT.with_suffix(".summary.json")).write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
