from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

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


# Счётчик остатка обновляется часто и узкий -> оставляем место под HOT-апдейты,
# чтобы UPDATE не переписывал индексные записи.
_STORAGE_TUNING = ("ALTER TABLE sku_stock SET (fillfactor = 80)",)


async def create_schema() -> None:
    """Идемпотентное создание схемы.

    `create_all` не добавляет индексы к уже существующим таблицам, поэтому
    индексы досоздаются явно. В проде здесь была бы миграция (Alembic);
    для тестового задания достаточно идемпотентного DDL.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                await conn.execute(CreateIndex(index, if_not_exists=True))
        for stmt in _STORAGE_TUNING:
            await conn.execute(text(stmt))


async def vacuum_analyze(*tables: str) -> None:
    """VACUUM ANALYZE вне транзакции.

    Без свежей visibility map Index Only Scan всё равно ходит в кучу
    (Heap Fetches > 0), и покрывающий индекс не даёт выигрыша. После массовой
    загрузки каталога это обязательный шаг.
    """
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in tables:
            await conn.execute(text(f"VACUUM (ANALYZE) {table}"))


async def drop_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
