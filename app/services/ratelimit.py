"""Лимит запросов к поставщику: token bucket на самом PostgreSQL.

В БД, а не в памяти: лимит принадлежит поставщику, и два воркера с локальными
счётчиками превысят его вдвое. Строка в таблице - общая точка сериализации,
снятие токена умещается в один атомарный UPDATE.

Ждём заранее, а не разбираем 429: ответ 429 приходит уже после превышения.
Bucket узкий (`supplier_rate_burst`, по умолчанию 1): окно у поставщика
скользящее, и полный bucket в его начале даёт всплеск, который в окно не
помещается. `supplier_rate_safety` - запас на расхождение часов.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import SupplierBudget

log = get_logger("ratelimit")

# Пополнение и снятие одним выражением: сколько набежало с прошлого раза,
# столько и доступно, но не больше ёмкости.
_AVAILABLE = (
    "LEAST(capacity, tokens + EXTRACT(EPOCH FROM (now() - updated_at)) * refill_per_sec)"
)

TAKE_SQL = text(
    f"""
    UPDATE supplier_budgets
       SET tokens = {_AVAILABLE} - 1,
           updated_at = now(),
           taken_total = taken_total + 1
     WHERE supplier = :supplier
       AND {_AVAILABLE} >= 1
 RETURNING tokens, refill_per_sec
    """
)

WAIT_SQL = text(
    f"""
    UPDATE supplier_budgets
       SET throttled_total = throttled_total + 1
     WHERE supplier = :supplier
 RETURNING GREATEST(0.0, (1 - {_AVAILABLE}) / refill_per_sec) AS wait_seconds
    """
)


def refill_per_sec() -> float:
    return max(
        0.01,
        settings.supplier_rate_limit_per_min / 60.0 * settings.supplier_rate_safety,
    )


async def ensure_budgets(suppliers: tuple[str, ...]) -> None:
    """Завести бюджеты и подтянуть под текущие настройки."""
    rate = refill_per_sec()
    capacity = max(1.0, settings.supplier_rate_burst)
    async with session_scope() as s:
        for supplier in suppliers:
            await s.execute(
                pg_insert(SupplierBudget)
                .values(
                    supplier=supplier,
                    tokens=capacity,
                    capacity=capacity,
                    refill_per_sec=rate,
                )
                .on_conflict_do_update(
                    index_elements=["supplier"],
                    set_={"capacity": capacity, "refill_per_sec": rate},
                )
            )
    log.info("ratelimit.configured", suppliers=list(suppliers), per_sec=rate, burst=capacity)


async def take(supplier: str) -> float | None:
    """Снять токен: None - можно идти к поставщику, число - сколько ждать."""
    async with session_scope() as s:
        row = (await s.execute(TAKE_SQL, {"supplier": supplier})).first()
        if row is not None:
            return None
        wait = (await s.execute(WAIT_SQL, {"supplier": supplier})).scalar_one_or_none()
    if wait is None:
        # Строки нет: лимит для этого поставщика не настроен.
        return None
    log.debug("ratelimit.throttled", supplier=supplier, wait_seconds=float(wait))
    return float(wait)


async def snapshot() -> list[dict]:
    """Состояние бюджетов для наблюдения за прогрессом."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT supplier, ROUND({_AVAILABLE}::numeric, 2) AS tokens_available, "
                    "capacity, ROUND((refill_per_sec * 60)::numeric, 1) AS rate_per_min, "
                    "taken_total, throttled_total FROM supplier_budgets ORDER BY supplier"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]
