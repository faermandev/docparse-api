from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware

WEB_DIR = Path(__file__).parent / "web"


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
    app.include_router(webhooks_router)

    # Web UI: static files on the same origin as the API, so no CORS is needed.
    app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    return app


app = create_app()
