"""Pre-flight Anthropic API check (Phase 17).

One cheap live call, made before the research pipeline starts, so a run that cannot possibly
produce usable output fails loudly at the start instead of degrading silently for half an hour.

Why this is needed at all: main.py exits 0 even when every Anthropic call it makes fails.
That is deliberate -- research_likely_priorities() and research_stated_goal() are both built
to "skip rather than ship under the bar", so a failed call returns None and the pipeline moves
on. That behavior is right for one bad response and wrong for an exhausted credit balance,
where EVERY call will fail: the run still spends real String/QuickEnrich/Apollo credits, writes
Contacts with no Likely Priorities, and reports success. Apollo enrollment downstream then skips
those same contacts for a missing field (load_enrollable_contacts requires Likely Priorities),
and run-cron.sh finishes by posting ":white_check_mark: completed OK". Nothing in that chain is
a bug on its own; together they turn a billing problem into a silent no-op day.

This calls the SDK directly rather than going through providers._llm_extract() on purpose:
_llm_extract swallows the exception and returns None, which loses the one thing this check
exists to find out -- WHY the call failed. A credit/billing failure is a hard stop; a one-off
network blip or model-id typo is a different conversation.

Exit codes, which run-cron.sh reports in its abort log line:
  0 - the API answered. The run can proceed.
  2 - ANTHROPIC_API_KEY is missing from .env.
  3 - the call failed for a billing/credit/quota reason. Nothing downstream will work today.
  4 - the call failed for any other reason (network, auth, bad model id, API outage).

Usage (as a module from the repo root, same as every other script here -- package-relative
imports mean `python3 src/preflight_anthropic.py` fails on ImportError):
    python3 -m src.preflight_anthropic
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from . import providers as prov
except ImportError:  # running as a script rather than a package module
    import providers as prov

# The cheapest call that still proves the account can actually complete a request: a credit or
# quota refusal is rejected before any tokens are generated, so this costs ~nothing when it
# passes and nothing at all when it fails.
PREFLIGHT_PROMPT = "Reply with the single word: ok"
PREFLIGHT_MAX_TOKENS = 5

# Substrings that mark a billing/quota refusal rather than a transient failure. Matched against
# the exception type name and message together, lowercased, because the Anthropic SDK's exception
# hierarchy varies by version (the same refusal surfaces as BadRequestError on one version and
# PermissionDeniedError on another) -- the message text is the stable signal, not the class.
# "credit balance is too low" is the current wording; the rest are defensive.
_CREDIT_FAILURE_MARKERS = (
    "credit balance",
    "insufficient credit",
    "insufficient_quota",
    "billing",
    "payment",
    "quota",
    "spending limit",
    "usage limit",
)


def is_credit_failure(detail: str) -> bool:
    """Whether a failure detail string names a billing/credit/quota problem (exit 3) rather than
    some other API failure (exit 4). Kept as its own function so the marker list has one home and
    a correction is a one-line change -- same reasoning as providers.scrubby_classify()."""
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _CREDIT_FAILURE_MARKERS)


def check():
    """Make the call. Returns (exit_code, detail). Never raises -- a preflight that crashed
    would be indistinguishable from the API being down, and the caller needs a code either way."""
    try:
        client = prov._get_anthropic_client()
    except prov.MissingKey as exc:
        return 2, str(exc)

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=PREFLIGHT_MAX_TOKENS,
            messages=[{"role": "user", "content": PREFLIGHT_PROMPT}],
        )
    except Exception as exc:  # the SDK's own exception hierarchy varies by version
        detail = "{}: {}".format(type(exc).__name__, exc)
        return (3 if is_credit_failure(detail) else 4), detail

    # Phase 19: tracked like every other Anthropic call, tagged "preflight". A local ledger
    # because this runs as its own process and has no run-wide ledger to join -- the per-call line
    # still lands in the shared spend log, which is where the day's total is assembled.
    ledger = prov.CreditLedger()
    prov.record_anthropic_call(ledger, "preflight", model, resp)

    text = "".join(
        b.text for b in (getattr(resp, "content", None) or [])
        if getattr(b, "type", "") == "text"
    ).strip()
    if not text:
        # A 200 with no text content isn't a credit problem, but it isn't a working API either.
        return 4, "API returned a response with no text content"
    return 0, "model {} replied {!r} (est ${:.6f})".format(
        model, text[:40], ledger.anthropic_total_cost())


def main() -> int:
    code, detail = check()
    if code == 0:
        print("Anthropic preflight OK -- {}".format(detail))
    elif code == 2:
        print("Anthropic preflight FAILED (ANTHROPIC_API_KEY missing): {}".format(detail))
    elif code == 3:
        print("Anthropic preflight FAILED (credits/billing -- every LLM call this run would "
              "fail): {}".format(detail))
    else:
        print("Anthropic preflight FAILED (API unreachable or rejected the call): {}".format(
            detail))
    return code


if __name__ == "__main__":
    sys.exit(main())
