from __future__ import annotations

from fastapi import FastAPI

from app.schemas import HealthResponse


def create_app() -> FastAPI:
    app = FastAPI(title="AI Solverthon API", version="0.1.0")

    @app.get("/api/v1/health/live", response_model=HealthResponse, tags=["health"])
    async def liveness() -> HealthResponse:
        return HealthResponse()

    return app


app = create_app()
