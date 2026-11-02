from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="Docs Extraction API",
        version="0.1.0",
        description="Upload documents and get validated, structured JSON back.",
    )
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(jobs_router)
    return app


app = create_app()
