"""Заглушки поставщиков A и B по контракту POST /issue.

Одно приложение обслуживает обоих (/a/... и /b/...), но запускается двумя
процессами на разных портах - чтобы "поставщик недоступен" означало настоящий
connection refused, а не флажок в коде. Пул кодов и журнал request_id у каждого
свои.

Что здесь важно для задания:

  - повтор с тем же request_id возвращает тот же код (или ту же ошибку) -
    именно на этом держится безопасность ретраев;
  - есть режим timeout_after_issue: код выдан и зафиксирован в базе, а ответ
    "не доходит". Это и есть ловушка таймаута;
  - доли ошибок и таймаутов настраиваются через POST /{s}/_control, так что
    сценарии воспроизводятся детерминированно, а не "как повезёт".
"""
from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from suppliers.models import Base, SupplierKey, SupplierRequest

SUPPLIERS = ("a", "b")

engine = create_async_engine(settings.supplier_database_url, pool_size=20, max_overflow=20)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Behaviour(BaseModel):
    """Поведение заглушки. `mode` перекрывает вероятностную модель."""

    mode: str = "random"  # random|ok|error_5xx|out_of_stock|timeout|timeout_after_issue|refuse
    error_rate: float = 0.0
    timeout_rate: float = 0.0
    # Доля "таймаутов", при которых код на самом деле был выдан.
    timeout_after_issue_share: float = 0.5
    hang_seconds: float = 10.0
    latency_ms: int = 0
    # Статус-запрос тоже зависает: исход становится принципиально неразрешимым.
    probe_hangs: bool = False
    out_of_stock_skus: list[str] = Field(default_factory=list)


BEHAVIOUR: dict[str, Behaviour] = {s: Behaviour() for s in SUPPLIERS}


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
    """Синтетические коды для сгенерированных SKU нагрузочного каталога."""
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


async def _claim_code(session, supplier: str, sku: str, request_id: str, order_id: str):
    """Атомарно выдать код под request_id. Идемпотентно по request_id."""
    # 1. Уже отвечали на этот request_id? Вернём тот же ответ.
    prev = await session.get(SupplierRequest, request_id)
    if prev is not None:
        return prev, True

    await _mint_if_needed(session, supplier, sku)

    # 2. Снимаем один свободный код с пула. SKIP LOCKED - чтобы параллельные
    #    запросы не дрались за одну и ту же строку.
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
        # Гонка двух одинаковых request_id: побеждает первый, читаем его ответ.
        # Наш UPDATE пула откатывается вместе с транзакцией, код не теряется.
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
    mode = _pick_mode(b)

    if b.latency_ms:
        await asyncio.sleep(b.latency_ms / 1000)

    # Отказ ДО выдачи: код точно не выдан.
    if mode == "refuse":
        raise HTTPException(status_code=503, detail={"status": "error", "reason": "unavailable"})
    if mode == "error_5xx":
        raise HTTPException(status_code=500, detail={"status": "error", "reason": "internal"})
    if mode == "timeout":
        # "Зависание" без выдачи: клиент должен упереться в свой read timeout.
        await asyncio.sleep(b.hang_seconds)
        raise HTTPException(status_code=504, detail={"status": "error", "reason": "timeout"})

    if body.sku in b.out_of_stock_skus:
        raise HTTPException(status_code=409, detail={"status": "error", "reason": "out_of_stock"})

    async with Session() as session:
        rec, replayed = await _claim_code(
            session, supplier, body.sku, body.request_id, body.order_id
        )
        await session.commit()
        outcome, code, reason = rec.outcome, rec.code, rec.reason

    # Ловушка: код зафиксирован в БД поставщика, но ответ клиенту "не доходит".
    if mode == "timeout_after_issue":
        await asyncio.sleep(b.hang_seconds)

    if outcome == "error":
        raise HTTPException(status_code=409, detail={"status": "error", "reason": reason})

    response.headers["X-Replayed"] = "1" if replayed else "0"
    return {"status": "ok", "request_id": rec.request_id, "code": code}


@app.get("/{supplier}/issue/{request_id}")
async def issue_status(supplier: str, request_id: str):
    """Разрешение неизвестного исхода: "а ты вообще выдавал код по этому request_id?"

    Реальные поставщики обычно дают такой lookup; без него единственный
    безопасный способ - идемпотентный повтор того же request_id.
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
    """Детерминированное управление хаосом (для тестов)."""
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
    """Пополнить остаток по SKU - сценарий восстановления из `out_of_stock`."""
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
