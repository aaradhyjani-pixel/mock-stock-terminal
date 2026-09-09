"""Application entry point.

Serves three things from one process: the JSON API, the WebSocket tape, and the
static front ends. One process is the whole architecture (see
``docs/architecture.md``); the engine runs as an asyncio task inside it, which
is what guarantees a single authoritative clock and a single answer to "what is
the price".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_rules, get_settings
from .db import create_all, dispose_engine, healthcheck
from .engine.market import get_engine
from .routers import admin, auth, public, stream, trading
from .ws import hub

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("exchange")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await create_all()
    log.info("database ready at %s", settings.database_url.split("@")[-1])

    if settings.using_default_secret:
        # Anyone who has read the source can forge a session token for any team
        # or any operator. This must never be true on the day.
        log.warning("=" * 72)
        log.warning("EXCHANGE_SECRET_KEY is still the built-in default.")
        log.warning("Anyone who has seen this repository can forge a login.")
        log.warning("Set it before the event:")
        log.warning('  python -c "import secrets; print(secrets.token_urlsafe(48))"')
        log.warning("=" * 72)

    heartbeat_task: asyncio.Task | None = None
    if settings.run_engine:
        await get_engine().start()
        heartbeat_task = asyncio.create_task(stream.heartbeat(), name="ws-heartbeat")
    else:
        log.info("engine disabled (EXCHANGE_RUN_ENGINE=false); ticks must be driven manually")

    yield

    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
    await get_engine().stop()
    await hub.close_all()
    await dispose_engine()
    log.info("shutdown complete")


app = FastAPI(
    title="Mock Exchange Terminal",
    description="Simulated NSE trading terminal for a college mock-stock competition.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

settings = get_settings()
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(public.router)
app.include_router(trading.router)
app.include_router(admin.router)
app.include_router(stream.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Never leak a stack trace to a participant's phone.

    The detail goes to the log with a reference the help desk can search for;
    the participant gets a sentence they can act on.
    """
    reference = f"{id(exc):x}"[-6:]
    log.exception("unhandled error on %s %s (ref %s)", request.method, request.url.path, reference)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Something went wrong on our side. Nothing was charged to your account. "
                f"If it keeps happening, tell the help desk reference {reference}."
            )
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    engine = get_engine()
    database_ok = await healthcheck()
    return JSONResponse(
        status_code=200 if database_ok else 503,
        content={
            "ok": database_ok,
            "database": database_ok,
            "engine": engine.health,
            "competition": get_rules().competition_name,
        },
    )


# ------------------------------------------------------------------ frontends

@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Make the browser check before reusing a cached asset.

    If a stylesheet or script is fixed between trading days, every phone in the
    hall must pick it up on the next reload. ``no-cache`` still allows a 304, so
    this costs a conditional request rather than a re-download, and it removes
    an entire category of "it works on my laptop" from event day.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


def _page(name: str):
    async def handler():
        path = WEB_DIR / name
        if not path.exists():
            return JSONResponse(status_code=404, content={"detail": f"{name} is not built."})
        return FileResponse(path)

    return handler


app.add_api_route("/", _page("index.html"), include_in_schema=False)
app.add_api_route("/login", _page("login.html"), include_in_schema=False)
app.add_api_route("/console", _page("console.html"), include_in_schema=False)
app.add_api_route("/console/login", _page("console-login.html"), include_in_schema=False)
app.add_api_route("/projector", _page("projector.html"), include_in_schema=False)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # One worker, always. Two workers means two engines, two clocks and two
        # different prices, which is the one thing this design must not allow.
        workers=1,
    )


if __name__ == "__main__":
    run()
