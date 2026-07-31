"""FastAPI application.

Endpoints are declared ``def``, not ``async def``, so Starlette runs them in
its threadpool alongside the synchronous SQLAlchemy session (AD-2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import system
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import (
    ConfigurationError,
    DuplicateError,
    NotFoundError,
    PlatformError,
)
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    logger.info(
        "api.startup",
        environment=str(settings.environment),
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
    )
    yield
    logger.info("api.shutdown")


def create_app() -> FastAPI:
    """Application factory.

    A factory rather than a module-level singleton so tests can build an app
    against overridden settings without the import order deciding what
    configuration it gets.
    """
    settings = get_settings()

    app = FastAPI(
        title="Job Aggregation Platform",
        description=(
            "Self-learning job aggregation: registers career pages, scans them on "
            "a schedule, extracts postings regardless of how the site is built, "
            "matches them against a profile, and notifies on relevant new jobs."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    if not settings.is_production:
        # Wide-open CORS in development only; production should be fronted by
        # a proxy that sets its own policy.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(system.router)
    app.include_router(api_router, prefix=settings.api_prefix)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain errors onto HTTP status codes in one place.

    Without this every endpoint would need its own try/except to avoid
    returning a 500 for what is really a 404 or a 409.
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
        )

    @app.exception_handler(DuplicateError)
    async def _duplicate(_: Request, exc: DuplicateError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    @app.exception_handler(ConfigurationError)
    async def _misconfigured(_: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("api.configuration_error", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "server is misconfigured"},
        )

    @app.exception_handler(PlatformError)
    async def _platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        logger.error("api.platform_error", path=request.url.path, error=str(exc))
        # 502 rather than 500: these are failures of an upstream career site or
        # model provider, not of this service.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)}
        )


app = create_app()
