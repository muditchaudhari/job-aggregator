"""Playwright lifecycle management.

One browser per worker process, one fresh context per page (AD-9). Launching a
browser costs 1–2 s and ~100 MB; creating a context costs ~10 ms. Reusing the
browser is the big win, and *not* reusing the context is what stops cookies,
localStorage, and service workers leaking between unrelated companies.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.core.config import get_settings
from app.core.errors import TransientFetchError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Thread-local, not process-global. Playwright's synchronous API binds its
#: event loop to the creating thread, so sharing one browser across the scan
#: pool raises "Playwright object is used from a different thread" — or worse,
#: silently interleaves two pages' state.
_local = threading.local()


def _launch() -> Any:
    """Start Playwright and launch a browser for the calling thread."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise TransientFetchError(
            "Playwright is not installed; rendering unavailable"
        ) from exc

    settings = get_settings()
    _local.playwright = sync_playwright().start()
    browser_type = getattr(_local.playwright, settings.playwright_browser)
    _local.browser = browser_type.launch(
        headless=settings.playwright_headless,
        args=[
            # Chromium's default /dev/shm is 64 MB in most containers, which it
            # will exhaust and then crash on content-heavy pages.
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    logger.info("browser.launched", engine=settings.playwright_browser)
    atexit.register(shutdown_browser)
    return _local.browser


def get_browser() -> Any:
    if getattr(_local, "browser", None) is None:
        _launch()
    return _local.browser


@contextmanager
def browser_page(
    *, user_agent: str | None = None, proxy: str | None = None
) -> Iterator[Any]:
    """Yield a page in a disposable context.

    Resource blocking is applied here rather than per call site: images, fonts,
    and media are never needed to read a job listing, and skipping them
    typically halves render time on a marketing-heavy careers page.
    """
    settings = get_settings()
    browser = get_browser()

    context_options: dict[str, Any] = {
        "viewport": {
            "width": settings.playwright_viewport_width,
            "height": settings.playwright_viewport_height,
        },
        "ignore_https_errors": True,
    }
    if user_agent:
        context_options["user_agent"] = user_agent
    if proxy:
        context_options["proxy"] = {"server": proxy}

    context = browser.new_context(**context_options)
    try:
        blocked = settings.blocked_resource_types
        if blocked:
            context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in blocked
                    else route.continue_()
                ),
            )
        page = context.new_page()
        page.set_default_timeout(settings.scrape_render_timeout_seconds * 1000)
        try:
            yield page
        finally:
            page.close()
    finally:
        context.close()


def shutdown_browser() -> None:
    """Tear down this thread's browser.

    Registered with ``atexit`` because Celery's warm shutdown does not run
    module-level finalisers, and an orphaned Chromium keeps its memory.
    """
    browser = getattr(_local, "browser", None)
    if browser is not None:
        try:
            browser.close()
        except Exception:  # pragma: no cover - best effort during shutdown
            logger.debug("browser.close_failed")
        _local.browser = None
    playwright = getattr(_local, "playwright", None)
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:  # pragma: no cover
            logger.debug("playwright.stop_failed")
        _local.playwright = None
