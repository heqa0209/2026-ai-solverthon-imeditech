from __future__ import annotations

from fastapi import FastAPI

from app import announcements, auth, company, health
from app.errors import install_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="AI Solverthon API", version="0.1.0")
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(company.router)
    app.include_router(announcements.router)
    return app


app = create_app()
