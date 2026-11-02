from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Docs Extraction API",
        version="0.1.0",
        description="Upload documents and get validated, structured JSON back.",
    )
    register_error_handlers(app)
    # Last added runs first: the request id is assigned before the size check responds.
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(jobs_router)
    return app


app = create_app()
