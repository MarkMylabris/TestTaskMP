"""Инициализация схем и наполнение каталога/пулов ключей.

    python -m scripts.seed              # каталог из задания + 50 ключей
    python -m scripts.seed --reset      # снести и создать заново
    python -m scripts.seed --extra 100  # + синтетические ключи на каждый SKU
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import create_schema, drop_schema, session_scope, vacuum_analyze
from app.models import Product, SkuStock
from suppliers.models import Base as SupplierBase
from suppliers.models import SupplierKey

DATA = Path(__file__).resolve().parent.parent / "data"
SUPPLIERS = ("a", "b")

sup_engine = create_async_engine(settings.supplier_database_url)
SupSession = async_sessionmaker(sup_engine, expire_on_commit=False)


async def seed_core(reset: bool) -> int:
    if reset:
        await drop_schema()
    await create_schema()

    catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
    async with session_scope() as s:
        for p in catalog["products"]:
            await s.execute(
                pg_insert(Product)
                .values(
                    sku=p["sku"], name=p["name"], type=p["type"],
                    price_minor=int(p["price"]) * 100, currency=p["currency"],
                    image=p.get("image"), is_active=True,
                )
                .on_conflict_do_update(
                    index_elements=["sku"],
                    set_={"name": p["name"], "price_minor": int(p["price"]) * 100},
                )
            )
            await s.execute(
                pg_insert(SkuStock)
                .values(sku=p["sku"], available=0, reserved=0)
                .on_conflict_do_nothing(index_elements=["sku"])
            )
    return len(catalog["products"])


async def seed_suppliers(reset: bool, extra: int) -> dict[str, int]:
    async with sup_engine.begin() as conn:
        if reset:
            await conn.run_sync(SupplierBase.metadata.drop_all)
        await conn.run_sync(SupplierBase.metadata.create_all)

    catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
    keys = json.loads((DATA / "keys.json").read_text(encoding="utf-8"))["keys"]
    skus = [p["sku"] for p in catalog["products"]]

    rows = []
    # Пул из задания раскладываем по кругу: SKU - по кругу, поставщик - чётность.
    for i, code in enumerate(keys):
        rows.append(
            {
                "supplier": SUPPLIERS[i % 2],
                "sku": skus[i % len(skus)],
                "code": code,
                "state": "available",
            }
        )
    # Синтетический запас - только для нагрузочных прогонов.
    for sku in skus:
        for supplier in SUPPLIERS:
            for n in range(extra):
                rows.append(
                    {
                        "supplier": supplier,
                        "sku": sku,
                        "code": f"EXT{supplier.upper()}-{sku}-{n:05d}",
                        "state": "available",
                    }
                )

    async with SupSession() as s:
        for chunk in (rows[i : i + 500] for i in range(0, len(rows), 500)):
            await s.execute(
                pg_insert(SupplierKey).values(chunk).on_conflict_do_nothing(
                    index_elements=["code"]
                )
            )
        await s.commit()
        counts = dict(
            (
                await s.execute(
                    text(
                        "SELECT supplier, count(*) FROM supplier_keys "
                        "WHERE state='available' GROUP BY supplier"
                    )
                )
            ).all()
        )
    return counts


async def sync_stock_snapshot() -> int:
    """Перелить остатки поставщиков в витринный снимок (обычно это делает воркер)."""
    async with SupSession() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT sku, count(*) FROM supplier_keys WHERE state='available' "
                    "GROUP BY sku"
                )
            )
        ).all()
    async with session_scope() as s:
        for sku, n in rows:
            await s.execute(
                pg_insert(SkuStock)
                .values(sku=sku, available=int(n))
                .on_conflict_do_update(index_elements=["sku"], set_={"available": int(n)})
            )
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="удалить и пересоздать схемы")
    ap.add_argument("--extra", type=int, default=0, help="синтетических ключей на SKU/поставщика")
    args = ap.parse_args()

    products = await seed_core(args.reset)
    counts = await seed_suppliers(args.reset, args.extra)
    skus = await sync_stock_snapshot()
    await vacuum_analyze("products", "sku_stock")
    print(f"products: {products}")
    print(f"supplier keys available: {counts}")
    print(f"stock snapshot for {skus} sku")


if __name__ == "__main__":
    asyncio.run(main())
