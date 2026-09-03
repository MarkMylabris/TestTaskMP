"""Поднимает настоящий стек: два поставщика и API, каждый своим процессом,
на отдельных портах и отдельных базах.

Тесты ходят по живому HTTP, а не через ASGI-транспорт. Это дольше, но иначе
нечего проверять: ни гонка на SELECT FOR UPDATE, ни read timeout к поставщику
на моках не воспроизводятся, а весь смысл задания именно в них.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

API_PORT = int(os.getenv("TEST_API_PORT", "8123"))
SUP_A_PORT = int(os.getenv("TEST_SUP_A_PORT", "9123"))
SUP_B_PORT = int(os.getenv("TEST_SUP_B_PORT", "9124"))

API = f"http://127.0.0.1:{API_PORT}"
SUP_A = f"http://127.0.0.1:{SUP_A_PORT}"
SUP_B = f"http://127.0.0.1:{SUP_B_PORT}"

PG_DSN = os.getenv("TEST_PG_DSN", "postgresql://gamestore:gamestore@127.0.0.1:5432")
CORE_DB = os.getenv("TEST_CORE_DB", "gamestore_core_test")
SUP_DB = os.getenv("TEST_SUP_DB", "gamestore_suppliers_test")

ENV = {
    **os.environ,
    "DATABASE_URL": f"postgresql+asyncpg://gamestore:gamestore@127.0.0.1:5432/{CORE_DB}",
    "SUPPLIER_DATABASE_URL":
        f"postgresql+asyncpg://gamestore:gamestore@127.0.0.1:5432/{SUP_DB}",
    "SUPPLIER_A_URL": SUP_A,
    "SUPPLIER_B_URL": SUP_B,
    # Быстрые таймауты, чтобы сценарии этапа 3 укладывались в секунды.
    "SUPPLIER_READ_TIMEOUT": "1.0",
    "SUPPLIER_CONNECT_TIMEOUT": "0.5",
    "SUPPLIER_MAX_ATTEMPTS": "2",
    "SUPPLIER_BACKOFF_BASE": "0.05",
    "SUPPLIER_BACKOFF_MAX": "0.2",
    "WORKER_ENABLED": "1",
    "WORKER_POLL_INTERVAL": "0.05",
    "SWEEPER_INTERVAL": "1.0",
    "STUCK_ORDER_SECONDS": "3",
    "DELIVERY_MAX_ATTEMPTS": "20",
    "LOG_LEVEL": "WARNING",
}


async def _ensure_databases() -> None:
    conn = await asyncpg.connect(f"{PG_DSN}/postgres")
    try:
        for db in (CORE_DB, SUP_DB):
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", db)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db}"')
    finally:
        await conn.close()


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"service did not start: {url}")


def _spawn(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


@pytest.fixture(scope="session")
def stack():
    asyncio.run(_ensure_databases())
    subprocess.run(
        [PY, "-m", "scripts.seed", "--reset"], cwd=ROOT, env=ENV, check=True,
        stdout=subprocess.DEVNULL,
    )

    procs = {
        "sup_a": _spawn("suppliers.main:app", SUP_A_PORT),
        "sup_b": _spawn("suppliers.main:app", SUP_B_PORT),
        "api": _spawn("app.main:app", API_PORT),
    }
    try:
        _wait_http(f"{SUP_A}/health")
        _wait_http(f"{SUP_B}/health")
        _wait_http(f"{API}/health")
        yield procs
    finally:
        for p in procs.values():
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)


@pytest.fixture
async def api(stack):
    async with httpx.AsyncClient(base_url=API, timeout=30.0) as client:
        yield client


@pytest.fixture(autouse=True)
async def reset_suppliers(stack):
    """Перед каждым тестом поставщики в детерминированном режиме "всё ок"."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        for base, s in ((SUP_A, "a"), (SUP_B, "b")):
            await c.post(f"{base}/{s}/_control", json={
                "mode": "ok", "error_rate": 0.0, "timeout_rate": 0.0,
                "hang_seconds": 3.0, "latency_ms": 0, "out_of_stock_skus": [],
                "probe_hangs": False,
            })
    yield


# ------------------------------------------------------------------ helpers #
async def control(supplier: str, patch: dict) -> dict:
    base = SUP_A if supplier == "a" else SUP_B
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{base}/{supplier}/_control", json=patch)
        r.raise_for_status()
        return r.json()


async def restock(supplier: str, sku: str, count: int = 5) -> None:
    base = SUP_A if supplier == "a" else SUP_B
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{base}/{supplier}/_restock", json={"sku": sku, "count": count})
        r.raise_for_status()


async def supplier_request(supplier: str, request_id: str) -> httpx.Response:
    base = SUP_A if supplier == "a" else SUP_B
    async with httpx.AsyncClient(timeout=10.0) as c:
        return await c.get(f"{base}/{supplier}/issue/{request_id}")


async def create_order(api: httpx.AsyncClient, sku: str, order_id: str | None = None) -> dict:
    body = {"sku": sku}
    if order_id:
        body["order_id"] = order_id
    r = await api.post("/orders", json=body)
    r.raise_for_status()
    return r.json()


async def wait_status(
    api: httpx.AsyncClient, order_id: str, statuses: set[str], timeout: float = 30.0
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = await api.get(f"/orders/{order_id}")
        r.raise_for_status()
        last = r.json()
        if last["status"] in statuses:
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(
        f"order {order_id} stuck in {last['status'] if last else '?'}, expected one of {statuses}"
    )


async def timeline(api: httpx.AsyncClient, order_id: str) -> dict:
    r = await api.get(f"/admin/orders/{order_id}/timeline")
    r.raise_for_status()
    return r.json()
