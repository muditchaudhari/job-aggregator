"""Prometheus metrics.

Deliberately a small, fixed set. Metrics with unbounded label values (company
id, URL) are a cardinality bomb in Prometheus, so per-company detail lives in
the ``scrape_runs`` table and is queried through ``GET /metrics`` summary fields
or SQL — not through labels here.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

pages_scraped = Counter(
    "jap_pages_scraped_total",
    "Career pages fetched.",
    labelnames=("ats_type", "strategy", "outcome"),
    registry=REGISTRY,
)

scrape_duration = Histogram(
    "jap_scrape_duration_seconds",
    "Wall time for a single company scan.",
    labelnames=("ats_type",),
    buckets=(0.5, 1, 2, 5, 10, 20, 45, 90, 180),
    registry=REGISTRY,
)

render_duration = Histogram(
    "jap_render_duration_seconds",
    "Playwright render time.",
    buckets=(0.5, 1, 2, 5, 10, 20, 45),
    registry=REGISTRY,
)

jobs_found = Counter(
    "jap_jobs_found_total", "Postings returned by extraction.", registry=REGISTRY
)
jobs_new = Counter("jap_jobs_new_total", "Postings not seen before.", registry=REGISTRY)
jobs_duplicate = Counter(
    "jap_jobs_duplicate_total", "Postings discarded as duplicates.", registry=REGISTRY
)

extraction_attempts = Counter(
    "jap_extraction_attempts_total",
    "Extraction attempts by ladder tier and outcome.",
    labelnames=("tier", "outcome"),
    registry=REGISTRY,
)

selector_confidence = Gauge(
    "jap_selector_confidence",
    "Confidence of the active selector version, by domain.",
    labelnames=("website",),
    registry=REGISTRY,
)

selector_regenerations = Counter(
    "jap_selector_regenerations_total",
    "Selector versions created by the learner.",
    labelnames=("website",),
    registry=REGISTRY,
)

llm_calls = Counter(
    "jap_llm_calls_total",
    "LLM invocations.",
    labelnames=("provider", "purpose", "outcome"),
    registry=REGISTRY,
)

llm_tokens = Counter(
    "jap_llm_tokens_total",
    "Tokens consumed.",
    labelnames=("provider", "direction"),
    registry=REGISTRY,
)

llm_cost_usd = Counter(
    "jap_llm_cost_usd_total",
    "Estimated spend.",
    labelnames=("provider",),
    registry=REGISTRY,
)

notifications_sent = Counter(
    "jap_notifications_total",
    "Notification deliveries.",
    labelnames=("channel", "outcome"),
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)
