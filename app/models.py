"""Схема данных ядра.

Принцип простой: всё, что нельзя нарушить, выражено ограничением в БД, а не
проверкой в коде. Проверку в коде можно случайно снести рефакторингом или
обойти гонкой, ограничение - нет.

    payment_events.event_id  PK          вебхук обрабатывается один раз
    issuances.order_id       UNIQUE      у заказа не бывает двух выдач
    issuances.code           UNIQUE      один ключ не уйдёт в два заказа
    supplier_attempts.request_id UNIQUE  попытка фиксируется один раз
    jobs (kind, dedupe_key)  UNIQUE      одинаковых активных задач не бывает
                             partial
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- #
# Каталог (этап 1 + этап 5)
# --------------------------------------------------------------------------- #
class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)  # копейки
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    image: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint("price_minor > 0", name="ck_products_price_positive"),
        # Витрина (этап 5): покрывающие индексы под keyset-пагинацию.
        # Всё, что рисуется в карточке товара, лежит в INCLUDE, поэтому
        # выборка идёт Index Only Scan без обращения к куче.
        Index(
            "ix_products_storefront",
            "sku",
            postgresql_include=["name", "type", "price_minor", "currency", "image"],
            # Именно `is_active`, а не `is_active IS TRUE`: доказыватель
            # предикатов PostgreSQL сопоставляет частичный индекс с условием
            # запроса только при совпадающей форме, иначе индекс не применится.
            postgresql_where=is_active,
        ),
        Index(
            "ix_products_storefront_by_type",
            "type",
            "sku",
            postgresql_include=["name", "price_minor", "currency", "image"],
            postgresql_where=is_active,
        ),
    )


class SkuStock(Base):
    """Денормализованный снимок остатка по SKU для "горячей" витрины.

    Источник истины по остатку - поставщик; сюда его кладёт фоновая
    синхронизация. Витрина читает только эту таблицу (см. этап 5).
    """

    __tablename__ = "sku_stock"

    sku: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.sku", ondelete="CASCADE"), primary_key=True
    )
    available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("available >= 0", name="ck_sku_stock_available_non_negative"),
        # Index Only Scan при джойне витрины: available лежит в самом индексе.
        Index("ix_sku_stock_covering", "sku", postgresql_include=["available"]),
        # Отдельного индекса по available нет намеренно: витрина ходит сюда
        # только точечно по sku, а лишний индекс - это лишняя запись на каждой
        # выдаче. fillfactor задаётся ALTER TABLE в app.db.create_schema.
    )


# --------------------------------------------------------------------------- #
# Заказы
# --------------------------------------------------------------------------- #
ORDER_STATUSES = (
    "created",
    "paid",
    "delivering",
    "delivered",
    "payment_failed",
    "out_of_stock",
    "delivery_failed",
)
TERMINAL_STATUSES = frozenset({"delivered", "payment_failed"})
RECOVERABLE_STATUSES = frozenset({"out_of_stock", "delivery_failed"})


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sku: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.sku", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    customer_email: Mapped[str | None] = mapped_column(Text)

    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN " + str(ORDER_STATUSES), name="ck_orders_status"
        ),
        CheckConstraint("amount_minor > 0", name="ck_orders_amount_positive"),
        # Сверка "оплачен, но не выдан" и добивание зависших заказов.
        Index(
            "ix_orders_unfinished",
            "status",
            "updated_at",
            postgresql_where=(status.notin_(("delivered", "payment_failed"))),
        ),
        Index("ix_orders_sku_created", "sku", "created_at"),
    )


# --------------------------------------------------------------------------- #
# Платежи
# --------------------------------------------------------------------------- #
EVENT_STATES = (
    "applied",     # событие изменило состояние заказа
    "ignored",     # событие корректно, но не применимо (вне порядка / повтор статуса)
    "orphan",      # заказа ещё/уже нет - ждём и повторяем в фоне
    "rejected",    # событие противоречит заказу (сумма/валюта)
)


class PaymentEvent(Base):
    """Журнал вебхуков. PK по event_id - это и есть дедупликация at-least-once."""

    __tablename__ = "payment_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    event_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = _now()
    processing_state: Mapped[str] = mapped_column(String(32), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('paid','failed')", name="ck_payment_events_status"),
        CheckConstraint(
            "processing_state IN " + str(EVENT_STATES), name="ck_payment_events_state"
        ),
        Index(
            "ix_payment_events_orphan",
            "received_at",
            postgresql_where=(processing_state == "orphan"),
        ),
    )


# --------------------------------------------------------------------------- #
# Выдача
# --------------------------------------------------------------------------- #
class Issuance(Base):
    """Факт выдачи. Одна строка = один выданный клиенту код."""

    __tablename__ = "issuances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    supplier: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        # Ровно одна выдача на заказ - гарантия этапа 2.
        UniqueConstraint("order_id", name="uq_issuances_order"),
        # Один ключ не может уйти в два заказа - гарантия из условия.
        UniqueConstraint("code", name="uq_issuances_code"),
    )


ATTEMPT_STATES = (
    "in_flight",   # запрос отправлен, ответа ещё нет
    "ok",          # поставщик вернул код
    "failed",      # поставщик ТОЧНО не выдал код (connect refused / явная ошибка)
    "unknown",     # таймаут чтения: код мог быть выдан - трогать нельзя
)


class SupplierAttempt(Base):
    """Журнал намерений и исходов обращений к поставщику.

    Строка пишется ДО HTTP-запроса. Если процесс упадёт посреди вызова,
    останется `in_flight`, и восстановление будет знать, что исход неизвестен.
    """

    __tablename__ = "supplier_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="in_flight")
    http_status: Mapped[int | None] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("request_id", "attempt_no", name="uq_supplier_attempt"),
        CheckConstraint("state IN " + str(ATTEMPT_STATES), name="ck_supplier_attempt_state"),
        Index("ix_supplier_attempts_request", "request_id"),
        Index(
            "ix_supplier_attempts_unresolved",
            "started_at",
            postgresql_where=(state.in_(("in_flight", "unknown"))),
        ),
    )


# --------------------------------------------------------------------------- #
# Деньги (этап 4)
# --------------------------------------------------------------------------- #
LEDGER_ACCOUNTS = (
    "customer",           # обязательства перед клиентом
    "revenue",            # выручка
    "supplier_cost",      # себестоимость закупки кода
    "inventory",          # склад кодов
    "refund",             # возвраты
)


class LedgerEntry(Base):
    """Двойная запись: сумма amount_minor внутри одного txn_id всегда равна нулю."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    txn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    account: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)  # +debit / -credit
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_ledger_nonzero"),
        CheckConstraint("account IN " + str(LEDGER_ACCOUNTS), name="ck_ledger_account"),
        # Проводка по одному поводу и заказу не задваивается.
        UniqueConstraint("order_id", "kind", "account", name="uq_ledger_once_per_order_kind"),
        Index("ix_ledger_txn", "txn_id"),
    )


# --------------------------------------------------------------------------- #
# Очередь фоновых задач
# --------------------------------------------------------------------------- #
class Job(Base):
    """Транзакционный outbox + очередь.

    Задача ставится в той же транзакции, что и смена статуса заказа, поэтому
    "оплатили, но задачу потеряли" невозможно. Воркер разбирает очередь через
    SELECT ... FOR UPDATE SKIP LOCKED.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','running','done','failed')", name="ck_jobs_state"
        ),
        Index(
            "uq_jobs_active",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where=(state.in_(("pending", "running"))),
        ),
        Index(
            "ix_jobs_ready",
            "run_at",
            postgresql_where=(state == "pending"),
        ),
    )
