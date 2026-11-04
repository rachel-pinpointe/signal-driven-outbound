// One-shot company-signal scan for our cron-driven daily run.
//
// `npm run cycle` only fetches JOBS (src/ingest.ts runAllOnce) — it never calls runClientSignals,
// which is normally only reached from HyperSignal's own internal always-on scheduler (src/scheduler.ts)
// or its dashboard/API ("check signals now"). We don't run a persistent server (each cron firing is a
// fresh process), so this script calls runClientSignals directly for every active watchlist client.
//
// GDELT is rate-limited (~1 lookup/5.2s) and therefore doesn't run inline — runClientSignals queues it
// into the `slow_queue` table and returns immediately, expecting a long-running server's background
// timer (startSlowQueue) to drip through it over time. We have no long-running process, so after the
// scan we drain that queue ourselves, synchronously, until it's empty or a time budget runs out —
// otherwise GDELT coverage would never advance (queued items just pile up, unprocessed, forever).
//
// Usage: npx tsx scripts/run-signals-once.mts
import { seedFromYaml, loadClients } from "../src/config.ts";
import { runClientSignals } from "../src/signals/index.ts";
import { drainOne, pacedProducers } from "../src/signals/slowQueue.ts";

const DRAIN_BUDGET_MS = Number(process.env.DRAIN_BUDGET_MS ?? 8 * 60 * 1000);

seedFromYaml();
const clients = loadClients().filter((c) => c.active !== false && c.kind === "watchlist");

if (!clients.length) {
  console.error("No active watchlist clients found in clients/*.yaml");
  process.exit(1);
}

let total = 0;
for (const client of clients) {
  const fresh = await runClientSignals(client);
  console.log(`[signals] ${client.name}: ${fresh} new signal(s)`);
  total += fresh;
}

const paced = pacedProducers();
if (paced.length) {
  console.log(`\n[slowqueue] draining paced producers (${paced.join(", ")}), budget ${DRAIN_BUDGET_MS / 1000}s...`);
  const deadline = Date.now() + DRAIN_BUDGET_MS;
  let drained = 0;
  outer: while (Date.now() < deadline) {
    let any = false;
    for (const key of paced) {
      if (Date.now() >= deadline) break outer;
      const did = await drainOne(key);
      if (did) { any = true; drained++; }
    }
    if (!any) break; // every paced queue is empty
  }
  console.log(`[slowqueue] drained ${drained} item(s)${Date.now() >= deadline ? " (hit time budget, rest carries to next run)" : " (queue empty)"}`);
}

console.log(`\n=== SIGNAL SCAN DONE === new=${total}`);
process.exit(0);
