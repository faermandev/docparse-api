from fastapi import FastAPI

from app.api.health import router as health_router


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="Docs Extraction API",
        version="0.1.0",
        description="Upload documents and get validated, structured JSON back.",
    )
    app.include_router(health_router)
    return app


app = create_app()
