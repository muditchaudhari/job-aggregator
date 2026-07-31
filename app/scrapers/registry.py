"""Adapter registry.

One place that maps an :class:`ATSType` to the class that handles it, so adding
a platform is a single import plus a single tuple entry — no ``if/elif`` chain
in the scan service, and no risk of the detector and the dispatcher disagreeing
about which adapter serves which platform.
"""

from __future__ import annotations

from app.models.enums import ATSType
from app.scrapers.adapters.apple import AppleScraper
from app.scrapers.adapters.ashby import AshbyScraper
from app.scrapers.adapters.custom_react import CustomReactScraper
from app.scrapers.adapters.eightfold import EightfoldScraper
from app.scrapers.adapters.generic_html import GenericHtmlScraper
from app.scrapers.adapters.greenhouse import GreenhouseScraper
from app.scrapers.adapters.lever import LeverScraper
from app.scrapers.adapters.phenom import PhenomScraper
from app.scrapers.adapters.smartrecruiters import SmartRecruitersScraper
from app.scrapers.adapters.successfactors import SuccessFactorsScraper
from app.scrapers.adapters.taleo import TaleoScraper
from app.scrapers.adapters.uber import UberScraper
from app.scrapers.adapters.workday import WorkdayScraper
from app.scrapers.base import BaseScraper

#: Ordered most specific to least. Detection walks this list, so a page that
#: embeds Greenhouse inside a React app resolves to Greenhouse — the more
#: specific and far cheaper answer — rather than to the generic renderer.
ADAPTERS: tuple[type[BaseScraper], ...] = (
    GreenhouseScraper,
    LeverScraper,
    AppleScraper,
    AshbyScraper,
    EightfoldScraper,
    UberScraper,
    PhenomScraper,
    WorkdayScraper,
    SmartRecruitersScraper,
    SuccessFactorsScraper,
    TaleoScraper,
    CustomReactScraper,
    GenericHtmlScraper,
)

_BY_TYPE: dict[ATSType, type[BaseScraper]] = {
    adapter.ats_type: adapter for adapter in ADAPTERS
}


def get_adapter(ats_type: ATSType) -> type[BaseScraper]:
    """Adapter for a platform, defaulting to the generic HTML reader.

    Falls back rather than raising: an unknown or newly added enum value should
    degrade to a scrape that probably works, not take the company offline.
    """
    return _BY_TYPE.get(ats_type, GenericHtmlScraper)


def api_backed_types() -> frozenset[ATSType]:
    return frozenset(a.ats_type for a in ADAPTERS if a.supports_api)
