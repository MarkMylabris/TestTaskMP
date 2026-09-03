"""Очередь фоновых задач на самом PostgreSQL.

Отдельный брокер тут не нужен и даже вреден: задача ставится в той же
транзакции, что и смена статуса заказа (транзакционный outbox), а с внешней
очередью пришлось бы решать, что делать, если транзакция закоммитилась, а
публикация в брокер упала.

Разбор - через FOR UPDATE SKIP LOCKED, так что воркеров можно поднять сколько
угодно, и одну задачу никогда не возьмут двое.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

# Предикат частичного уникального индекса - нужен PostgreSQL для inference в ON CONFLICT.
_ACTIVE = text("state IN ('pending','running')")

KIND_DELIVER = "deliver_order"
KIND_APPLY_ORPHAN = "apply_orphan_events"
KIND_SYNC_STOCK = "sync_stock"
KIND_RESOLVE_UNKNOWN = "resolve_unknown_attempt"


def backoff_delay(attempts: int, base: float = 0.5, cap: float = 60.0) -> timedelta:
    return timedelta(seconds=min(cap, base * (2 ** max(0, attempts - 1))))


async def enqueue(
    session: AsyncSession,
    kind: str,
    dedupe_key: str,
    payload: dict | None = None,
    *,
    delay: timedelta | None = None,
    max_attempts: int = 25,
) -> bool:
    """Поставить задачу. Возвращает False, если такая уже висит в очереди.

    Дедупликация - частичный уникальный индекс по (kind, dedupe_key)
    среди state IN ('pending','running').
    """
    run_at = datetime.now(timezone.utc) + (delay or timedelta())
    stmt = (
        pg_insert(Job)
        .values(
            kind=kind,
            dedupe_key=dedupe_key,
            payload=payload or {},
            run_at=run_at,
            max_attempts=max_attempts,
            state="pending",
        )
        .on_conflict_do_nothing(index_elements=["kind", "dedupe_key"], index_where=_ACTIVE)
        .returning(Job.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


CLAIM_SQL = text(
    """
    UPDATE jobs
       SET state = 'running', attempts = attempts + 1, updated_at = now()
     WHERE id IN (
           SELECT id FROM jobs
            WHERE state = 'pending' AND run_at <= now()
            ORDER BY run_at
              FOR UPDATE SKIP LOCKED
            LIMIT :batch)
 RETURNING id, kind, dedupe_key, payload, attempts, max_attempts
    """
)


async def claim(session: AsyncSession, batch: int) -> list[dict]:
    rows = (await session.execute(CLAIM_SQL, {"batch": batch})).mappings().all()
    return [dict(r) for r in rows]


async def finish(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        text("UPDATE jobs SET state='done', updated_at=now(), last_error=NULL WHERE id=:id"),
        {"id": job_id},
    )


async def retry_later(
    session: AsyncSession, job_id: int, attempts: int, max_attempts: int, error: str
) -> None:
    """Вернуть задачу в очередь с экспоненциальным бэкоффом либо признать провал."""
    if attempts >= max_attempts:
        await session.execute(
            text("UPDATE jobs SET state='failed', last_error=:e, updated_at=now() WHERE id=:id"),
            {"id": job_id, "e": error[:2000]},
        )
        return
    run_at = datetime.now(timezone.utc) + backoff_delay(attempts)
    await session.execute(
        text(
            "UPDATE jobs SET state='pending', run_at=:r, last_error=:e, updated_at=now() "
            "WHERE id=:id"
        ),
        {"id": job_id, "r": run_at, "e": error[:2000]},
    )


async def reschedule(session: AsyncSession, job_id: int, delay: timedelta, note: str) -> None:
    """Мягкий перенос (не ошибка): например, ждём пополнения остатка."""
    run_at = datetime.now(timezone.utc) + delay
    await session.execute(
        text(
            "UPDATE jobs SET state='pending', run_at=:r, last_error=:e, updated_at=now() "
            "WHERE id=:id"
        ),
        {"id": job_id, "r": run_at, "e": note[:2000]},
    )
