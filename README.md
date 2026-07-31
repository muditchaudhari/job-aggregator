# Job Aggregation Platform

A self-learning job aggregator. Register company career pages; it scans them on
a schedule, extracts postings regardless of how each site is built, matches them
against your profile, and notifies you only about relevant new jobs.

The organising principle: **LLM calls are a failure path, not a code path.** A
healthy company is scraped thousands of times without ever touching a model.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design decisions, trade-offs, and
diagrams.

---

## How it works

```
Beat tick → due companies → detect ATS → fetch (HTTP or render)
   → extract (API → embedded JSON → CSS → XPath → LLM)
   → normalise → deduplicate → match → notify → record
```

The extraction ladder is where the economics live. Each tier is tried in order
and its output scored; only a genuine failure descends to the next.

| Tier | Source | Cost |
|---|---|---|
| 1 | Official ATS API | one request |
| 2 | Embedded JSON (JSON-LD, `__NEXT_DATA__`, framework state) | one request |
| 3 | Stored CSS selectors | one request (+render) |
| 4 | Stored XPath | one request (+render) |
| 5 | LLM selector generation | render + ~8–15 k tokens |

Tier 5 writes its result back as a **new selector version**, so the next scan of
that site re-enters at tier 3 and costs nothing. That is the "self-learning"
part: you pay a model once per site, not once per scrape.

### Supported platforms

Greenhouse, Lever, Ashby, Workday, SmartRecruiters, SAP SuccessFactors, Oracle
Taleo, client-rendered (React/Vue/Angular) pages, and generic HTML. The first
five are read through their JSON APIs; the rest go through the ladder.

---

## Quick start

Three files you edit, one command you run.

| File | What goes in it |
|---|---|
| `config/portals.txt` | career page links, one per line |
| `config/skills.txt` | your skills, one per line |
| `config/preferences.yml` | role, years of experience, locations, filters |

You never say what kind of site a link is — the tool detects that itself
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters, SuccessFactors, Taleo, a
React app, or plain HTML) and picks the cheapest way to read it.

```bash
make run
```

It scans every portal and prints a table: what each one was detected as, how
many jobs it returned, which extraction tier was used, how many LLM calls it
cost, and how long it took — then your top matches, ranked.

```
PORTAL                TYPE               JOBS   NEW           TIER  LLM   TIME
Airtable              greenhouse           41    41            api    0   0.8s
Ramp                  ashby               123   123            api    0   1.5s
GitLab                greenhouse          183   183            api    0   2.6s
Northwind Labs        generic_html          8     8   css_selector    0   0.0s
5/5 portals returned jobs · 434 found · 434 new · 0 LLM calls
```

Useful flags: `--semantic` scores with the LLM and adds written reasoning
(slower, rate limited); `--top 30` lists more matches; `--notify` also sends
email. `make status` shows what is registered, `make reset` starts over.

