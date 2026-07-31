"""robots.txt handling.

Implements RFC 9309 matching rather than deferring to
``urllib.robotparser``, which resolves conflicts by *file order* and so gets
the single most common real-world pattern backwards::

    User-agent: *
    Disallow: /
    Allow: /careers

Under the RFC the **longest matching pattern** wins, so ``/careers`` is
permitted. urllib returns the first rule that matches — ``Disallow: /`` — and
refuses the whole site. Microsoft, among many others, publishes exactly this,
and the effect is silently declining to fetch pages we are explicitly invited
to fetch.

Files are cached per registrable domain, because fetching robots.txt before
every page would double our request count against the very sites we are trying
to be polite to.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.urls import registrable_domain

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 3600
_DEFAULT_AGENT = "*"


@dataclass(slots=True)
class _Rule:
    pattern: str
    allow: bool
    #: Specificity for conflict resolution: the RFC compares the length of the
    #: rule's path, so a longer pattern is a more specific instruction.
    length: int = 0
    regex: re.Pattern[str] | None = None


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a robots path pattern into a regex.

    Only two metacharacters exist: ``*`` (any sequence) and ``$`` (end of
    URL). Everything else is literal, so it must be escaped — a path
    containing ``.`` or ``+`` would otherwise match far more than it should.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(segment) for segment in body.split("*")]
    expression = ".*".join(parts)
    return re.compile(f"^{expression}{'$' if anchored_end else ''}")


class RobotsFile:
    """A parsed robots.txt, queried per user agent."""

    def __init__(self, text: str) -> None:
        self._groups: list[_Group] = []
        self._parse(text)

    def _parse(self, text: str) -> None:
        current: _Group | None = None
        expecting_agent = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            key, value = field_name.strip().lower(), value.strip()

            if key == "user-agent":
                # Consecutive user-agent lines share one group of rules.
                if current is None or not expecting_agent:
                    current = _Group()
                    self._groups.append(current)
                current.agents.append(value.lower())
                expecting_agent = True
                continue

            if current is None:
                continue
            expecting_agent = False

            if key in ("allow", "disallow"):
                # "Disallow:" with an empty value means "nothing is
                # disallowed" and must not be treated as a rule matching "".
                if key == "disallow" and not value:
                    continue
                current.rules.append(
                    _Rule(
                        pattern=value,
                        allow=(key == "allow"),
                        length=len(value),
                        regex=_compile(value),
                    )
                )
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.crawl_delay = float(value)

    def _group_for(self, user_agent: str) -> _Group | None:
        """Most specific matching group, falling back to the wildcard one.

        A named agent's group replaces the ``*`` group entirely rather than
        adding to it — that is what the RFC specifies, and merging them would
        apply rules never intended for us.
        """
        agent = user_agent.lower()
        best: _Group | None = None
        best_length = -1

        for group in self._groups:
            for candidate in group.agents:
                if candidate == _DEFAULT_AGENT:
                    if best is None:
                        best, best_length = group, 0
                elif candidate in agent and len(candidate) > best_length:
                    best, best_length = group, len(candidate)
        return best

    def can_fetch(self, path: str, user_agent: str = _DEFAULT_AGENT) -> bool:
        group = self._group_for(user_agent)
        if group is None or not group.rules:
            return True

        target = unquote(path) or "/"
        winner: _Rule | None = None
        for rule in group.rules:
            # Longest pattern wins; on an exact tie, Allow beats Disallow.
            if (rule.regex is not None and rule.regex.match(target)) and (
                winner is None
                or rule.length > winner.length
                or (rule.length == winner.length and rule.allow)
            ):
                winner = rule
        return winner.allow if winner is not None else True

    def crawl_delay(self, user_agent: str = _DEFAULT_AGENT) -> float | None:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None


class RobotsCache:
    """In-process cache of parsed robots.txt files.

    Per-process rather than in Redis: the file is small, and a few workers each
    holding their own copy for an hour is cheaper than the serialisation dance.
    """

    def __init__(self, user_agent: str = _DEFAULT_AGENT) -> None:
        self._user_agent = user_agent
        self._entries: dict[str, tuple[RobotsFile | None, float]] = {}

    def _load(self, url: str) -> RobotsFile | None:
        parsed = urlparse(url)
        robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
        try:
            response = httpx.get(robots_url, timeout=10.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            logger.debug("robots.unreachable", url=robots_url, error=str(exc))
            return None

        if response.status_code >= 400:
            # 404 means "no rules published", which is permission by omission.
            # A 5xx is ambiguous; treating it as permissive matches every major
            # crawler and avoids a site outage blocking us after it recovers.
            return None
        return RobotsFile(response.text)

    def get(self, url: str) -> RobotsFile | None:
        domain = registrable_domain(url)
        cached = self._entries.get(domain)
        now = time.time()
        if cached and now - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]

        parsed = self._load(url)
        self._entries[domain] = (parsed, now)
        return parsed

    def is_allowed(self, url: str, user_agent: str | None = None) -> bool:
        if not get_settings().scrape_respect_robots:
            return True
        robots = self.get(url)
        if robots is None:
            return True
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return robots.can_fetch(path, user_agent or self._user_agent)

    def crawl_delay(self, url: str, user_agent: str | None = None) -> float | None:
        robots = self.get(url)
        if robots is None:
            return None
        return robots.crawl_delay(user_agent or self._user_agent)


_cache: RobotsCache | None = None


def get_robots_cache() -> RobotsCache:
    global _cache
    if _cache is None:
        _cache = RobotsCache()
    return _cache
