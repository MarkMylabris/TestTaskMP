"""Этап 5: витрина каталога под нагрузкой.

Проверяется не "быстро ли на моей машине" - такой тест был бы бесполезным и
мигал бы на CI. Проверяется, что план выполнения остался тем, на который мы
рассчитывали: Index Only Scan по покрывающему индексу, нулевые Heap Fetches,
ни Sort, ни Seq Scan.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import ENV, ROOT

LOAD_SKUS = 20000


def _nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []) or []:
        yield from _nodes(child)


@pytest.fixture(scope="module")
def big_catalog(stack):
    subprocess.run(
        [sys.executable, "-m", "scripts.load_catalog", "--count", str(LOAD_SKUS), "--runs", "1"],
        cwd=ROOT, env=ENV, check=True, stdout=subprocess.DEVNULL,
    )
    return LOAD_SKUS


async def test_storefront_pagination_is_keyset(api, big_catalog):
    seen: set[str] = set()
    cursor = None
    for _ in range(5):
        params = {"limit": 100, "type": "key", "in_stock": True}
        if cursor:
            params["cursor"] = cursor
        r = await api.get("/catalog/storefront", params=params)
        assert r.status_code == 200
        page = r.json()
        skus = [i["sku"] for i in page["items"]]
        assert skus == sorted(skus), "страница отсортирована по sku"
        assert not (seen & set(skus)), "страницы не пересекаются"
        seen |= set(skus)
        assert all(i["available"] > 0 for i in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) >= 400


async def test_storefront_plan_stays_bounded(api, big_catalog):
    """Инварианты плана горячего запроса витрины:

      * нет Seq Scan и нет Sort - порядок берётся прямо из индекса;
      * товары читаются покрывающим индексом без походов в кучу;
      * прочитано O(limit) строк, а не O(размера каталога);
      * остатки берутся точечно, Index Only Scan, Heap Fetches = 0.
    """
    r = await api.get(
        "/catalog/storefront/explain",
        params={"limit": 50, "type": "key", "in_stock": True, "cursor": "LOAD-0005000"},
    )
    assert r.status_code == 200
    plan = r.json()["plan"][0]["Plan"]
    nodes = list(_nodes(plan))
    pretty = json.dumps(plan, ensure_ascii=False)[:2000]

    node_types = {n["Node Type"] for n in nodes}
    assert "Seq Scan" not in node_types, pretty
    assert "Sort" not in node_types, f"сортировка означает потерю порядка индекса: {pretty}"
    assert "Nested Loop" in node_types, f"джойн к остаткам обязан быть точечным: {pretty}"

    scan = next(n for n in nodes if n.get("Relation Name") == "products")
    assert scan["Node Type"] == "Index Only Scan", pretty
    assert scan["Index Name"] == "ix_products_storefront_by_type", pretty
    assert scan["Heap Fetches"] == 0, f"покрывающий индекс не должен ходить в кучу: {pretty}"
    assert scan["Actual Rows"] < 500, f"прочитано O(limit), а не O(каталога): {pretty}"

    stock = next(n for n in nodes if n.get("Relation Name") == "sku_stock")
    assert stock["Node Type"] == "Index Only Scan", pretty
    assert stock["Heap Fetches"] == 0, pretty
    assert stock["Actual Loops"] < 500, pretty


async def test_covering_index_is_usable_by_the_planner(api, big_catalog):
    """Покрывающий индекс существует и реально применим к запросу витрины."""
    import asyncpg

    from tests.conftest import CORE_DB, PG_DSN

    conn = await asyncpg.connect(f"{PG_DSN}/{CORE_DB}")
    try:
        defs = await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename='products' AND indexname LIKE 'ix_products_storefront%'"
        )
        by_name = {r["indexname"]: r["indexdef"] for r in defs}
        assert "ix_products_storefront_by_type" in by_name
        # Предикат обязан быть записан как `WHERE is_active`: форма
        # `WHERE is_active IS TRUE` не сопоставляется с условием запроса,
        # и частичный индекс молча перестаёт использоваться.
        assert by_name["ix_products_storefront_by_type"].endswith("WHERE is_active")
        assert "INCLUDE (name, price_minor, currency, image)" in (
            by_name["ix_products_storefront_by_type"]
        )

        await conn.execute("SET enable_seqscan = off")
        plan = await conn.fetchval(
            """
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT p.sku, p.name, p.price_minor, p.currency, p.image
              FROM products p
             WHERE p.is_active AND p.type = 'key' AND p.sku > 'LOAD-0005000'
             ORDER BY p.type, p.sku LIMIT 50
            """
        )
        plan = json.loads(plan)[0]["Plan"]
        scan = next(n for n in _nodes(plan) if n.get("Relation Name") == "products")
        assert scan["Node Type"] == "Index Only Scan", json.dumps(plan, ensure_ascii=False)
        assert scan["Index Name"] == "ix_products_storefront_by_type"
        assert scan["Heap Fetches"] == 0
    finally:
        await conn.close()


async def test_storefront_latency_is_bounded_by_limit(api, big_catalog):
    """Время ответа не растёт при переходе на "глубокие" страницы."""
    first = await api.get("/catalog/storefront", params={"limit": 50, "type": "key"})
    deep = await api.get(
        "/catalog/storefront",
        params={"limit": 50, "type": "key", "cursor": "LOAD-0018000"},
    )
    assert first.status_code == deep.status_code == 200
    # Порядок величины один и тот же: глубокая страница не "дороже" первой.
    assert deep.json()["took_ms"] < max(20.0, first.json()["took_ms"] * 5)


async def test_product_endpoint(api, big_catalog):
    r = await api.get("/catalog/products/KEY-CS2-PRIME")
    assert r.status_code == 200
    body = r.json()
    assert body["price"] == 1290.0 and body["currency"] == "RUB"
    assert (await api.get("/catalog/products/NOPE")).status_code == 404
