# HyperSignal config backup

`hypersignal/` (the actual HyperSignal-Hiring clone + its npm install + local SQLite DB) is
gitignored on purpose — it's a vendored clone of someone else's tool
(github.com/athm793/HyperSignal-Hiring), reinstallable any time with `git clone`, and its `data/`
folder holds a SQLite DB we don't want in this repo's history.

But two files under `hypersignal/` ARE ours, not upstream's, and losing them would mean redoing
Phase 4's actual design work — so they're mirrored here for version history and disaster recovery.
The daily driver, `run-cron.sh`, is not mirrored here: the current version lives in
[hypersignal-gtm](https://github.com/rachel-pinpointe/hypersignal-gtm/blob/main/run-cron.sh).
If this Mac is ever rebuilt: re-clone HyperSignal into `hypersignal/`, then copy these back into
place at the paths in their own header comments.

- `nycbound-calibration.yaml` → `hypersignal/clients/nycbound-calibration.yaml` (the watchlist config: which 20 companies, which signal producers)
- `run-signals-once.mts` → `hypersignal/scripts/run-signals-once.mts` (triggers the company-signal producers + drains the GDELT queue — `npm run cycle` alone does NOT do this, see the comment at the top of the file)

Keep these in sync manually — editing the live copy under `hypersignal/` and forgetting to copy back
here is the obvious failure mode.