Running it unattended on a schedule is a separate mode — see
[Background mode](#background-mode) — but get the scan right first.

## Configuration

Every setting lives in `.env`; see [.env.example](.env.example) for the full
list with defaults. The ones that matter most:

| Variable | Default | What it controls |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini`, `anthropic`, `openai`, or `null` to disable tier 5 entirely |
| `LLM_MODEL` | `gemini-3-flash-preview` | Model id, passed through verbatim |
| `LLM_DAILY_BUDGET_USD` | `5.0` | Hard ceiling; tier 5 stops when it is hit |
| `EXTRACTION_MIN_CONFIDENCE` | `0.6` | Score an extraction must clear to be trusted |
| `SELECTOR_REGENERATE_AFTER_FAILURES` | `3` | Consecutive failures before relearning |
| `MATCH_THRESHOLD` | `0.6` | Minimum score to notify |
| `SCRAPE_REQUESTS_PER_MINUTE_PER_DOMAIN` | `20` | Shared politeness budget per domain |
| `NOTIFY_CHANNELS` | `email` | Comma-separated: `email,slack,telegram` |

### Setting up the LLM

The default is **Gemini**, because it is the only one of the three providers
with a genuinely free tier, and because that tier is bounded by requests per
day rather than by spend — a good fit for a system whose model use is a rare
failure path.

1. Get a free key at <https://aistudio.google.com/apikey>
2. Put it in `.env` as `GEMINI_API_KEY=...` (`.env` is gitignored)
3. Verify it:

```bash
make check-llm
```

`scripts/check_llm.py` runs the *real* tier-5 path against a sample page —
reduce HTML → ask the model for selectors → run those selectors → score the
result — and reports which stage failed if any did.

**On picking a model.** Do not use a `gemini-2.5-*` model on a newly created
key. Google has gated the 2.5 series to pre-existing users ahead of its
[2026-10-16 shutdown](https://discuss.ai.google.dev/t/gemini-2-5-flash-deprecated-without-warning-earlier-than-shutdown-date/174217),
and the failure mode is nasty: `models.list()` still advertises them, so a
listing check passes and the first real call returns 404. That is why the
checker *invokes* the model rather than trusting the listing, and why it probes
alternatives and prints the exact `LLM_MODEL=` line to paste when the
configured one will not serve.

Verified working on a fresh free key: `gemini-3-flash-preview` (the default)
and `gemini-3.1-flash-lite`. `gemini-3-pro-preview` works but exhausts a free
key's quota quickly and is overkill for reading DOM structure.

Free-tier 429s are handled inside `GeminiProvider`: they are retried with
jittered backoff (honouring Google's own `retry_delay` hint) rather than
surfaced as errors, because on a free key throttling is the expected response
under load, not a fault — letting it through would count against the circuit
breaker and disable tier 5 over what is really "wait a moment".

**Switching provider.** `LLM_MODEL` is a plain config string and providers sit
behind an interface, so it is a two-line change:

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
```

The brief specified "OpenAI GPT-5.5"; no model by that identifier exists, which
is exactly why the provider is abstracted and the model id is not hardcoded.

**Turning it off.** `LLM_PROVIDER=null` disables tier 5 and semantic matching
entirely. The other four extraction tiers and the rule matcher keep working —
useful for running at zero marginal cost, and for proving a site is scraped
deterministically.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/companies` | Register a career URL |
| `GET` | `/api/v1/companies` | List, filter by ATS / active |
| `GET` | `/api/v1/companies/{id}` | Detail plus last-run stats |
| `PATCH` | `/api/v1/companies/{id}` | Change name, cadence, strategy |
| `DELETE` | `/api/v1/companies/{id}` | Soft delete (jobs retained) |
| `POST` | `/api/v1/scan` | Scan due companies, or one |
| `POST` | `/api/v1/rescan` | Force a scan; `force_llm` to relearn |
| `GET` | `/api/v1/runs` | Recent scan telemetry |
| `GET` | `/api/v1/jobs` | Paginated, filterable |
| `GET` | `/api/v1/jobs/new` | First seen within a window, with scores |
| `GET` | `/api/v1/jobs/{id}` | Detail with match reasoning |
| `POST` | `/api/v1/profile` | Create/replace |
| `PUT` | `/api/v1/profile` | Partial update |
| `GET` | `/api/v1/notifications` | Delivery log, failures included |
| `GET` | `/health` | Liveness plus dependency readiness |
| `GET` | `/metrics` | Prometheus exposition |
| `GET` | `/metrics/summary` | Human-readable aggregates incl. LLM spend |

---

## Layout

```
app/
  api/            REST endpoints and dependencies
  core/           config, logging, metrics, errors
  database/       engine, session, base, portable column types
  models/         SQLAlchemy ORM
  schemas/        Pydantic request/response models
  repositories/   query construction, one per aggregate
  scrapers/       fetcher, browser pool, rate limiting, robots, adapters
  extractors/     the ladder: embedded JSON, selectors, LLM, validation
  learning/       selector generation, versioning, health tracking
  llm/            provider interface, budget breaker, prompts
  normalization/  dates, locations, salaries, hashing
  matcher/        rule and semantic matching
  notifications/  channels, templates, dispatcher
  services/       scan orchestration, company registration
  scheduler/      Celery app, tasks, Beat schedule
  utils/          text, URL, time helpers
alembic/          migrations
tests/            unit and integration suites
```

Dependencies point one way only (`api`/`scheduler` → `services` → domain
modules → `repositories` → `models`). There are no cycles.

---

## Testing

```bash
make test
```

Unit tests run against in-memory SQLite — the models declare Postgres types
with SQLite variants so the whole suite runs without a database container.

```bash
make test-cov
```

Coverage spans normalisation and date/salary/location parsing, URL
canonicalisation and hashing, selector and XPath extraction, embedded JSON,
HTML reduction, extraction scoring, every ATS adapter against canned payloads,
ATS detection, LLM response parsing and budget enforcement, selector learning
and regeneration, rule matching and vetoes, notification rendering and
idempotency, the full ladder, an end-to-end scan, and the REST API.

---

## Operations

**Hosted, free, automatic.** [HOSTING.md](HOSTING.md) sets the scan up on
GitHub Actions: every 30 minutes, emailing new postings via Resend, with no
server and no cost. A scan needs neither Redis nor Postgres — SQLite holds the
state and the LLM budget degrades to a per-process counter — so a cron runner
is the whole deployment.

**Structured logs.** JSON in production (`LOG_JSON=true`). A scan binds
`company_id` and `run_id` into the context, so every line beneath it carries
them without being passed a logger.

**Metrics.** `/metrics` exposes counters for pages scraped, extraction attempts
by tier and outcome, jobs found/new/duplicate, LLM calls, tokens and spend,
selector confidence per domain, and notification outcomes. Per-company detail
deliberately stays out of labels (cardinality) and lives in `scrape_runs`,
queryable via `/api/v1/runs`.

**Politeness.** robots.txt is honoured, requests are rate limited per domain
through a Redis token bucket shared by all workers, user agents and proxies are
pinned per domain, and retries use exponential backoff with full jitter.

**Failure handling.** Transient errors retry; a company that fails repeatedly
backs off exponentially and is deactivated (not deleted) after
`SCRAPE_MAX_CONSECUTIVE_FAILURES`. If the LLM budget is exhausted the breaker
opens and scans continue on tiers 1–4.

---

## Known limitations

- **Single-user in practice.** The `users` table and its foreign keys exist and
  are real, but there is no authentication; `api/deps.py` resolves "the caller"
  to the first profile. Adding auth means changing that one function.
- **Location parsing is best-effort.** The country list covers the markets these
  boards serve, not the world. The raw string is always retained alongside the
  parse, so a mis-parse degrades matching rather than losing information.
- **Taleo and SuccessFactors are the weakest adapters.** Both platforms are
  heavily themed per tenant; the built-in selectors cover the common
  generations, and anything else falls through to the learner.
- **No vector search.** Semantic matching is an LLM call per shortlisted job.
  Embeddings would be cheaper at volume; `matcher/base.py` is the interface to
  implement.
