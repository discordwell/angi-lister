import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routers import auth, health, webhook, api, console

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("angi-lister")


@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Angi-Lister starting up")
    yield
    log.info("Angi-Lister shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Netic - Angi Lead Receiver",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Health routes (no auth)
    app.include_router(health.router, tags=["health"])

    # Auth routes (magic link login)
    app.include_router(auth.router, tags=["auth"])

    # Webhook endpoint (Angi lead ingestion)
    app.include_router(webhook.router, tags=["webhook"])

    # JSON API endpoints
    app.include_router(api.router, tags=["api"])

    # Console UI (HTML, HTTP Basic auth)
    app.include_router(console.router, tags=["console"])

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/console")

    return app
