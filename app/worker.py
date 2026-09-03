"""Фоновый воркер: разбор очереди плюс доводчик зависших заказов.

Живёт либо внутри процесса API (WORKER_ENABLED=1), либо отдельно:

    python -m app.worker

Реплик можно поднять сколько угодно: очередь разбирается через FOR UPDATE
SKIP LOCKED, задачи между процессами не задваиваются.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta

import httpx
from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.logging_conf import configure_logging, get_logger
from app.services import jobs, payments
from app.services.delivery import deliver_order
from app.services.supplier_client import SUPPLIER_URLS, SupplierClient

log = get_logger("worker")


# --------------------------------------------------------------------------- #
# Обработчики задач
# --------------------------------------------------------------------------- #
async def handle_deliver(job: dict, client: SupplierClient) -> tuple[str, timedelta | None, str]:
    order_id = job["payload"]["order_id"]
    result = await deliver_order(order_id, client)

    if result.status in ("delivered", "skipped"):
        return "done", None, result.note or result.status

    if job["attempts"] >= settings.delivery_max_attempts:
        # Дальше - ручной разбор; заказ остаётся в восстановимом статусе,
        # его видно в /admin/reconciliation.
        log.error(
            "delivery.giving_up", order_id=order_id, attempts=job["attempts"],
            status=result.status, note=result.note,
        )
        return "failed", None, f"{result.status}: {result.note}"

    delay = result.retry_after or jobs.backoff_delay(job["attempts"])
    return "retry", delay, f"{result.status}: {result.note}"


async def handle_orphans(job: dict, client: SupplierClient) -> tuple[str, timedelta | None, str]:
    order_id = job["payload"]["order_id"]
    async with session_scope() as s:
        exists = (
            await s.execute(text("SELECT 1 FROM orders WHERE id=:id"), {"id": order_id})
        ).scalar_one_or_none()
        if not exists:
            return "retry", jobs.backoff_delay(job["attempts"]), "order still absent"
        applied = await payments.apply_orphan_events(s, order_id)
    log.info("payment.orphans_applied", order_id=order_id, applied=applied)
    return "done", None, f"applied={applied}"


async def handle_sync_stock(job: dict, client: SupplierClient) -> tuple[str, timedelta | None, str]:
    """Обновить снимок остатков витрины из поставщиков (этап 5)."""
    totals: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=5.0) as http:
        for supplier, base in SUPPLIER_URLS.items():
            try:
                r = await http.get(f"{base}/{supplier}/stock")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("stock.sync_failed", supplier=supplier, error=repr(exc))
                continue
            for sku, n in r.json()["stock"].items():
                totals[sku] = totals.get(sku, 0) + int(n)

    if not totals:
        return "retry", timedelta(seconds=10), "no supplier reachable"

    async with session_scope() as s:
        await s.execute(
            text(
                """
                INSERT INTO sku_stock (sku, available, reserved, updated_at)
                SELECT k.sku, k.available, 0, now()
                  FROM (SELECT unnest(CAST(:skus AS text[])) AS sku,
                               unnest(CAST(:counts AS int[])) AS available) k
                  JOIN products p ON p.sku = k.sku
                    ON CONFLICT (sku) DO UPDATE
                       SET available = EXCLUDED.available, updated_at = now()
                """
            ),
            {"skus": list(totals.keys()), "counts": list(totals.values())},
        )
    log.info("stock.synced", skus=len(totals))
    return "done", None, f"skus={len(totals)}"


HANDLERS = {
    jobs.KIND_DELIVER: handle_deliver,
    jobs.KIND_APPLY_ORPHAN: handle_orphans,
    jobs.KIND_SYNC_STOCK: handle_sync_stock,
}


# --------------------------------------------------------------------------- #
# Цикл воркера
# --------------------------------------------------------------------------- #
async def _run_job(job: dict, client: SupplierClient, sem: asyncio.Semaphore) -> None:
    async with sem:
        handler = HANDLERS.get(job["kind"])
        try:
            if handler is None:
                outcome, delay, note = "failed", None, f"unknown job kind {job['kind']}"
            else:
                outcome, delay, note = await handler(job, client)
        except Exception as exc:  # noqa: BLE001 - задача не должна ронять воркер
            log.exception("job.error", job_id=job["id"], kind=job["kind"], error=repr(exc))
            outcome, delay, note = "retry", jobs.backoff_delay(job["attempts"]), repr(exc)

        async with session_scope() as s:
            if outcome == "done":
                await jobs.finish(s, job["id"])
            elif outcome == "retry":
                await jobs.reschedule(s, job["id"], delay or timedelta(seconds=1), note)
            else:
                await jobs.retry_later(
                    s, job["id"], job["max_attempts"], job["max_attempts"], note
                )
        log.debug("job.done", job_id=job["id"], kind=job["kind"], outcome=outcome, note=note)


async def worker_loop(stop: asyncio.Event) -> None:
    sem = asyncio.Semaphore(settings.worker_concurrency)
    async with SupplierClient() as client:
        running: set[asyncio.Task] = set()
        while not stop.is_set():
            async with session_scope() as s:
                batch = await jobs.claim(s, settings.worker_batch)
            if not batch:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.worker_poll_interval)
                continue
            for job in batch:
                task = asyncio.create_task(_run_job(job, client, sem))
                running.add(task)
                task.add_done_callback(running.discard)
        if running:
            await asyncio.gather(*running, return_exceptions=True)


SWEEP_STUCK = text(
    """
    SELECT id FROM orders
     WHERE status NOT IN ('delivered','payment_failed')
       AND paid_at IS NOT NULL
       AND updated_at < now() - make_interval(secs => :stuck)
     ORDER BY updated_at
     LIMIT 200
    """
)

SWEEP_ORPHANS = text(
    """
    SELECT DISTINCT e.order_id
      FROM payment_events e
      JOIN orders o ON o.id = e.order_id
     WHERE e.processing_state = 'orphan'
     LIMIT 200
    """
)


async def sweeper_loop(stop: asyncio.Event) -> None:
    """Безопасно доводит "зависшие" заказы до финального статуса.

    Безопасно - потому что просто ставит ту же самую задачу выдачи, а она
    идемпотентна: `jobs` не даст задвоить задачу, `issuances.order_id UNIQUE`
    не даст задвоить выдачу, а незакрытые попытки к поставщику сначала
    разрешаются статус-запросом.
    """
    ticks = 0
    while not stop.is_set():
        try:
            async with session_scope() as s:
                stuck = (await s.execute(SWEEP_STUCK, {"stuck": settings.stuck_order_seconds})).scalars().all()
                for order_id in stuck:
                    if await jobs.enqueue(
                        s, jobs.KIND_DELIVER, dedupe_key=order_id,
                        payload={"order_id": order_id},
                    ):
                        log.warning("sweeper.requeued_stuck_order", order_id=order_id)

                for order_id in (await s.execute(SWEEP_ORPHANS)).scalars().all():
                    await jobs.enqueue(
                        s, jobs.KIND_APPLY_ORPHAN, dedupe_key=order_id,
                        payload={"order_id": order_id},
                    )

                if ticks % 6 == 0:
                    await jobs.enqueue(s, jobs.KIND_SYNC_STOCK, dedupe_key="all")
        except Exception as exc:  # noqa: BLE001
            log.exception("sweeper.error", error=repr(exc))
        ticks += 1
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.sweeper_interval)


async def run_forever() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    log.info("worker.started", concurrency=settings.worker_concurrency)
    await asyncio.gather(worker_loop(stop), sweeper_loop(stop))
    log.info("worker.stopped")


if __name__ == "__main__":
    configure_logging()
    asyncio.run(run_forever())
