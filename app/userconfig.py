"""Reads the three files a user actually edits.

``config/portals.txt``      career page links, one per line
``config/skills.txt``       skills, one per line
``config/preferences.yml``  role, experience, location, filtering

Kept separate from ``core/config.py``, which is machine configuration
(database URLs, timeouts, API keys) read from the environment. These three are
the user's own inputs and are meant to be edited by hand, so the parsers are
forgiving: blank lines, comments and stray whitespace are all fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path("config")
PORTALS_FILE = CONFIG_DIR / "portals.txt"
SKILLS_FILE = CONFIG_DIR / "skills.txt"
PREFERENCES_FILE = CONFIG_DIR / "preferences.yml"


class ConfigError(Exception):
    """A user-facing configuration problem, phrased as something to go fix."""


@dataclass(slots=True)
class Portal:
    url: str
    name: str | None = None


@dataclass(slots=True)
class UserConfig:
    portals: list[Portal] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    email: str = ""
    full_name: str | None = None
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    years_experience: int | None = None
    max_seniority_gap: int = 1
    scan_every_minutes: int = 30
    remote_preference: str = "any"
    include_unknown_location: bool = True
    match_threshold: float = 0.6
    desired_salary_min: int | None = None
    salary_currency: str | None = None
    requires_visa_sponsorship: bool = False


def _read_lines(path: Path) -> list[str]:
    """Non-empty, non-comment lines. Inline ``#`` is *not* stripped — a URL can
    legitimately contain one, and so can a skill like ``C#``."""
    if not path.exists():
        raise ConfigError(f"missing {path} — see config/ for the expected files")
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def load_portals(path: Path = PORTALS_FILE) -> list[Portal]:
    portals: list[Portal] = []
    seen: set[str] = set()

    for line in _read_lines(path):
        url, _, name = line.partition("|")
        url, name = url.strip(), name.strip() or None
        if not url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{path}: not a URL — {line!r}\n"
                "  each line must start with http:// or https://"
            )
        # Duplicates in the file are a copy-paste slip, not an intent to scan
        # the same board twice.
        if url in seen:
            continue
        seen.add(url)
        portals.append(Portal(url=url, name=name))

    if not portals:
        raise ConfigError(f"{path} has no links in it — add at least one career page URL")
    return portals


def load_skills(path: Path = SKILLS_FILE) -> list[str]:
    skills, seen = [], set()
    for line in _read_lines(path):
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            skills.append(line)
    return skills


def load_preferences(path: Path = PREFERENCES_FILE) -> dict:
    if not path.exists():
        raise ConfigError(f"missing {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of settings")
    if not data.get("email"):
        raise ConfigError(f"{path}: 'email' is required (it identifies your profile)")
    return data


def load(config_dir: Path = CONFIG_DIR) -> UserConfig:
    """Read all three files into one object."""
    prefs = load_preferences(config_dir / PREFERENCES_FILE.name)
    return UserConfig(
        portals=load_portals(config_dir / PORTALS_FILE.name),
        skills=load_skills(config_dir / SKILLS_FILE.name),
        email=str(prefs["email"]),
        full_name=prefs.get("full_name"),
        roles=list(prefs.get("roles") or []),
        locations=list(prefs.get("locations") or []),
        exclude=list(prefs.get("exclude") or []),
        seniority=str(prefs.get("seniority", "unknown")).lower(),
        years_experience=prefs.get("years_experience"),
        max_seniority_gap=int(prefs.get("max_seniority_gap", 1)),
        scan_every_minutes=max(5, int(prefs.get("scan_every_minutes", 30))),
        remote_preference=str(prefs.get("remote_preference", "any")).lower(),
        include_unknown_location=bool(prefs.get("include_unknown_location", True)),
        match_threshold=float(prefs.get("match_threshold", 0.6)),
        desired_salary_min=prefs.get("desired_salary_min"),
        salary_currency=prefs.get("salary_currency"),
        requires_visa_sponsorship=bool(prefs.get("requires_visa_sponsorship", False)),
    )


def _as_enum(enum_class: type, value: str, field: str) -> Any:
    """Coerce a YAML string to its enum member, with a usable error.

    Required at this boundary: ``EnumString`` converts on the way *out* of the
    database, but a value assigned from a config file is read back by the
    matcher before any round-trip happens. Left as a plain string it reaches
    ``profile.seniority.rank`` and raises ``AttributeError`` mid-scan.
    """
    try:
        return enum_class(value)
    except ValueError:
        options = ", ".join(member.value for member in enum_class)
        raise ConfigError(
            f"{PREFERENCES_FILE}: {field}={value!r} is not valid.\n"
            f"  choose one of: {options}"
        ) from None


def profile_fields(config: UserConfig) -> dict:
    """Map the user's wording onto ``UserProfile`` column names."""
    from app.models.enums import RemotePreference, SeniorityLevel

    return {
        "preferred_roles": config.roles,
        "preferred_locations": config.locations,
        "preferred_skills": config.skills,
        "excluded_keywords": config.exclude,
        "industries": [],
        "seniority": _as_enum(SeniorityLevel, config.seniority, "seniority"),
        "remote_preference": _as_enum(
            RemotePreference, config.remote_preference, "remote_preference"
        ),
        "years_experience": config.years_experience,
        "max_seniority_gap": config.max_seniority_gap,
        "requires_visa_sponsorship": config.requires_visa_sponsorship,
        "desired_salary_min": config.desired_salary_min,
        "salary_currency": config.salary_currency,
        "match_threshold": config.match_threshold,
        "include_unknown_location": config.include_unknown_location,
    }
