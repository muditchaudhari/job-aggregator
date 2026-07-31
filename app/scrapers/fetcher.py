"""Page fetching.

One entry point — :meth:`Fetcher.fetch` — that decides between a plain HTTP GET
and a headless render, applies robots, rate limiting, user-agent and proxy
selection, and retries transient failures with exponential backoff and jitter.

Everything above this module works with :class:`FetchResult` and never knows
whether a browser was involved.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import (
    BlockedError,
    PermanentFetchError,
    RateLimitedError,
    RobotsDisallowedError,
    TransientFetchError,
)
from app.core.logging import get_logger
from app.models.enums import ScrapingStrategy
from app.scrapers.rate_limit import get_rate_limiter
from app.scrapers.robots import get_robots_cache
from app.scrapers.user_agents import default_headers, proxy_for, user_agent_for
from app.utils.urls import registrable_domain

logger = get_logger(__name__)

#: Whether HTTP/2 can be negotiated. ``httpx`` raises ``ImportError`` from the
#: client constructor when ``http2=True`` and ``h2`` is absent, which would
#: turn a missing optional dependency into a total scraping outage.
try:  # pragma: no cover - import-time capability probe
    import h2  # noqa: F401

    HTTP2_AVAILABLE = True
except ImportError:  # pragma: no cover
    HTTP2_AVAILABLE = False
    logger.warning(
        "fetcher.http2_unavailable",
        hint="pip install 'httpx[http2]' — falling back to HTTP/1.1",
    )

#: Markers that a "200 OK" is actually a bot wall. These pages are valid HTML
#: and would otherwise sail through extraction as "zero jobs found", which
#: looks identical to a broken selector and would trigger a pointless — and
#: expensive — relearn.
#: Unambiguous: a page saying these *is* a challenge page.
_BOT_WALL_MARKERS = (
    "are you a human",
    "verify you are human",
    "cf-browser-verification",
    "checking your browser",
    "access denied",
    "request unsuccessful",
    "incapsula",
    "px-captcha",
)

#: Suggestive but not conclusive. Plenty of legitimate pages load a CAPTCHA
#: widget for their *login* or *apply* form while serving full content to
#: everyone else — Microsoft's careers site does. Treating the bare word as a
#: wall rejected a page that had rendered perfectly, so these only count when
#: the page is also devoid of content.
_WEAK_BOT_WALL_MARKERS = ("captcha", "recaptcha", "bot detection", "unusual traffic")

#: Signals that a document is a client-rendered shell: near-empty body, but a
#: large JavaScript payload. Used to escalate HTTP → Playwright automatically.
_SPA_MARKERS = (
    "__next_data__",
    "id=\"root\"",
    "id='root'",
    "id=\"__nuxt\"",
    "ng-version",
    "data-reactroot",
)


@dataclass(slots=True)
class FetchResult:
    """Everything downstream needs about a retrieved page."""

    url: str
    final_url: str
    status_code: int
    text: str
    content_type: str
    strategy: ScrapingStrategy
    fetch_ms: int
    render_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    #: Parsed body when the response was JSON — the tier-1 path.
    json_body: Any | None = None

    @property
    def is_json(self) -> bool:
        return self.json_body is not None

    @property
    def looks_like_spa(self) -> bool:
        """Heuristic: did the server send a shell instead of content?

        Both halves matter. A short document alone might just be a small page;
        a framework marker alone might be a server-rendered React app that is
        perfectly readable. It is the combination — framework present, content
        absent — that means a render is required.
        """
        lowered = self.text.lower()
        has_marker = any(marker in lowered for marker in _SPA_MARKERS)
        return has_marker and self._text_volume < 2000

    @property
    def looks_blocked(self) -> bool:
        lowered = self.text.lower()
        if any(marker in lowered[:20_000] for marker in _BOT_WALL_MARKERS):
            return True
        # A challenge page is tiny. A content-rich page that merely *loads* a
        # CAPTCHA widget is not being blocked.
        if any(marker in lowered for marker in _WEAK_BOT_WALL_MARKERS):
            return self._text_volume < 2000
        return False

    @property
    def _text_volume(self) -> int:
        """Rough count of visible characters, discounting markup."""
        lowered = self.text.lower()
        return len(lowered) - lowered.count("<") * 12


class Fetcher:
    """Retrieves career pages under the platform's politeness rules."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._owns_client = client is None
        self._fallback_client: httpx.Client | None = None

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
        if self._fallback_client is not None:
            self._fallback_client.close()
            self._fallback_client = None

    def _http1_client(self) -> httpx.Client:
        """A deliberately HTTP/1.1 client, used to retry a 403.

        Some WAFs fingerprint the HTTP/2 handshake and reject clients whose
        h2 settings do not look like a real browser — Uber's job API answers
        HTTP/1.1 with 200 and HTTP/2 with 403, for byte-identical headers.
        Falling back costs one extra request on the rare 403 and turns a hard
        failure into a success.
        """
        if self._fallback_client is None:
            self._fallback_client = httpx.Client(
                timeout=self._settings.scrape_http_timeout_seconds,
                follow_redirects=True,
                http2=False,
            )
        return self._fallback_client

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._settings.scrape_http_timeout_seconds,
                follow_redirects=True,
                # HTTP/2 because several ATS CDNs downgrade or throttle HTTP/1.1
                # clients that do not look like browsers. Negotiated only when
                # `h2` is present: httpx raises on construction otherwise, and
                # losing HTTP/2 should cost us a little politeness with those
                # CDNs, not every scrape on the box.
                http2=HTTP2_AVAILABLE,
            )
        return self._client

    # -- Public API --------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        strategy: ScrapingStrategy = ScrapingStrategy.AUTO,
        headers: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
    ) -> FetchResult:
        """Fetch ``url``, escalating to a render when the HTML is a shell.

        ``AUTO`` starts with HTTP and escalates only if the response looks like
        an SPA or a bot wall. That ordering is the whole cost model: rendering
        every page would be correct and roughly 20× more expensive.
        """
        domain = registrable_domain(url)

        if not get_robots_cache().is_allowed(url, user_agent_for(domain)):
            raise RobotsDisallowedError("robots.txt disallows this path", url=url)

        if not get_rate_limiter().acquire(domain):
            raise RateLimitedError("rate limit budget exhausted", domain=domain)

        if strategy is ScrapingStrategy.PLAYWRIGHT:
            return self._render(url, wait_for_selector=wait_for_selector)

        result = self._retrying_http_get(url, headers=headers)

        if strategy is ScrapingStrategy.AUTO and (
            result.looks_like_spa or result.looks_blocked
        ):
            logger.info(
                "fetch.escalating_to_render",
                url=url,
                reason="spa" if result.looks_like_spa else "blocked",
            )
            return self._render(url, wait_for_selector=wait_for_selector)

        return result

    def fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Tier-1 helper for ATS APIs. Raises if the body is not JSON."""
        result = self._retrying_http_get(
            url,
            headers={**(headers or {}), "Accept": "application/json"},
            params=params,
        )
        if result.json_body is None:
            raise PermanentFetchError(
                "expected JSON response", url=url, content_type=result.content_type
            )
        return result.json_body

    def post_json(
        self, url: str, payload: Any, *, headers: dict[str, str] | None = None
    ) -> Any:
        """POST a JSON body and return the parsed response.

        Needed because not every ATS API is a GET: Workday's CXS endpoint takes
        its pagination and facet filters in a POST body. Rate limiting and
        robots still apply — a JSON endpoint is as much a request against the
        host as a page is.
        """
        domain = registrable_domain(url)
        if not get_rate_limiter().acquire(domain):
            raise RateLimitedError("rate limit budget exhausted", domain=domain)

        request_headers = {
            **default_headers(domain),
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        }

        last_error: Exception | None = None
        for attempt in range(1, self._settings.scrape_max_retries + 1):
            try:
                response = self._http_client().post(
                    url, json=payload, headers=request_headers
                )
                self._raise_for_status(response, url)
                return response.json()
            except (TransientFetchError, RateLimitedError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt == self._settings.scrape_max_retries:
                    break
                time.sleep(self._backoff(attempt))
            except (json.JSONDecodeError, ValueError) as exc:
                raise PermanentFetchError("response was not JSON", url=url) from exc
        assert last_error is not None
        raise last_error

    # -- Internals ---------------------------------------------------------

    def _retrying_http_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(1, self._settings.scrape_max_retries + 1):
            try:
                return self._http_get(url, headers=headers, params=params)
            except BlockedError as exc:
                # Before believing a 403, rule out HTTP/2 fingerprinting.
                if not HTTP2_AVAILABLE or exc.context.get("status") != 403:
                    raise
                logger.info("fetch.retrying_over_http1", url=url)
                return self._http_get(
                    url, headers=headers, params=params, force_http1=True
                )
            except (TransientFetchError, RateLimitedError) as exc:
                last_error = exc
                if attempt == self._settings.scrape_max_retries:
                    break
                delay = self._backoff(attempt)
                logger.warning(
                    "fetch.retrying",
                    url=url,
                    attempt=attempt,
                    delay=round(delay, 2),
                    error=str(exc),
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Full jitter rather than fixed exponential: when a site returns 503 to
        every worker at once, undithered backoff has them all retry in the same
        instant, repeatedly. Randomising the whole interval spreads the herd.
        """
        ceiling = self._settings.scrape_backoff_base_seconds * (2 ** (attempt - 1))
        return random.uniform(0, min(ceiling, 30.0))

    def _http_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        force_http1: bool = False,
    ) -> FetchResult:
        domain = registrable_domain(url)
        request_headers = {**default_headers(domain), **(headers or {})}
        proxy = proxy_for(domain)

        started = time.monotonic()
        try:
            if proxy:
                with httpx.Client(
                    timeout=self._settings.scrape_http_timeout_seconds,
                    follow_redirects=True,
                    proxy=proxy,
                ) as proxied:
                    response = proxied.get(url, headers=request_headers, params=params)
            else:
                client = self._http1_client() if force_http1 else self._http_client()
                response = client.get(url, headers=request_headers, params=params)
        except httpx.TimeoutException as exc:
            raise TransientFetchError("request timed out", url=url) from exc
        except httpx.HTTPError as exc:
            raise TransientFetchError("transport error", url=url, detail=str(exc)) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._raise_for_status(response, url)

        content_type = response.headers.get("content-type", "")
        json_body = None
        if "json" in content_type.lower():
            try:
                json_body = response.json()
            except (json.JSONDecodeError, ValueError):
                # A JSON content-type with an unparseable body is a broken
                # upstream, not a JSON page. Fall through and treat it as text.
                logger.debug("fetch.json_decode_failed", url=url)

        return FetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            text=response.text,
            content_type=content_type,
            strategy=ScrapingStrategy.HTTP,
            fetch_ms=elapsed_ms,
            headers=dict(response.headers),
            json_body=json_body,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, url: str) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in (401, 403):
            raise BlockedError("blocked by origin", url=url, status=status)
        if status == 429:
            raise RateLimitedError("throttled by origin", url=url)
        if status in (404, 410):
            raise PermanentFetchError("page is gone", url=url, status=status)
        if status >= 500:
            raise TransientFetchError("origin error", url=url, status=status)
        raise PermanentFetchError("unexpected status", url=url, status=status)

    def _render(self, url: str, *, wait_for_selector: str | None = None) -> FetchResult:
        from app.scrapers.browser import browser_page

        domain = registrable_domain(url)
        started = time.monotonic()

        with browser_page(
            user_agent=user_agent_for(domain), proxy=proxy_for(domain)
        ) as page:
            try:
                response = page.goto(
                    url,
                    wait_until=self._settings.playwright_wait_until,
                    timeout=self._settings.scrape_render_timeout_seconds * 1000,
                )
            except Exception as exc:
                raise TransientFetchError(
                    "render failed", url=url, detail=str(exc)
                ) from exc

            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, state="attached")
                except Exception:
                    # A missing selector is an extraction concern, not a fetch
                    # failure — hand the DOM up and let validation judge it.
                    logger.debug(
                        "render.selector_absent", url=url, selector=wait_for_selector
                    )

            html = page.content()
            final_url = page.url
            status = response.status if response is not None else 200

        render_ms = int((time.monotonic() - started) * 1000)
        result = FetchResult(
            url=url,
            final_url=final_url,
            status_code=status,
            text=html,
            content_type="text/html",
            strategy=ScrapingStrategy.PLAYWRIGHT,
            fetch_ms=render_ms,
            render_ms=render_ms,
        )
        if result.looks_blocked:
            raise BlockedError("bot wall present after render", url=url)
        return result
