// Phase 21 pilot bridge: probe candidate ATS boards with HyperSignal's OWN fetchers and verifyBoard,
// so the pilot measures the exact acceptance rule production uses rather than a Python re-implementation
// of it. Read-only against the vendors; never imported by anything.
//
// MUST be run with SIGNAL_DB pointing at a COPY of signals.db: importing ats.ts reaches db.ts, which
// opens and migrates whatever SIGNAL_DB names at import time. Refuses to start otherwise.
//
//   SIGNAL_DB=/path/to/copy.db hypersignal/node_modules/.bin/tsx pilots/ats_pilot_verify.mts in.json
//
// in.json: [{ "id": "...", "domain": "acme.com", "candidates": [["greenhouse","acme"], ...] }, ...]
// Candidates are probed in order, grouped by slug: every vendor for one slug runs in parallel, and a
// company stops at the first slug group that yields a verified board. One JSON line per company on
// stdout; progress on stderr.
//
// Rate limits: at most 2 requests in flight per vendor (a global semaphore across companies), 429s are
// retried with exponential backoff, and a vendor that is still 429ing after its retries
// VENDOR_429_LIMIT times aborts the whole run (exit code 3) rather than being read as "no board".
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const dbPath = process.env.SIGNAL_DB ?? "";
if (!dbPath || resolve(dbPath).endsWith("hypersignal/data/signals.db")) {
  console.error("REFUSING: SIGNAL_DB must point at a copy of signals.db, never the live file.");
  process.exit(2);
}

const ats = await import("../hypersignal/src/sources/ats.ts");
const { registrable, normalizeOrgDomain } = await import("../hypersignal/src/normalize.ts");

type Vendor = "greenhouse" | "lever" | "ashby" | "workable" | "smartrecruiters";
const FETCH: Record<Vendor, (slug: string, domain: string) => Promise<any[]>> = {
  greenhouse: (s, d) => ats.fetchGreenhouse(s, d),
  lever: (s, d) => ats.fetchLever(s, d),
  ashby: (s, d) => ats.fetchAshby(s, d),
  workable: (s, d) => ats.fetchWorkable(s, d),
  smartrecruiters: (s, d) => ats.fetchSmartRecruiters(s, d, { existenceOnly: true }),
};

// PACED=1: the separate final Workable pass -- one request at a time, PACED_GAP_MS apart, no retries,
// and three 429s IN A ROW stop that vendor only (remaining probes come back "workable-skipped").
const PACED = process.env.PACED === "1";
const PACED_GAP_MS = Number(process.env.PACED_GAP_MS ?? 3000);
const PER_VENDOR = PACED ? 1 : 2;
const COMPANY_CONCURRENCY = PACED ? 1 : 2;
const BACKOFF_MS = [2000, 4000, 8000, 16000];
const VENDOR_429_LIMIT = 3;

// ---- per-vendor semaphore ----
const inflight = new Map<string, number>();
const waiters = new Map<string, Array<() => void>>();
async function acquire(v: string) {
  while ((inflight.get(v) ?? 0) >= PER_VENDOR) {
    await new Promise<void>((r) => { const q = waiters.get(v) ?? []; q.push(r); waiters.set(v, q); });
  }
  inflight.set(v, (inflight.get(v) ?? 0) + 1);
}
function release(v: string) {
  inflight.set(v, (inflight.get(v) ?? 1) - 1);
  const q = waiters.get(v); if (q?.length) q.shift()!();
}

const stats = { requests: 0, byVendor: {} as Record<string, number>, rate429: {} as Record<string, number> };
const exhausted429 = new Map<string, number>();
let aborted = "";
let pacedConsecutive429 = 0, pacedStopped = "";
let lastPacedAt = 0;

// Record every ATS response's status, timing and rate-limit headers, so the report can say where a
// vendor started limiting and whether it sent Retry-After. Wraps the global fetch the fetchers use.
const t0Global = Date.now();
const rateLog: Array<{ t: number; host: string; status: number; retryAfter: string | null; rl: string | null }> = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: any, init?: any) => {
  const r = await realFetch(input, init);
  try {
    const host = new URL(typeof input === "string" ? input : input.url).hostname;
    if (r.status === 429 || /workable/.test(host)) {
      const rl = ["x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset"]
        .map((h) => r.headers.get(h) ? `${h}=${r.headers.get(h)}` : "").filter(Boolean).join(" ") || null;
      rateLog.push({ t: (Date.now() - t0Global) / 1000, host, status: r.status, retryAfter: r.headers.get("retry-after"), rl });
    }
  } catch { /* logging only */ }
  return r;
}) as typeof fetch;

// Persistent probe cache (PROBE_CACHE=path.jsonl): a definite answer (404, empty-200, verified,
// rejected) is reused on a resumed run; errors and 429s are never cached.
const cachePath = process.env.PROBE_CACHE ?? "";
const probeCache = new Map<string, any>();
if (cachePath) {
  try { for (const l of readFileSync(cachePath, "utf8").split("\n")) if (l.trim()) { const o = JSON.parse(l); probeCache.set(o.key, o.result); } } catch { /* first run */ }
}
const { appendFileSync } = await import("node:fs");
const cacheKey = (v: string, slug: string, domain: string) => `${v}|${slug.toLowerCase()}|${domain}`;

