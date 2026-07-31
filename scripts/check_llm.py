#!/usr/bin/env python
"""Verify the configured LLM end to end.

Runs the *real* tier-5 path against a sample page: reduce HTML → ask the model
for selectors → run those selectors → score the result. If this passes, the
self-learning path works; if it fails, it says which stage broke.

    python scripts/check_llm.py

Nothing here touches the database, Redis, or the network beyond the model
provider itself, so it is safe to run before the rest of the stack is up.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.errors import PlatformError
from app.extractors.html_reducer import reduce_html
from app.extractors.llm_extractor import generate_selectors
from app.extractors.selector_extractor import extract_with_selectors
from app.extractors.validation import score_extraction
from app.llm.base import LLMProvider
from app.llm.budget import BudgetTracker
from app.llm.client import LLMClient, UsageTally, build_provider

SAMPLE_URL = "https://example.com/careers"

#: Deliberately not markup any built-in selector recognises, so the model has
#: to actually read the structure rather than match a known pattern.
SAMPLE_HTML = """
<!doctype html>
<html><head><title>Careers</title>
<style>.x{color:red}</style>
<script>window.analytics={id:"noise"};</script>
</head>
<body>
  <nav><a href="/about">About us</a><a href="/contact">Contact</a></nav>
  <main>
    <h1>Open roles</h1>
    <div class="vacancy-row" data-testid="vacancy">
      <span class="vacancy-name">Senior Backend Engineer</span>
      <span class="vacancy-place">Bengaluru, India</span>
      <span class="vacancy-when">Posted 3 days ago</span>
      <a class="vacancy-link" href="/openings/8801">View role</a>
    </div>
    <div class="vacancy-row" data-testid="vacancy">
      <span class="vacancy-name">Data Platform Engineer</span>
      <span class="vacancy-place">Remote - India</span>
      <span class="vacancy-when">Posted yesterday</span>
      <a class="vacancy-link" href="/openings/8802">View role</a>
    </div>
    <div class="vacancy-row" data-testid="vacancy">
      <span class="vacancy-name">Site Reliability Engineer</span>
      <span class="vacancy-place">Pune, India</span>
      <span class="vacancy-when">Posted today</span>
      <a class="vacancy-link" href="/openings/8803">View role</a>
    </div>
  </main>
  <footer><a href="/privacy">Privacy policy</a></footer>
