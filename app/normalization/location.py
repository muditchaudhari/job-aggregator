"""Location parsing.

Turns free text into ``(city, region, country, remote_type)``. Location strings
are among the messiest fields on a job board — "Remote (US)", "Bengaluru,
Karnataka, India", "London / Hybrid", "Multiple Locations" — and the parse is
best-effort by nature.

The raw string is always preserved on the ``Job`` row alongside the parse, so a
mis-parse degrades matching rather than destroying information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import RemoteType
from app.utils.text import clean_text, normalize_key

_REMOTE_WORDS = ("remote", "work from home", "wfh", "telecommute", "anywhere", "virtual")
_HYBRID_WORDS = ("hybrid", "flexible", "partially remote", "part remote")
_ONSITE_WORDS = ("on-site", "onsite", "in office", "in-office", "on site")

#: Non-locations that appear in the location field. Parsing these as cities
#: would populate ``location_city`` with "Multiple Locations" and make every
#: city filter useless.
_PLACEHOLDERS = (
    "multiple locations", "various", "several locations", "worldwide",
    "global", "any location", "tbd", "n/a", "not specified", "-",
)

#: Enough coverage for the countries that appear on the boards we target;
#: unmatched trailing segments simply stay in ``region``.
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom",
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "india": "India", "canada": "Canada", "australia": "Australia",
    "germany": "Germany", "deutschland": "Germany", "france": "France",
    "spain": "Spain", "italy": "Italy", "netherlands": "Netherlands",
    "the netherlands": "Netherlands", "ireland": "Ireland", "poland": "Poland",
    "portugal": "Portugal", "sweden": "Sweden", "norway": "Norway",
    "denmark": "Denmark", "finland": "Finland", "switzerland": "Switzerland",
    "austria": "Austria", "belgium": "Belgium", "singapore": "Singapore",
    "japan": "Japan", "china": "China", "brazil": "Brazil", "mexico": "Mexico",
    "israel": "Israel", "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "south africa": "South Africa", "new zealand": "New Zealand",
}

#: US state and Indian state abbreviations seen in "City, ST" formats.
_REGION_ABBREVIATIONS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

_SPLIT_RE = re.compile(r"\s*(?:,|\||/|•|;|–|—| - )\s*")
_PARENS_RE = re.compile(r"\(([^)]*)\)")


@dataclass(slots=True)
class ParsedLocation:
    raw: str | None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    remote_type: RemoteType = RemoteType.UNKNOWN


def parse_location(value: str | None, *, hint: str | None = None) -> ParsedLocation:
    """Parse a location string, optionally biased by an explicit remote hint.

    ``hint`` comes from structured fields some APIs provide (Lever's
    ``workplaceType``, Ashby's ``isRemote``). Those are authoritative and beat
    anything inferred from the text.
    """
    raw = clean_text(value)
    if not raw:
        return ParsedLocation(raw=None, remote_type=_remote_from_hint(hint))

    remote_type = _remote_from_hint(hint)
    if remote_type is RemoteType.UNKNOWN:
        remote_type = _detect_remote(raw)

    working = raw
    # "Remote (US)" and "London (Hybrid)" both put meaning in parentheses;
    # keep the contents as a segment rather than discarding it.
    parenthetical = _PARENS_RE.findall(working)
    working = _PARENS_RE.sub(" ", working)

    segments = [
        clean_text(segment)
        for segment in _SPLIT_RE.split(working) + parenthetical
        if clean_text(segment)
    ]
    segments = [
        segment
        for segment in segments
        if not _is_remote_word(segment) and normalize_key(segment) not in ("",)
    ]

    if not segments or all(normalize_key(s) in _PLACEHOLDERS for s in segments):
        return ParsedLocation(raw=raw, remote_type=remote_type)

    country = None
    for index in range(len(segments) - 1, -1, -1):
        resolved = _COUNTRY_ALIASES.get(normalize_key(segments[index]))
        if resolved:
            country = resolved
            segments.pop(index)
            break

    city = segments[0] if segments else None
    region = None
    if len(segments) >= 2:
        region = segments[1]
    elif len(segments) == 1 and country is None:
        region = None

    # "San Francisco, CA" — the abbreviation is a region, not a city, and if it
    # ended up first the string had no city at all.
    if city and normalize_key(city) in _REGION_ABBREVIATIONS and not region:
        region, city = city, None

    return ParsedLocation(
        raw=raw,
        city=_titleish(city),
        region=_titleish(region),
        country=country,
        remote_type=remote_type,
    )


def _remote_from_hint(hint: str | None) -> RemoteType:
    if not hint:
        return RemoteType.UNKNOWN
    normalized = normalize_key(hint)
    if any(word in normalized for word in ("remote", "telecommute", "anywhere")):
        return RemoteType.REMOTE
    if "hybrid" in normalized:
        return RemoteType.HYBRID
    if any(word in normalized for word in ("onsite", "on site", "in office")):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


def _detect_remote(text: str) -> RemoteType:
    normalized = normalize_key(text)
    # Hybrid is checked first: "Hybrid Remote" is hybrid, and the reverse
    # ordering would classify it as fully remote.
    if any(word in normalized for word in (normalize_key(w) for w in _HYBRID_WORDS)):
        return RemoteType.HYBRID
    if any(word in normalized for word in (normalize_key(w) for w in _REMOTE_WORDS)):
        return RemoteType.REMOTE
    if any(word in normalized for word in (normalize_key(w) for w in _ONSITE_WORDS)):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


def _is_remote_word(segment: str) -> bool:
    normalized = normalize_key(segment)
    return normalized in {
        normalize_key(word)
        for word in _REMOTE_WORDS + _HYBRID_WORDS + _ONSITE_WORDS
    }


def _titleish(value: str | None) -> str | None:
    """Tidy capitalisation without mangling acronyms.

    ``str.title()`` would turn "USA" into "Usa" and "NYC" into "Nyc", so words
    that are already all-caps are left alone.
    """
    if not value:
        return None
    words = []
    for word in value.split():
        words.append(word if word.isupper() and len(word) <= 4 else word.capitalize())
    return " ".join(words) or None


def has_geography(parsed: ParsedLocation) -> bool:
    """Does this location name a *place*, beyond just saying "remote"?

    ``Remote`` alone does not. ``Remote — United States`` and
    ``New York, NY (HQ)`` do, and that distinction is the whole point: a job
    flagged remote by its ATS is very often remote *within one country*.
    """
    return bool(parsed.city or parsed.region or parsed.country)


def split_preferences(preferred: list[str]) -> tuple[list[str], bool]:
    """Separate real place names from the "Remote" marker.

    Listing "Remote" is a statement about *working arrangement*, not about
    geography, but it reads as a place and gets substring-matched like one.
    Left mixed in, it made ``Remote - United States`` match the word "Remote"
    and sail past a Bangalore-only filter — the job is remote, just not remote
    to you. Keeping the two apart is what makes the geography check mean
    anything.
    """
    places = [p for p in preferred if not _is_remote_word(p)]
    wants_remote = len(places) != len(preferred)
    return places, wants_remote


def location_is_acceptable(
    parsed: ParsedLocation, preferred: list[str], *, include_unknown: bool
) -> bool:
    """Can the candidate actually take this job, location-wise?"""
    if not preferred:
        return True

    places, wants_remote = split_preferences(preferred)

    # A named place you asked for always wins, remote or not.
    if places and matches_preferred(parsed, places):
        return True

    if parsed.remote_type is RemoteType.REMOTE:
        # Genuinely location-agnostic: nothing in the text ties it anywhere,
        # so anyone can take it — provided remote is acceptable to you.
        if not has_geography(parsed):
            return wants_remote or not places
        # Remote, but tied to a country that is not one of yours.
        return False

    if not parsed.raw:
        return include_unknown
    return not places


def matches_preferred(parsed: ParsedLocation, preferred: list[str]) -> bool:
    """Does a parsed location satisfy any of the user's preferred locations?

    Compares against the raw string as well as the parsed parts, because users
    write "Bay Area" and boards write "San Francisco, CA" — and the raw string
    is the one place a substring match has a chance of connecting them.
    """
    if not preferred:
        return True
    haystacks = [
        normalize_key(part)
        for part in (parsed.raw, parsed.city, parsed.region, parsed.country)
        if part
    ]
    for wanted in preferred:
        target = normalize_key(wanted)
        if target and any(target in haystack for haystack in haystacks):
            return True
    return False