async function fetchPaced(v: Vendor, slug: string, domain: string): Promise<any[]> {
  if (pacedStopped) throw new Error("paced-skipped");
  const wait = lastPacedAt + PACED_GAP_MS - Date.now();
  if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  lastPacedAt = Date.now();
  stats.requests++; stats.byVendor[v] = (stats.byVendor[v] ?? 0) + 1;
  try {
    const jobs = await FETCH[v](slug, domain);
    pacedConsecutive429 = 0;
    return jobs;
  } catch (e) {
    if ((e as Error).message === "ats-http-429") {
      stats.rate429[v] = (stats.rate429[v] ?? 0) + 1;
      if (++pacedConsecutive429 >= 3) pacedStopped = `${v} returned 429 three times in a row after ${stats.byVendor[v]} paced requests`;
    } else pacedConsecutive429 = 0;
    throw e;
  }
}

async function fetchWithBackoff(v: Vendor, slug: string, domain: string): Promise<any[]> {
  if (PACED) return fetchPaced(v, slug, domain);
  for (let attempt = 0; ; attempt++) {
    if (aborted) throw new Error("aborted");
    let retryAfter = 0;
    await acquire(v);
    try {
      stats.requests++; stats.byVendor[v] = (stats.byVendor[v] ?? 0) + 1;
      return await FETCH[v](slug, domain);
    } catch (e) {
      if ((e as Error).message !== "ats-http-429") throw e;
      stats.rate429[v] = (stats.rate429[v] ?? 0) + 1;
      if (attempt >= BACKOFF_MS.length) {
        const n = (exhausted429.get(v) ?? 0) + 1; exhausted429.set(v, n);
        if (n >= VENDOR_429_LIMIT) aborted = `${v} kept returning 429 (${n} probes exhausted all retries)`;
        throw e;
      }
      retryAfter = BACKOFF_MS[attempt]!;
    } finally { release(v); }
    await new Promise((r) => setTimeout(r, retryAfter));   // back off OUTSIDE the semaphore
  }
}

function evidence(domain: string, jobs: any[]) {
  const dom = normalizeOrgDomain(domain);
  const domRe = new RegExp("\\b" + dom.replace(/[.]/g, "\\.") + "\\b", "i");
  const domainInPosting = jobs.some((j) => domRe.test(String(j.description ?? "") + " " + String((j.raw as any)?.description ?? "")));
  const names = [...new Set(jobs.map((j) => j.company).filter((c: unknown) => typeof c === "string" && c.trim()))] as string[];
  const hosts = [...new Set(jobs.map((j) => { try { return new URL(j.url).hostname; } catch { return ""; } }).filter(Boolean))] as string[];
  const selfHosted = hosts.some((h) => registrable(h) === dom);
  // Negative evidence: postings that name a SAME-STEM domain other than this company's own
  // ("onyx.app" on a board probed for onyx.security) are a namesake's board.
  const stem0 = dom.split(".")[0]!;
  const sib = new RegExp("\\b(?:www\\.)?(" + stem0.replace(/[^a-z0-9-]/g, "") + "\\.[a-z]{2,}(?:\\.[a-z]{2})?)\\b", "gi");
  const otherDomains = new Set<string>();
  for (const j of jobs) {
    const t = String(j.description ?? "") + " " + String((j.raw as any)?.description ?? "");
    for (const m of t.matchAll(sib)) { const d = m[1]!.toLowerCase(); if (d !== dom) otherDomains.add(d); }
  }
  return { names: names.slice(0, 5), applyHosts: hosts.slice(0, 5), selfHosted, domainInPosting,
           otherSameStemDomains: [...otherDomains].slice(0, 3) };
}

