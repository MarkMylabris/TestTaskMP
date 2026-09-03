from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import admin, catalog, orders, webhooks
from app.config import settings
from app.db import create_schema, engine
from app.logging_conf import configure_logging, get_logger
from app.worker import sweeper_loop, worker_loop

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await create_schema()

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    if settings.worker_enabled:
        tasks = [
            asyncio.create_task(worker_loop(stop)),
            asyncio.create_task(sweeper_loop(stop)),
        ]
        log.info("worker.embedded_started")

    log.info("api.started")
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(t, timeout=5)
        await engine.dispose()
        log.info("api.stopped")


app = FastAPI(
    title="Digital goods store - core",
    version="1.0.0",
    description="Ядро магазина цифровых товаров: заказы, платежи, автовыдача.",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Сквозной request_id в каждой строке лога запроса."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(http_request_id=rid, path=request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        log.exception("http.error", method=request.method)
        raise
    response.headers["x-request-id"] = rid
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("http.unhandled", path=request.url.path, error=repr(exc))
    return JSONResponse(status_code=500, content={"detail": "internal error"})


app.include_router(orders.router)
app.include_router(webhooks.router)
app.include_router(catalog.router)
app.include_router(admin.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}
