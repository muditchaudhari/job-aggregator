"""URL handling.

Canonicalisation matters more than it looks: the URL is one of the four inputs
to the deduplication hash (AD-6), so any instability here shows up directly as
false "new job" notifications.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

#: Parameters that identify the *referrer*, not the *resource*. Boards re-tag
#: these on every render, which is precisely the churn that made naive URL
#: diffing unusable.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gh_src",
        "gh_jid",
        "lever-source",
        "lever-origin",
        "source",
        "src",
        "ref",
        "referrer",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "trk",
        "trackingId",
        "_ga",
    }
)

#: Suffixes that are not registrable on their own; ``foo.co.uk`` must keep
#: three labels while ``foo.com`` keeps two. A full public-suffix list is
#: overkill here — these cover the ATS-hosting domains we actually see.
MULTI_PART_TLDS = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.za",
        "com.au",
        "com.br",
        "com.sg",
        "com.mx",
        "com.tr",
    }
)


def registrable_domain(url: str) -> str:
    """Return the domain used as the selector and rate-limit key.

    ``https://boards.greenhouse.io/acme?x=1`` -> ``greenhouse.io``

    Deliberately collapses subdomains: one learned strategy should serve every
    board on a shared ATS host, and one rate-limit bucket should cover every
    company hosted there.
    """
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def canonicalize_url(url: str, *, base: str | None = None) -> str:
    """Strip the noise that makes an unchanged posting look new.

    Removes tracking parameters and fragments, lowercases the host, drops a
    default port, and sorts the surviving query parameters so that ordering
    differences between renders do not change the hash.
    """
    if base:
        url = urljoin(base, url)
    parsed = urlparse(url.strip())

    host = (parsed.hostname or "").lower()
    if parsed.port and not (
        (parsed.scheme == "https" and parsed.port == 443)
        or (parsed.scheme == "http" and parsed.port == 80)
    ):
        host = f"{host}:{parsed.port}"

    query = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k not in TRACKING_PARAMS
    )

    # The trailing slash is preserved. It is not a tracking artefact: to a
    # server "/en/jobs" and "/en/jobs/" are different resources, and stripping
    # it turned Uber's board from a 200 into a 403. The only reason to remove
    # it would be tidier dedup keys, which is not worth being unable to fetch
    # the page at all.
    path = parsed.path or "/"

    return urlunparse(
        (parsed.scheme or "https", host, path, "", urlencode(query), "")
    )


def absolutize(href: str | None, base_url: str) -> str | None:
    """Resolve a possibly-relative href against the page it was found on."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    return urljoin(base_url, href)


def same_site(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)