// Phase 21 precision rule, applied ON TOP of verifyBoard (which can accept an unnamed board on slug
// length alone). A board counts only with real corroboration: a self-hosted apply URL, the company's
// own domain in a posting, a stated employer name that matches the domain stem or the Fibery name,
// or (unnamed boards only) a slug identical to the domain stem. Returns null when none holds.
const key = (s: string) => String(s ?? "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "")
  .replace(/(inc|llc|ltd|corp|co|gmbh|plc)$/, "");
function strictBasis(domain: string, fiberyName: string, slug: string, ev: ReturnType<typeof evidence>): string | null {
  if (ev.selfHosted) return "self-hosted apply URL";
  if (!ev.domainInPosting && ev.otherSameStemDomains.length) return null;   // namesake
  if (ev.domainInPosting) return "company domain appears in posting text";
  const stem = key(normalizeOrgDomain(domain).split(".")[0]!);
  const fname = key(fiberyName);
  const nameOk = (n: string) => {
    const k = key(n);
    if (k.length < 4) return false;
    if (k === stem || (fname && k === fname)) return true;
    const pre = (a: string, b: string) => { const sh = a.length < b.length ? a : b, lo = a.length < b.length ? b : a; return sh.length >= 5 && lo.startsWith(sh); };
    return pre(k, stem) || (!!fname && pre(k, fname));
  };
  if (ev.names.some(nameOk)) return "stated employer name matches domain/Fibery name";
  if (!ev.names.length && key(slug) === stem && stem.length >= 4) return "unnamed board, slug == domain stem (exact)";
  return null;
}

async function probe(v: Vendor, slug: string, domain: string, fiberyName: string) {
  const k = cacheKey(v, slug, domain);
  if (probeCache.has(k)) return { ...probeCache.get(k), cached: true };
  const r: any = await probeLive(v, slug, domain, fiberyName);
  if (cachePath && (/^http-(404|410)$/.test(r.outcome) || ["empty-200", "verified", "rejected"].includes(r.outcome))) {
    probeCache.set(k, r); appendFileSync(cachePath, JSON.stringify({ key: k, result: r }) + "\n");
  }
  return r;
}

async function probeLive(v: Vendor, slug: string, domain: string, fiberyName: string) {
  const warns: string[] = [];
  const orig = console.warn;
  try {
    const jobs = await fetchWithBackoff(v, slug, domain);
    if (!jobs.length) return { vendor: v, slug, outcome: "empty-200", jobs: 0 };
    const ev = evidence(domain, jobs);
    console.warn = (...a: unknown[]) => { warns.push(a.map(String).join(" ")); };
    let ok = false;
    try { ok = ats.verifyBoard(domain, slug, jobs); } finally { console.warn = orig; }
    if (!ok) return { vendor: v, slug, outcome: "rejected", jobs: jobs.length, ...ev, reason: "verifyBoard: " + (warns.join(" | ") || "returned false") };
    const basis = strictBasis(domain, fiberyName, slug, ev);
    return basis
      ? { vendor: v, slug, outcome: "verified", jobs: jobs.length, ...ev, basis, note: warns.join(" | "),
          sampleTitles: jobs.slice(0, 3).map((j) => j.title) }
      : { vendor: v, slug, outcome: "rejected", jobs: jobs.length, ...ev,
          reason: (ev.otherSameStemDomains.length && !ev.domainInPosting ? `precision rule: namesake, postings reference ${ev.otherSameStemDomains.join(", ")} not ${domain}; ` : "") + "precision rule: verifyBoard accepted but no corroboration (no matching name, no self-hosted URL, no domain in postings, slug != stem)" + (warns.length ? " | " + warns.join(" | ") : "") };
  } catch (e) {
    const m = (e as Error).message ?? String(e);
    if (m === "paced-skipped") return { vendor: v, slug, outcome: `${v}-skipped`, jobs: 0 };
    const http = /^ats-http-(\d{3})$/.exec(m);
    return { vendor: v, slug, outcome: http ? `http-${http[1]}` : `error: ${m.slice(0, 80)}`, jobs: 0 };
  }
}

async function runCompany(c: { id: string; domain: string; name?: string; candidates: [Vendor, string][] }) {
  const groups = new Map<string, Vendor[]>();
  for (const [v, s] of c.candidates) { if (!FETCH[v]) continue; const g = groups.get(s) ?? []; if (!g.includes(v)) g.push(v); groups.set(s, g); }
  const probes: any[] = [];
  let hit: any = null;
  for (const [slug, vendors] of groups) {
    if (aborted) break;
    const res = await Promise.all(vendors.map((v) => probe(v, slug, c.domain, c.name ?? "")));
    probes.push(...res);
    hit = res.find((r) => r.outcome === "verified") ?? null;
    if (hit) break;
  }
  return { id: c.id, domain: c.domain, hit, probes, aborted: aborted || undefined };
}

const input = JSON.parse(readFileSync(process.argv[2]!, "utf8")) as any[];
let next = 0, done = 0;
const t0 = Date.now();
await Promise.all(Array.from({ length: Math.min(COMPANY_CONCURRENCY, input.length) }, async () => {
  for (let i = next++; i < input.length; i = next++) {
    if (aborted) break;
    const r = await runCompany(input[i]);
    process.stdout.write(JSON.stringify(r) + "\n");
    done++;
    console.error(`[bridge] ${done}/${input.length} ${r.domain}: ${r.hit ? `${r.hit.vendor}:${r.hit.slug}` : "miss"} (${r.probes.length} probes)`);
  }
}));
process.stdout.write(JSON.stringify({ stats: { ...stats, seconds: (Date.now() - t0) / 1000, aborted, pacedStopped,
  rateLog429: rateLog.filter((x) => x.status === 429).slice(0, 40), rateLogWorkable: rateLog.filter((x) => /workable/.test(x.host)) } }) + "\n");
process.exit(aborted ? 3 : 0);
