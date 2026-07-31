"""ATS detection.

Answers "what is this page?" before anything tries to scrape it, in three
escalating stages so the cheap answer is reached without a request:

1. **Hostname.** ``boards.greenhouse.io/acme`` needs no network at all.
2. **Page body.** Meta tags, script sources, embed markers, DOM hints — one
   HTTP GET, no render.
3. **Rendered body.** Only when the served HTML is an empty shell.

The result is cached on the company row. Re-detection happens when extraction
starts failing, not on every scan: sites change ATS rarely, and paying an extra
request per company per run to confirm what we already know would be a
substantial share of the total request budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import FetchError
from app.core.logging import get_logger
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters import ashby, greenhouse, lever, smartrecruiters
from app.scrapers.fetcher import Fetcher
from app.scrapers.registry import ADAPTERS

logger = get_logger(__name__)

#: Board-token extractors for the platforms that have one. Detecting the
#: platform is only half the job — without the token, an API-backed adapter
#: cannot use its API and silently degrades to HTML scraping.
_TOKEN_EXTRACTORS = {
    ATSType.GREENHOUSE: greenhouse.extract_board_token,
    ATSType.LEVER: lever.extract_board_token,
    ATSType.ASHBY: ashby.extract_board_token,
    ATSType.SMARTRECRUITERS: smartrecruiters.extract_board_token,
}


@dataclass(slots=True)
class DetectionResult:
    ats_type: ATSType
    strategy: ScrapingStrategy
    board_token: str | None = None
    api_endpoint: str | None = None
    confidence: float = 0.0
    #: How we arrived at the answer: ``hostname`` | ``body`` | ``rendered`` |
    #: ``fallback``. Logged so that a wrong detection can be traced to the
    #: evidence that produced it.
    evidence: str = "fallback"

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.7


def detect(url: str, fetcher: Fetcher) -> DetectionResult:
    """Identify the platform behind ``url``."""

    host_hit = detect_from_hostname(url)
    if host_hit is not None:
        host_hit.board_token = _token_for(host_hit.ats_type, url, "")
        logger.info(
            "detect.hostname", url=url, ats=host_hit.ats_type, token=host_hit.board_token
        )
        return host_hit

    try:
        result = fetcher.fetch(url, strategy=ScrapingStrategy.HTTP)
    except FetchError as exc:
        # Cannot see the page, so cannot classify it. Generic HTML with AUTO
        # strategy is the honest answer: it will render if needed and the
        # ladder will work it out.
        logger.warning("detect.fetch_failed", url=url, error=str(exc))
        return DetectionResult(
            ats_type=ATSType.GENERIC_HTML,
            strategy=ScrapingStrategy.AUTO,
            confidence=0.1,
            evidence="fallback",
        )

    body_hit = detect_from_body(result.text, url, evidence="body")
    if body_hit is not None:
        return body_hit

    # An empty shell hides its markers in the JavaScript bundle; render and
    # look again before giving up on identifying the platform.
    if result.looks_like_spa:
        try:
            rendered = fetcher.fetch(url, strategy=ScrapingStrategy.PLAYWRIGHT)
        except FetchError as exc:
            logger.warning("detect.render_failed", url=url, error=str(exc))
        else:
            rendered_hit = detect_from_body(rendered.text, url, evidence="rendered")
            if rendered_hit is not None:
                return rendered_hit
            return DetectionResult(
                ats_type=ATSType.CUSTOM_REACT,
                strategy=ScrapingStrategy.PLAYWRIGHT,
                confidence=0.6,
                evidence="rendered",
            )

        return DetectionResult(
            ats_type=ATSType.CUSTOM_REACT,
            strategy=ScrapingStrategy.PLAYWRIGHT,
            confidence=0.4,
            evidence="body",
        )

    return DetectionResult(
        ats_type=ATSType.GENERIC_HTML,
        strategy=ScrapingStrategy.HTTP,
        confidence=0.5,
        evidence="body",
    )


def detect_from_hostname(url: str) -> DetectionResult | None:
    """Free classification for ATS-hosted boards."""
    for adapter in ADAPTERS:
        if adapter.host_markers and adapter.matches_host(url):
            return DetectionResult(
                ats_type=adapter.ats_type,
                strategy=adapter.default_strategy,
                confidence=0.95,
                evidence="hostname",
            )
    return None


def detect_from_body(
    body: str, url: str, *, evidence: str = "body"
) -> DetectionResult | None:
    """Classify from page source.

    ``CustomReactScraper`` is skipped here: its markers identify a rendering
    technology, not a platform, and letting them match would classify every
    React-built Greenhouse embed as "custom" — the more expensive and less
    accurate of the two answers.
    """
    for adapter in ADAPTERS:
        if adapter.ats_type is ATSType.CUSTOM_REACT:
            continue
        if adapter.body_markers and adapter.matches_body(body):
            token = _token_for(adapter.ats_type, url, body)
            return DetectionResult(
                ats_type=adapter.ats_type,
                strategy=adapter.default_strategy,
                board_token=token,
                confidence=0.85 if token else 0.75,
                evidence=evidence,
            )
    return None


def _token_for(ats_type: ATSType, url: str, body: str) -> str | None:
    extractor = _TOKEN_EXTRACTORS.get(ats_type)
    if extractor is None:
        return None
    try:
        return extractor(url, body)
    except Exception:  # pragma: no cover - defensive; regexes should not raise
        logger.debug("detect.token_extraction_failed", ats=ats_type, url=url)
        return None
