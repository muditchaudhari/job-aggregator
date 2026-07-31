"""v1 router assembly.

``system`` is mounted separately in ``main.py`` because ``/health`` and
``/metrics`` must live at the root, not under ``/api/v1`` — orchestrators and
Prometheus scrapers expect them there and should not have to know about API
versioning.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import companies, jobs, notifications, profile, scans

api_router = APIRouter()
api_router.include_router(companies.router)
api_router.include_router(jobs.router)
api_router.include_router(profile.router)
api_router.include_router(notifications.router)
api_router.include_router(scans.router)