</body></html>
"""

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(message: str) -> None:
    print(f"{GREEN}  ✓{RESET} {message}")


def bad(message: str) -> None:
    print(f"{RED}  ✗{RESET} {message}")


def warn(message: str) -> None:
    print(f"{YELLOW}  !{RESET} {message}")


def heading(message: str) -> None:
    print(f"\n{message}")


def check_configuration() -> tuple[bool, LLMProvider | None]:
    heading("1. Configuration")
    settings = get_settings()
    print(f"{DIM}   provider={settings.llm_provider} model={settings.llm_model}{RESET}")

    if settings.llm_provider == "null" or not settings.llm_enabled:
        bad("LLM is disabled (LLM_PROVIDER=null or LLM_ENABLED=false)")
        print("    Tiers 1-4 still work; tier 5 and semantic matching are off.")
        return False, None

    try:
        provider = build_provider()
    except PlatformError as exc:
        bad(f"could not build provider: {exc}")
        return False, None

    if not provider.is_available:
        key_name = f"{settings.llm_provider.upper()}_API_KEY"
        bad(f"{key_name} is not set")
        print("    Add it to .env — get a free Gemini key at")
        print("    https://aistudio.google.com/apikey")
        return False, None

    # Never print the key, only that one is present and roughly well-formed.
    ok(f"{settings.llm_provider} provider configured, API key present")
    return True, provider


#: Models worth suggesting when the configured one will not serve. Text
#: generation only — image/tts/computer-use variants cannot do this job, and
#: pro-tier models burn a free key's quota in a handful of calls.
_SKIP_SUBSTRINGS = ("image", "tts", "computer-use", "embedding", "aqa", "veo", "imagen")


def _is_text_candidate(name: str) -> bool:
    return name.startswith("gemini") and not any(s in name for s in _SKIP_SUBSTRINGS)


def _ping(model: str) -> tuple[bool, str]:
    """Can this model actually be invoked? One attempt, no backoff."""
    provider = build_provider("gemini", model)
    provider.max_attempts = 1  # type: ignore[attr-defined]
    try:
        provider.complete(prompt='Reply with exactly {"ok":true}', max_tokens=2000)
        return True, "callable"
    except PlatformError as exc:
        text = str(exc).lower()
        if "will not serve" in text or "no longer available" in text or "not found" in text:
            return False, "not available to this account"
        if "quota" in text or "429" in text or "rate limit" in text:
            return False, "quota / rate limited"
        return False, str(exc)[:70]


def check_models(provider: LLMProvider) -> bool:
    """Verify the configured model is *callable*, not merely listed.

    These are genuinely different things. Google gates older models to
    pre-existing users ahead of a shutdown while still advertising them in
    ``models.list()`` — so a listing check passes and the first real call
    404s. Only an actual invocation settles it.
    """
    heading("2. Model availability")
    configured = get_settings().llm_model
    names = provider.list_models()

    if names:
        print(f"{DIM}   {len(names)} models listed by this key{RESET}")
        if configured not in names:
            warn(f"{configured!r} is not even in the listing")

    callable_, reason = _ping(configured)
    if callable_:
        ok(f"{configured!r} is listed AND callable")
        return True

    bad(f"{configured!r} is not usable: {reason}")

    candidates = [n for n in names if _is_text_candidate(n) and n != configured]
    # Newest first — that is also the direction Google pushes users on gating.
    candidates.sort(reverse=True)
    if not candidates:
        return False

    print(f"{DIM}   probing alternatives...{RESET}")
    working: list[str] = []
    for name in candidates[:5]:
        alive, why = _ping(name)
        mark = f"{GREEN}usable{RESET}" if alive else f"{DIM}{why}{RESET}"
        print(f"{DIM}   - {name:32}{RESET} {mark}")
        if alive:
            working.append(name)
            if len(working) == 2:
                break

    if working:
        print()
        warn("fix this by setting, in .env:")
        print(f"    LLM_MODEL={working[0]}")
    return False


def check_reduction() -> int:
    heading("3. HTML reduction (what actually gets sent)")
    reduced = reduce_html(SAMPLE_HTML)
    print(
        f"{DIM}   {reduced.original_bytes} bytes -> {reduced.reduced_bytes} bytes "
        f"({reduced.reduction_ratio:.0%} smaller), region: {reduced.root_path}{RESET}"
    )
    if "analytics" in reduced.html or "Privacy policy" in reduced.html:
        warn("page chrome survived reduction")
    else:
        ok("scripts, styles, nav and footer stripped")
    if reduced.candidate_count >= 2:
        ok(f"{reduced.candidate_count} repeating entries detected")
    else:
        warn("no repeating structure found — the model gets less to work with")
    return reduced.reduced_bytes


def check_generation(client: LLMClient) -> bool:
    heading("4. Live selector generation")
    tally = UsageTally()

    try:
        generated = generate_selectors(SAMPLE_HTML, SAMPLE_URL, client=client, tally=tally)
    except PlatformError as exc:
        bad(f"generation failed: {exc}")
        return False

    ok("model responded with parseable JSON")
    print(f"{DIM}   container: {generated.selectors.container}{RESET}")
    print(f"{DIM}   title:     {generated.selectors.title}{RESET}")
    print(f"{DIM}   url:       {generated.selectors.url}{RESET}")
    print(f"{DIM}   location:  {generated.selectors.location}{RESET}")
    print(f"{DIM}   date:      {generated.selectors.date}{RESET}")
    print(f"{DIM}   claimed confidence: {generated.claimed_confidence:.2f}{RESET}")

    heading("5. Verification (do those selectors actually work?)")
    jobs = [
        job
        for job in extract_with_selectors(SAMPLE_HTML, SAMPLE_URL, generated.selectors)
        if job.is_usable()
    ]
    score = score_extraction(jobs)

    print(f"{DIM}   extracted {len(jobs)} of 3 expected postings{RESET}")
    for job in jobs:
        print(f"{DIM}     - {job.title} | {job.location} | {job.url}{RESET}")

    if score.reasons:
        for reason in score.reasons:
            warn(reason)

    print(f"{DIM}   measured confidence: {score.confidence:.2f}{RESET}")

    heading("6. Cost")
    print(
        f"{DIM}   {tally.input_tokens} in / {tally.output_tokens} out tokens, "
        f"${tally.cost_usd:.6f} estimated{RESET}"
    )
    print(f"{DIM}   at this rate, ~{int(5.0 / max(tally.cost_usd, 1e-9)):,} "
          f"selector generations fit in a $5 daily budget{RESET}")

    if len(jobs) == 3 and score.is_acceptable:
        ok("selectors verified against the page — tier 5 is working end to end")
        return True
    if jobs:
        warn("partial extraction: the model produced usable but imperfect selectors")
        print("    The learner would reject anything below EXTRACTION_MIN_CONFIDENCE")
        print(f"    (currently {get_settings().extraction_min_confidence}).")
        return score.is_acceptable

    bad("selectors matched nothing — the learner would reject and not store them")
    return False


def check_budget() -> None:
    heading("7. Budget tracker")

    # Pinged first because ``status()`` deliberately fails *closed* on a Redis
    # error — it reports the breaker as open, which would otherwise read as
    # "you have tripped the breaker" when it really means "Redis is down".
    import redis

    try:
        redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2).ping()
    except Exception as exc:
        warn(f"Redis unreachable ({type(exc).__name__})")
        print("    Spend tracking needs Redis; without it the breaker fails closed")
        print("    and tier 5 stays disabled. Start Redis, then re-run.")
        return

    status = BudgetTracker().status()
    if status.breaker_open:
        warn("circuit breaker is OPEN — tier 5 is currently disabled")
    else:
        ok(
            f"${status.spent_usd:.4f} of ${status.limit_usd:.2f} spent today "
            f"({status.calls_today} calls)"
        )


def main() -> int:
    print("LLM check — exercising the real tier-5 path")

    configured, provider = check_configuration()
    if not configured or provider is None:
        return 1

    if not check_models(provider):
        # No point running the generation stage against a model that will not
        # answer; the error would just be the same 404 with more noise.
        print(f"\n{RED}Not ready.{RESET} Fix LLM_MODEL and re-run.")
        return 1

    check_reduction()

    client = LLMClient(provider=provider)
    succeeded = check_generation(client)
    check_budget()

    print()
    if succeeded:
        print(f"{GREEN}Ready.{RESET} The self-learning path works against the live API.")
        return 0
    print(f"{RED}Not ready.{RESET} See the failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
