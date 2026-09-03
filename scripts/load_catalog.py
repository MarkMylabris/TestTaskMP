"""Генератор "большого" каталога и замер горячего запроса витрины (этап 5).

    python -m scripts.load_catalog --count 50000
    python -m scripts.load_catalog --explain            # только план и замеры
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

from sqlalchemy import text

from app.db import create_schema, session_scope, vacuum_analyze

TYPES = ("topup", "key", "subscription", "giftcard")


async def generate(count: int, batch: int = 5000) -> None:
    await create_schema()
    rnd = random.Random(42)
    async with session_scope() as s:
        for start in range(0, count, batch):
            rows = []
            for i in range(start, min(start + batch, count)):
                # Тип НЕ зашит в sku: иначе порядок по PK случайно совпал бы
                # с группировкой по типу и замер получился бы нечестным.
                t = rnd.choice(TYPES)
                rows.append(
                    {
                        "sku": f"LOAD-{i:07d}-{rnd.randrange(1000, 9999)}",
                        "name": f"Нагрузочный товар #{i}",
                        "type": t,
                        "price_minor": rnd.randrange(9900, 999900, 100),
                        "currency": "RUB",
                        "image": f"assets/{t}.png",
                        # 15% распродано - чтобы частичный индекс имел смысл
                        "available": 0 if rnd.random() < 0.15 else rnd.randint(1, 200),
                    }
                )
            await s.execute(
                text(
                    """
                    INSERT INTO products (sku, name, type, price_minor, currency, image, is_active)
                    VALUES (:sku, :name, :type, :price_minor, :currency, :image, true)
                    ON CONFLICT (sku) DO NOTHING
                    """
                ),
                rows,
            )
            await s.execute(
                text(
                    """
                    INSERT INTO sku_stock (sku, available, reserved)
                    VALUES (:sku, :available, 0)
                    ON CONFLICT (sku) DO UPDATE SET available = EXCLUDED.available
                    """
                ),
                rows,
            )
            print(f"  inserted {min(start + batch, count)}/{count}", flush=True)

    # VACUUM обновляет visibility map - без него Index Only Scan
    # всё равно лезет в кучу и покрывающий индекс не работает.
    await vacuum_analyze("products", "sku_stock")


HOT_QUERY = """
SELECT p.sku, p.name, p.type, p.price_minor, p.currency, p.image,
       COALESCE(s.available, 0) AS available
  FROM products p
  LEFT JOIN LATERAL (
       SELECT st.available FROM sku_stock st WHERE st.sku = p.sku LIMIT 1
  ) s ON true
 WHERE p.is_active
   AND p.type = :ptype
   AND s.available > 0
   AND p.sku > :cursor
 ORDER BY p.type, p.sku
 LIMIT :limit
"""


async def measure(runs: int = 200, limit: int = 50) -> None:
    async with session_scope() as s:
        total = (await s.execute(text("SELECT count(*) FROM products"))).scalar_one()
        sizes = (
            await s.execute(
                text(
                    "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size "
                    "FROM pg_catalog.pg_statio_user_tables "
                    "WHERE relname IN ('products','sku_stock') ORDER BY relname"
                )
            )
        ).mappings().all()
        print(f"\nSKU в каталоге: {total}")
        for row in sizes:
            print(f"  {row['relname']}: {row['size']}")

        params = {"ptype": "key", "cursor": "", "limit": limit}
        plan = (
            await s.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {HOT_QUERY}"), params
            )
        ).scalar_one()
        print("\n--- EXPLAIN (ANALYZE, BUFFERS) горячего запроса витрины ---")
        print(json.dumps(plan, ensure_ascii=False, indent=2)[:4000])

        # Замер: имитируем листание витрины по страницам (keyset).
        rnd = random.Random(7)
        latencies = []
        for _ in range(runs):
            cursor = f"LOAD-{rnd.randrange(0, 999999):07d}"
            t0 = time.perf_counter()
            await s.execute(
                text(HOT_QUERY), {"ptype": "key", "cursor": cursor, "limit": limit}
            )
            latencies.append((time.perf_counter() - t0) * 1000)
        latencies.sort()
        print(
            f"\nГорячий запрос ({runs} прогонов, LIMIT {limit}): "
            f"p50={latencies[len(latencies)//2]:.2f}ms  "
            f"p95={latencies[int(len(latencies)*0.95)]:.2f}ms  "
            f"max={latencies[-1]:.2f}ms"
        )

        # Для контраста - то же самое через OFFSET.
        offset_sql = HOT_QUERY.replace("AND p.sku > :cursor", "") + " OFFSET :off"
        latencies = []
        for _ in range(min(runs, 50)):
            t0 = time.perf_counter()
            await s.execute(
                text(offset_sql),
                {"ptype": "key", "limit": limit, "off": rnd.randrange(0, max(1, total // 5))},
            )
            latencies.append((time.perf_counter() - t0) * 1000)
        latencies.sort()
        print(
            f"То же с OFFSET (антипаттерн):            "
            f"p50={latencies[len(latencies)//2]:.2f}ms  "
            f"p95={latencies[int(len(latencies)*0.95)]:.2f}ms  "
            f"max={latencies[-1]:.2f}ms"
        )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50000)
    ap.add_argument("--explain", action="store_true", help="не генерировать, только замерить")
    ap.add_argument("--runs", type=int, default=200)
    args = ap.parse_args()
    if not args.explain:
        print(f"Генерирую {args.count} SKU...")
        await generate(args.count)
    await measure(runs=args.runs)


if __name__ == "__main__":
    asyncio.run(main())
