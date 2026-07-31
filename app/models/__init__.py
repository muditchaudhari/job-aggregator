"""ORM models.

Every model must be imported here. Alembic's autogenerate walks
``Base.metadata``, and a model that no module has imported is invisible to it —
the classic cause of a migration that silently drops a table.
"""

from app.database.base import Base
from app.models.company import Company
from app.models.job import Job
from app.models.match import JobMatch
from app.models.notification import Notification
from app.models.scrape_run import ScrapeRun
from app.models.selector import Selector
from app.models.user import User, UserProfile

__all__ = [
    "Base",
    "Company",
    "Job",
    "JobMatch",
    "Notification",
    "ScrapeRun",
    "Selector",
    "User",
    "UserProfile",
]
