"""Заглушки поставщиков A и B по контракту POST /issue.

Одно приложение обслуживает обоих (/a/... и /b/...), но запускается двумя
процессами на разных портах: "поставщик недоступен" должно означать настоящий
connection refused, а не флажок в коде. Пул кодов и журнал request_id у
каждого свои, поведение задаётся через POST /{s}/_control - сценарии
воспроизводятся детерминированно, а не "как повезёт".

  - повтор с тем же request_id возвращает тот же ответ: на этом держится
    безопасность ретраев;
  - timeout_after_issue: код выдан и записан, а ответ не доходит;
  - unsupported_skus: "я такое не продаю" (410). Отказ навсегда, в отличие от
    out_of_stock, где имеет смысл дождаться завоза;
  - duplicate_code, foreign_code, error_after_issue: недобросовестный
    поставщик, ответу верить нельзя;
  - GET /{s}/codes/{code}: кому на самом деле принадлежит код;
  - лимит запросов со скользящим окном, сверх лимита 429. Нарушения заглушка
    считает сама (GET /{s}/_stats).
"""
from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from suppliers.models import Base, SupplierKey, SupplierRequest

SUPPLIERS = ("a", "b")

engine = create_async_engine(settings.supplier_database_url, pool_size=20, max_overflow=20)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Behaviour(BaseModel):
    """Поведение заглушки. `mode` перекрывает вероятностную модель."""

    # random|ok|error_5xx|out_of_stock|timeout|timeout_after_issue|refuse
    # |duplicate_code|foreign_code|error_after_issue
    mode: str = "random"
    error_rate: float = 0.0
    timeout_rate: float = 0.0
    # Доля таймаутов, при которых код на самом деле выдан.
    timeout_after_issue_share: float = 0.5
    hang_seconds: float = 10.0
    latency_ms: int = 0
    # Статус-запрос тоже зависает: исход неразрешим в принципе.
    probe_hangs: bool = False
    # Лимит на POST /issue за окно. 0 - без лимита.
    rate_limit: int = 0
    rate_window_seconds: float = 60.0
    out_of_stock_skus: list[str] = Field(default_factory=list)
    # Товар, которого нет и не будет: отказ навсегда.
    unsupported_skus: list[str] = Field(default_factory=list)


BEHAVIOUR: dict[str, Behaviour] = {s: Behaviour() for s in SUPPLIERS}

# Времена запросов в текущем окне и счётчики нарушений.
ISSUE_LOG: dict[str, list[float]] = {s: [] for s in SUPPLIERS}
RATE_STATS: dict[str, dict[str, int]] = {
    s: {"accepted": 0, "rejected_429": 0, "over_limit": 0, "max_in_window": 0}
    for s in SUPPLIERS
}


def _register_request(supplier: str) -> tuple[bool, int]:
    """Учесть запрос в окне. Возвращает (принят, сколько в окне)."""
    b = BEHAVIOUR[supplier]
    now = time.monotonic()
    log = ISSUE_LOG[supplier]
    horizon = now - b.rate_window_seconds
    log[:] = [t for t in log if t > horizon]

    stats = RATE_STATS[supplier]
    if b.rate_limit and len(log) >= b.rate_limit:
        stats["rejected_429"] += 1
        return False, len(log)

    log.append(now)
    stats["accepted"] += 1
    stats["max_in_window"] = max(stats["max_in_window"], len(log))
    if b.rate_limit and len(log) > b.rate_limit:
        # Сюда попасть нельзя, но если попали - это нарушение лимита.
        stats["over_limit"] += 1
    return True, len(log)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Supplier stubs (A/B)", lifespan=lifespan)


class IssueRequest(BaseModel):
    request_id: str
    sku: str
    order_id: str


def _check(supplier: str) -> str:
    if supplier not in SUPPLIERS:
        raise HTTPException(status_code=404, detail="unknown supplier")
    return supplier


