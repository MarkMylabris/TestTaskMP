"""Витрина каталога - тот самый "горячий" запрос из этапа 5.

Задача: список товаров с остатками не должен деградировать на десятках тысяч
SKU. Что для этого сделано.

Остаток вынесен в отдельную узкую таблицу sku_stock. Товар меняется редко,
остаток - часто; в одной широкой таблице каждая выдача переписывала бы всю
строку и все её индексы.

Индексы покрывающие и частичные: всё, что рисуется в карточке, лежит в INCLUDE,
поэтому скан идёт Index Only с нулевыми Heap Fetches. Отдельно стоит отметить
форму предиката: WHERE is_active, а не WHERE is_active IS TRUE. Доказыватель
предикатов PostgreSQL сравнивает формы буквально, и во втором варианте частичный
индекс молча перестаёт применяться. На 50k SKU это 6.5 мс вместо 0.3.

Пагинация keyset, а не OFFSET: OFFSET на глубокой странице читает и выбрасывает
десятки тысяч строк, keyset всегда ровно limit. На замерах 0.36 мс против 8.3.

Джойн к остаткам - JOIN LATERAL с LIMIT 1 внутри. LIMIT нужен не за тем, чтобы
ограничить выборку (там и так одна строка по PK), а чтобы PostgreSQL не
развернул подзапрос и не ушёл в merge join, который на недооценке селективности
прочитывает всю таблицу остатков. С LATERAL план фиксирован: индексный проход по
товарам плюс ровно limit точечных обращений по PK.

План можно посмотреть прямо на проде: GET /catalog/storefront/explain.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import ProductOut, StorefrontPage

router = APIRouter(prefix="/catalog", tags=["catalog"])

STOREFRONT_SQL = """
SELECT p.sku, p.name, p.type, p.price_minor, p.currency, p.image,
       COALESCE(s.available, 0) AS available
  FROM products p
  LEFT JOIN LATERAL (
       SELECT st.available FROM sku_stock st WHERE st.sku = p.sku LIMIT 1
  ) s ON true
 WHERE p.is_active
   {type_filter}
   {cursor_filter}
   {stock_filter}
 ORDER BY {order_by}
 LIMIT :limit
"""


def _build_sql(*, product_type: str | None, cursor: str | None, in_stock: bool) -> str:
    return STOREFRONT_SQL.format(
        type_filter="AND p.type = :ptype" if product_type else "",
        cursor_filter="AND p.sku > :cursor" if cursor else "",
        stock_filter="AND s.available > 0" if in_stock else "",
        # При фиксированном type сортировки `type, sku` и `sku` эквивалентны,
        # но первая точно совпадает с порядком ключа ix_products_storefront_by_type,
        # и планировщик гарантированно берёт покрывающий индекс вместо PK
        # (PK пришлось бы досортировывать или фильтровать лишние строки).
        order_by="p.type, p.sku" if product_type else "p.sku",
    )


@router.get("/storefront", response_model=StorefrontPage)
async def storefront(
    session: AsyncSession = Depends(get_session),
    type: str | None = Query(default=None, description="topup|key|subscription|giftcard"),
    in_stock: bool = Query(default=False),
    cursor: str | None = Query(default=None, description="последний sku предыдущей страницы"),
    limit: int = Query(default=50, ge=1, le=200),
):
    sql = _build_sql(product_type=type, cursor=cursor, in_stock=in_stock)
    params = {"limit": limit}
    if type:
        params["ptype"] = type
    if cursor:
        params["cursor"] = cursor

    started = time.perf_counter()
    rows = (await session.execute(text(sql), params)).mappings().all()
    took = (time.perf_counter() - started) * 1000

    items = [
        ProductOut(
            sku=r["sku"], name=r["name"], type=r["type"],
            price=r["price_minor"] / 100, currency=r["currency"], image=r["image"],
            available=r["available"], in_stock=r["available"] > 0,
        )
        for r in rows
    ]
    return StorefrontPage(
        items=items,
        next_cursor=items[-1].sku if len(items) == limit else None,
        took_ms=round(took, 2),
    )


@router.get("/storefront/explain")
async def storefront_explain(
    session: AsyncSession = Depends(get_session),
    type: str | None = Query(default=None),
    in_stock: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """EXPLAIN (ANALYZE, BUFFERS) того же самого запроса витрины."""
    sql = _build_sql(product_type=type, cursor=cursor, in_stock=in_stock)
    params = {"limit": limit}
    if type:
        params["ptype"] = type
    if cursor:
        params["cursor"] = cursor
    plan = (
        await session.execute(
            text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params
        )
    ).scalar_one()
    return {"sql": sql.strip(), "plan": plan}


@router.get("/products/{sku}", response_model=ProductOut)
async def product(sku: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(
            text(
                "SELECT p.sku, p.name, p.type, p.price_minor, p.currency, p.image, "
                "COALESCE(s.available,0) AS available "
                "FROM products p LEFT JOIN sku_stock s ON s.sku=p.sku "
                "WHERE p.sku=:sku AND p.is_active"
            ),
            {"sku": sku},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown sku")
    return ProductOut(
        sku=row["sku"], name=row["name"], type=row["type"],
        price=row["price_minor"] / 100, currency=row["currency"], image=row["image"],
        available=row["available"], in_stock=row["available"] > 0,
    )
