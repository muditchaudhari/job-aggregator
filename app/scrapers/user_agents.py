"""User-agent selection.

Rotation is deliberately *stable per domain* rather than random per request.
A client that presents four different browsers to the same host within a minute
looks far more like a bot than one that consistently presents the same browser,
so random-per-request rotation is actively counterproductive.
"""

from __future__ import annotations

import zlib

from app.core.config import get_settings

DESKTOP_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
)

DEFAULT_USER_AGENT = DESKTOP_USER_AGENTS[0]


def user_agent_for(domain: str) -> str:
    """Deterministic per-domain agent.

    CRC32 rather than ``hash()``: Python randomises string hashing per process,
    which would make the choice differ between workers and reintroduce exactly
    the inconsistency this function exists to avoid.
    """
    if not get_settings().scrape_rotate_user_agents:
        return DEFAULT_USER_AGENT
    if not domain:
        return DEFAULT_USER_AGENT
    index = zlib.crc32(domain.encode()) % len(DESKTOP_USER_AGENTS)
    return DESKTOP_USER_AGENTS[index]


def default_headers(domain: str, *, referer: str | None = None) -> dict[str, str]:
    # Accept-Encoding is deliberately absent. httpx sets it from the codecs it
    # can actually decode; hardcoding "br" advertised Brotli support we did not
    # have, so any origin that honoured it (Ashby does) returned a body that
    # decoded to binary noise and failed to parse — as a *content* error, which
    # made it look like the API had changed shape.
    headers = {
        "User-Agent": user_agent_for(domain),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def proxy_for(domain: str) -> str | None:
    """Pin a domain to one proxy.

    Same reasoning as the user agent: a session that hops between source IPs
    mid-crawl is a stronger bot signal than a session that does not.
    """
    proxies = get_settings().proxies
    if not proxies:
        return None
    return proxies[zlib.crc32(domain.encode()) % len(proxies)]