async def _mint_if_needed(session, supplier: str, sku: str) -> None:
    """Синтетические коды для SKU нагрузочного каталога."""
    if not sku.startswith("LOAD-"):
        return
    exists = await session.scalar(
        select(func.count())
        .select_from(SupplierKey)
        .where(SupplierKey.supplier == supplier, SupplierKey.sku == sku)
    )
    if exists:
        return
    await session.execute(
        pg_insert(SupplierKey)
        .values(
            [
                {
                    "supplier": supplier,
                    "sku": sku,
                    "code": f"GEN{supplier.upper()}-{sku}-{i:04d}",
                    "state": "available",
                }
                for i in range(20)
            ]
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )


async def _dishonest_code(session, supplier: str, sku: str, mode: str) -> str | None:
    """Код, который поставщик отдавать не имеет права.

    duplicate_code - уже выданный ключ того же SKU, foreign_code - ключ
    другого товара. Пул не трогаем: иначе подлог не обнаружить снаружи.
    """
    if mode == "duplicate_code":
        for where in (
            (SupplierKey.sku == sku, SupplierKey.state == "issued"),
            (SupplierKey.state == "issued",),   # своего SKU нет, сойдёт любой
        ):
            code = await session.scalar(
                select(SupplierKey.code)
                .where(SupplierKey.supplier == supplier, *where)
                .order_by(SupplierKey.id.desc())
                .limit(1)
            )
            if code is not None:
                return code
        return None
    # foreign_code: ключ другого товара, лучше не выданный - тогда
    # единственная аномалия в ответе это чужой SKU.
    for where in (
        (SupplierKey.sku != sku, SupplierKey.state == "available"),
        (SupplierKey.sku != sku,),
    ):
        code = await session.scalar(
            select(SupplierKey.code)
            .where(SupplierKey.supplier == supplier, *where)
            .order_by(SupplierKey.id)
            .limit(1)
        )
        if code is not None:
            return code
    return None


async def _claim_code(session, supplier: str, sku: str, request_id: str, order_id: str):
    """Атомарно выдать код. Идемпотентно по request_id."""
    # Уже отвечали на этот request_id - вернём тот же ответ.
    prev = await session.get(SupplierRequest, request_id)
    if prev is not None:
        return prev, True

    await _mint_if_needed(session, supplier, sku)

    # Снимаем свободный код с пула; SKIP LOCKED - чтобы параллельные запросы
    # не дрались за одну строку.
    row = (
        await session.execute(
            text(
                """
                UPDATE supplier_keys
                   SET state = 'issued', request_id = :rid, issued_at = now()
                 WHERE id = (
                       SELECT id FROM supplier_keys
                        WHERE supplier = :sup AND sku = :sku AND state = 'available'
                        ORDER BY id
                          FOR UPDATE SKIP LOCKED
                        LIMIT 1)
             RETURNING code
                """
            ),
            {"rid": request_id, "sup": supplier, "sku": sku},
        )
    ).first()

    if row is None:
        rec = SupplierRequest(
            request_id=request_id, supplier=supplier, order_id=order_id, sku=sku,
            outcome="error", reason="out_of_stock",
        )
    else:
        rec = SupplierRequest(
            request_id=request_id, supplier=supplier, order_id=order_id, sku=sku,
            outcome="ok", code=row[0],
        )
    session.add(rec)
    try:
        await session.flush()
    except Exception:
        # Гонка двух одинаковых request_id: побеждает первый, читаем его
        # ответ. Наш UPDATE пула откатился, код не потерян.
        await session.rollback()
        for _ in range(20):
            async with Session() as s2:
                prev = await s2.get(SupplierRequest, request_id)
            if prev is not None:
                return prev, True
            await asyncio.sleep(0.05)
        raise HTTPException(status_code=503, detail={"status": "error", "reason": "conflict"})
    return rec, False


def _pick_mode(b: Behaviour) -> str:
    if b.mode != "random":
        return b.mode
    r = random.random()
    if r < b.error_rate:
        return "error_5xx"
    if r < b.error_rate + b.timeout_rate:
        return (
            "timeout_after_issue"
            if random.random() < b.timeout_after_issue_share
            else "timeout"
        )
    return "ok"


@app.post("/{supplier}/issue")
async def issue(
    body: IssueRequest,
    response: Response,
    supplier: str = Path(...),
):
    supplier = _check(supplier)
    b = BEHAVIOUR[supplier]

    accepted, in_window = _register_request(supplier)
    if not accepted:
        response.headers["Retry-After"] = str(max(1, int(b.rate_window_seconds)))
        raise HTTPException(
            status_code=429,
            detail={"status": "error", "reason": "rate_limited", "in_window": in_window},
        )

    mode = _pick_mode(b)

    if b.latency_ms:
        await asyncio.sleep(b.latency_ms / 1000)

    # Отказ до выдачи: код точно не выдан.
    if mode == "refuse":
        raise HTTPException(status_code=503, detail={"status": "error", "reason": "unavailable"})
    if mode == "error_5xx":
        raise HTTPException(status_code=500, detail={"status": "error", "reason": "internal"})
    if mode == "timeout":
        # Зависание без выдачи: клиент упрётся в свой read timeout.
        await asyncio.sleep(b.hang_seconds)
        raise HTTPException(status_code=504, detail={"status": "error", "reason": "timeout"})

    if body.sku in b.unsupported_skus:
        raise HTTPException(
            status_code=410, detail={"status": "error", "reason": "sku_not_supported"}
        )

    if body.sku in b.out_of_stock_skus:
        raise HTTPException(status_code=409, detail={"status": "error", "reason": "out_of_stock"})

    if mode in ("duplicate_code", "foreign_code"):
        async with Session() as session:
            prev = await session.get(SupplierRequest, body.request_id)
            if prev is None:
                bad = await _dishonest_code(session, supplier, body.sku, mode)
                if bad is not None:
                    session.add(
                        SupplierRequest(
                            request_id=body.request_id, supplier=supplier,
                            order_id=body.order_id, sku=body.sku, outcome="ok", code=bad,
                        )
                    )
                    await session.commit()
                    prev = await session.get(SupplierRequest, body.request_id)
            if prev is not None and prev.outcome == "ok":
                response.headers["X-Replayed"] = "0"
                return {"status": "ok", "request_id": prev.request_id, "code": prev.code}
        # Подсунуть нечего, работаем честно.

    async with Session() as session:
        rec, replayed = await _claim_code(
            session, supplier, body.sku, body.request_id, body.order_id
        )
        await session.commit()
        outcome, code, reason = rec.outcome, rec.code, rec.reason

    if mode == "error_after_issue" and outcome == "ok":
        # Код выдан и записан, а клиент видит ошибку: по ответу кажется, что
        # выдачи не было.
        raise HTTPException(
            status_code=500, detail={"status": "error", "reason": "internal"}
        )

    # Ловушка: код записан, но ответ клиенту не доходит.
    if mode == "timeout_after_issue":
        await asyncio.sleep(b.hang_seconds)

    if outcome == "error":
        raise HTTPException(status_code=409, detail={"status": "error", "reason": reason})

    response.headers["X-Replayed"] = "1" if replayed else "0"
    return {"status": "ok", "request_id": rec.request_id, "code": code}


@app.get("/{supplier}/issue/{request_id}")
async def issue_status(supplier: str, request_id: str):
    """Выдавал ли поставщик код по этому request_id.

    Без такого lookup единственный безопасный способ - идемпотентный повтор.
    """
    _check(supplier)
    if BEHAVIOUR[supplier].probe_hangs:
        await asyncio.sleep(BEHAVIOUR[supplier].hang_seconds)
        raise HTTPException(status_code=504, detail={"status": "error", "reason": "timeout"})
    async with Session() as session:
        rec = await session.get(SupplierRequest, request_id)
    if rec is None:
        raise HTTPException(status_code=404, detail={"status": "not_found"})
    if rec.outcome == "ok":
        return {"status": "ok", "request_id": request_id, "code": rec.code}
    return {"status": "error", "request_id": request_id, "reason": rec.reason}


@app.get("/{supplier}/codes/{code}")
async def code_owner(supplier: str, code: str):
    """Кому на самом деле принадлежит код.

    Единственный внешний способ проверить ответ: пул ключей врать не умеет.
    """
    _check(supplier)
    async with Session() as session:
        key = (
            await session.execute(
                select(SupplierKey).where(
                    SupplierKey.supplier == supplier, SupplierKey.code == code
                )
            )
        ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail={"status": "not_found"})
    return {
        "code": key.code,
        "sku": key.sku,
        "state": key.state,
        "request_id": key.request_id,
    }


@app.get("/{supplier}/_stats")
async def rate_stats(supplier: str):
    """Счётчики лимита: нарушение видно отсюда, а не со стороны клиента."""
    _check(supplier)
    b = BEHAVIOUR[supplier]
    return {
        "supplier": supplier,
        "rate_limit": b.rate_limit,
        "rate_window_seconds": b.rate_window_seconds,
        **RATE_STATS[supplier],
    }


@app.post("/{supplier}/_stats/reset")
async def reset_rate_stats(supplier: str):
    _check(supplier)
    ISSUE_LOG[supplier].clear()
    RATE_STATS[supplier] = {
        "accepted": 0, "rejected_429": 0, "over_limit": 0, "max_in_window": 0
    }
    return RATE_STATS[supplier]


@app.get("/{supplier}/stock")
async def stock(supplier: str):
    _check(supplier)
    async with Session() as session:
        rows = (
            await session.execute(
                select(SupplierKey.sku, func.count())
                .where(SupplierKey.supplier == supplier, SupplierKey.state == "available")
                .group_by(SupplierKey.sku)
            )
        ).all()
    return {"supplier": supplier, "stock": {sku: n for sku, n in rows}}


@app.post("/{supplier}/_control")
async def control(supplier: str, patch: dict = Body(default_factory=dict)):
    """Детерминированное управление хаосом."""
    _check(supplier)
    current = BEHAVIOUR[supplier].model_dump()
    current.update(patch)
    BEHAVIOUR[supplier] = Behaviour(**current)
    return BEHAVIOUR[supplier].model_dump()


@app.get("/{supplier}/_control")
async def get_control(supplier: str):
    _check(supplier)
    return BEHAVIOUR[_check(supplier)].model_dump()


@app.post("/{supplier}/_restock")
async def restock(supplier: str, body: dict = Body(default_factory=dict)):
    """Пополнить остаток по SKU: восстановление из `out_of_stock`."""
    _check(supplier)
    sku = body["sku"]
    count = int(body.get("count", 5))
    prefix = body.get("prefix", "RSTK")
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    async with Session() as session:
        await session.execute(
            pg_insert(SupplierKey)
            .values(
                [
                    {
                        "supplier": supplier,
                        "sku": sku,
                        "code": f"{prefix}-{stamp}-{i:03d}",
                        "state": "available",
                    }
                    for i in range(count)
                ]
            )
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await session.commit()
    return {"supplier": supplier, "sku": sku, "added": count}


@app.get("/health")
async def health():
    return {"status": "ok"}
