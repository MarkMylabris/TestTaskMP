from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import ITEM_STATUSES, LEDGER_ACCOUNTS, ORDER_STATUSES, Base

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция "всё или ничего"."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


# Остаток обновляется часто: оставляем место под HOT-апдейты, чтобы UPDATE
# не переписывал индексные записи.
_STORAGE_TUNING = ("ALTER TABLE sku_stock SET (fillfactor = 80)",)


# Переход к многотоварному заказу. `create_all` создаёт только новые таблицы,
# колонки и ограничения существующих не трогает - их доводит этот блок.
# Идемпотентен: на свежей базе не делает ничего.
_MIGRATE_MULTI_ITEM = f"""
DO $$
BEGIN
    ALTER TABLE issuances         ADD COLUMN IF NOT EXISTS order_item_id varchar(48);
    ALTER TABLE supplier_attempts ADD COLUMN IF NOT EXISTS order_item_id varchar(48);
    ALTER TABLE ledger_entries    ADD COLUMN IF NOT EXISTS item_id varchar(48)
                                  NOT NULL DEFAULT '';

    -- Однотоварный заказ старой схемы становится позицией #1.
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'orders' AND column_name = 'sku') THEN
        INSERT INTO order_items (id, order_id, position, sku, amount_minor, currency,
                                 status, delivery_attempts, last_error,
                                 created_at, updated_at, delivered_at)
        SELECT o.id || '.1', o.id, 1, o.sku, o.amount_minor, o.currency,
               CASE WHEN o.status = 'delivered' THEN 'delivered'
                    WHEN o.status IN ('delivering', 'out_of_stock', 'delivery_failed')
                         THEN o.status
                    ELSE 'pending' END,
               o.delivery_attempts, o.last_error, o.created_at, o.updated_at, o.delivered_at
          FROM orders o
        ON CONFLICT (id) DO NOTHING;

        ALTER TABLE orders DROP COLUMN sku;
    END IF;

    UPDATE issuances         SET order_item_id = order_id || '.1' WHERE order_item_id IS NULL;
    UPDATE supplier_attempts SET order_item_id = order_id || '.1' WHERE order_item_id IS NULL;

    ALTER TABLE issuances         ALTER COLUMN order_item_id SET NOT NULL;
    ALTER TABLE supplier_attempts ALTER COLUMN order_item_id SET NOT NULL;

    -- "Одна выдача на заказ" превратилась в "одна выдача на позицию".
    ALTER TABLE issuances DROP CONSTRAINT IF EXISTS uq_issuances_order;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_issuances_order_item') THEN
        ALTER TABLE issuances
            ADD CONSTRAINT uq_issuances_order_item UNIQUE (order_item_id);
    END IF;

    -- Идемпотентность проводки теперь учитывает позицию.
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_ledger_once_per_order_kind'
                  AND pg_get_constraintdef(oid) NOT LIKE '%item_id%') THEN
        ALTER TABLE ledger_entries DROP CONSTRAINT uq_ledger_once_per_order_kind;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_ledger_once_per_order_kind') THEN
        ALTER TABLE ledger_entries ADD CONSTRAINT uq_ledger_once_per_order_kind
            UNIQUE (order_id, item_id, kind, account);
    END IF;

    ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100;

    -- Статус позиции `unavailable` (выдать нельзя в принципе).
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_order_items_status'
                  AND pg_get_constraintdef(oid) NOT LIKE '%unavailable%') THEN
        ALTER TABLE order_items DROP CONSTRAINT ck_order_items_status;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_order_items_status') THEN
        ALTER TABLE order_items ADD CONSTRAINT ck_order_items_status
            CHECK (status IN {ITEM_STATUSES!s});
    END IF;

    -- Счёт `delivered`: выдача гасит обязательство перед клиентом.
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_ledger_account'
                  AND pg_get_constraintdef(oid) NOT LIKE '%delivered%') THEN
        ALTER TABLE ledger_entries DROP CONSTRAINT ck_ledger_account;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_ledger_account') THEN
        ALTER TABLE ledger_entries ADD CONSTRAINT ck_ledger_account
            CHECK (account IN {LEDGER_ACCOUNTS!s});
    END IF;

    -- Старые выдачи обязательство не гасили: досчитываем проводку, иначе
    -- инвариант "оплачено = выдано + возвращено" на них не сойдётся.
    INSERT INTO ledger_entries (txn_id, order_id, item_id, account, amount_minor,
                                currency, kind, created_at)
    SELECT gen_random_uuid(), it.order_id, it.id, a.account,
           a.sign * it.amount_minor, it.currency, 'delivery_settled',
           COALESCE(it.delivered_at, now())
      FROM order_items it
      CROSS JOIN (VALUES ('delivered', 1), ('customer', -1)) AS a(account, sign)
     WHERE it.status = 'delivered'
       AND EXISTS (SELECT 1 FROM ledger_entries l
                    WHERE l.order_id = it.order_id AND l.kind = 'payment_captured')
       AND NOT EXISTS (SELECT 1 FROM ledger_entries l
                        WHERE l.order_id = it.order_id AND l.item_id = it.id
                          AND l.kind = 'delivery_settled');

    -- Новые статусы заказа (partially_delivered / refunded).
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_orders_status'
                  AND pg_get_constraintdef(oid) NOT LIKE '%partially_delivered%') THEN
        ALTER TABLE orders DROP CONSTRAINT ck_orders_status;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_orders_status') THEN
        ALTER TABLE orders ADD CONSTRAINT ck_orders_status
            CHECK (status IN {ORDER_STATUSES!s});
    END IF;
END $$;
"""


# История только дополняется. Не соглашение в коде, а запрет в БД.
# Драйвер выполняет по одному оператору за раз, поэтому DDL разбит.
_APPEND_ONLY = (
    """
    CREATE OR REPLACE FUNCTION forbid_rewrite() RETURNS trigger AS $fn$
    BEGIN
        RAISE EXCEPTION 'append-only table %: % is not allowed',
            TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
    END;
    $fn$ LANGUAGE plpgsql
    """,
    """
    DO $$
    DECLARE
        t text;
    BEGIN
        FOREACH t IN ARRAY ARRAY['order_events', 'ledger_entries'] LOOP
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_append_only_' || t, t);
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION forbid_rewrite()',
                'trg_append_only_' || t, t);
        END LOOP;
    END $$
    """,
)


async def create_schema() -> None:
    """Идемпотентное создание схемы.

    `create_all` не добавляет индексы к существующим таблицам, поэтому они
    досоздаются явно. В проде здесь была бы миграция (Alembic).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Миграция строго до индексов: часть индексов лежит на колонках,
        # которых в старой базе ещё нет.
        await conn.execute(text(_MIGRATE_MULTI_ITEM))
        for stmt in _APPEND_ONLY:
            await conn.execute(text(stmt))
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                await conn.execute(CreateIndex(index, if_not_exists=True))
        for stmt in _STORAGE_TUNING:
            await conn.execute(text(stmt))


async def vacuum_analyze(*tables: str) -> None:
    """VACUUM ANALYZE вне транзакции.

    Без свежей visibility map Index Only Scan ходит в кучу, и покрывающий
    индекс не даёт выигрыша. После массовой загрузки каталога обязателен.
    """
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in tables:
            await conn.execute(text(f"VACUUM (ANALYZE) {table}"))


async def drop_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
