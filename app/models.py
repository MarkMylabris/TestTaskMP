"""Схема данных ядра.

Всё, что нельзя нарушить, выражено ограничением в БД, а не проверкой в коде:
проверку можно снести рефакторингом или обойти гонкой, ограничение - нет.

    payment_events.event_id  PK          вебхук обрабатывается один раз
    issuances.order_item_id  UNIQUE      у позиции не бывает двух выдач
    issuances.code           UNIQUE      один ключ не уйдёт в два заказа
    supplier_attempts.request_id UNIQUE  попытка фиксируется один раз
    order_items (order_id, position)     позиция заказа не задваивается
    jobs (kind, dedupe_key)  partial UNIQUE   одна активная задача на ключ

Единица выдачи - позиция (`order_items`), а не заказ: свой поставщик, свой
request_id, свой исход, свои деньги. Заказ - агрегат над позициями.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
# Каталог
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
        # Покрывающие индексы под keyset-пагинацию витрины: вся карточка
        # товара лежит в INCLUDE, выборка идёт Index Only Scan.
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
    """Снимок остатка по SKU для витрины.

    Истина по остатку у поставщика, сюда её кладёт фоновая синхронизация.
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
        # available в индексе: джойн витрины идёт Index Only Scan.
        Index("ix_sku_stock_covering", "sku", postgresql_include=["available"]),
        # Индекса по available нет намеренно: витрина ходит сюда точечно по
        # sku, а лишний индекс - лишняя запись на каждой выдаче.
    )


# --------------------------------------------------------------------------- #
# Заказы
# --------------------------------------------------------------------------- #
ORDER_STATUSES = (
    "created",
    "paid",
    "delivering",
    "delivered",
    "partially_delivered",   # часть позиций выдана, за остальные деньги вернулись
    "refunded",              # не выдана ни одна позиция, деньги вернулись целиком
    "payment_failed",
    "out_of_stock",
    "delivery_failed",
)
# Статусы позиции. Заказ - агрегат над ними.
ITEM_STATUSES = (
    "pending",           # ждёт оплаты/выдачи
    "delivering",        # выдача идёт прямо сейчас
    "delivered",         # код у покупателя
    "out_of_stock",      # ни у одного поставщика нет кода - но ещё завезут
    "delivery_failed",   # выдать не удалось, повтор имеет смысл
    "unavailable",       # выдать нельзя в принципе - ждать нечего, возвращаем деньги
    "refunded",          # выдать не удалось, деньги за позицию возвращены
)
ITEM_TERMINAL_STATUSES = frozenset({"delivered", "refunded"})


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Сумма заказа = сумма позиций. Имя прежнее: эту величину сверяет вебхук.
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
        Index("ix_orders_created", "created_at"),
    )


class OrderItem(Base):
    """Позиция заказа: единица выдачи, денег и идемпотентности.

    id детерминированный (`<order_id>.<position>`), а не UUID: он попадает в
    request_id к поставщику и в dedupe_key задачи и обязан совпадать при
    любом повторе и после рестарта.
    """

    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.sku", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    supplier: Mapped[str | None] = mapped_column(String(32))
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("order_id", "position", name="uq_order_items_position"),
        CheckConstraint("status IN " + str(ITEM_STATUSES), name="ck_order_items_status"),
        CheckConstraint("amount_minor > 0", name="ck_order_items_amount_positive"),
        Index("ix_order_items_order", "order_id"),
        # Позиции, которые ещё не пришли к финалу: по ним работает доводчик.
        Index(
            "ix_order_items_unfinished",
            "updated_at",
            postgresql_where=(status.notin_(("delivered", "refunded"))),
        ),
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
    """Журнал вебхуков. PK по event_id и есть дедупликация at-least-once."""

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
    order_item_id: Mapped[str] = mapped_column(
        String(48), ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    supplier: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        # Ровно одна выдача на позицию: выдач столько, сколько позиций.
        UniqueConstraint("order_item_id", name="uq_issuances_order_item"),
        # Один ключ не может уйти в два заказа - гарантия из условия.
        UniqueConstraint("code", name="uq_issuances_code"),
        Index("ix_issuances_order", "order_id"),
    )


ATTEMPT_STATES = (
    "in_flight",   # запрос отправлен, ответа ещё нет
    "ok",          # поставщик вернул код
    "failed",      # поставщик ТОЧНО не выдал код (connect refused / явная ошибка)
    "unknown",     # таймаут чтения: код мог быть выдан - трогать нельзя
)


class SupplierAttempt(Base):
    """Журнал обращений к поставщику.

    Строка пишется ДО запроса: если процесс упадёт посреди вызова, останется
    `in_flight`, и восстановление будет знать, что исход неизвестен.
    """

    __tablename__ = "supplier_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    order_item_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
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


DISCREPANCY_KINDS = (
    "duplicate_code",   # код уже принадлежит другому запросу к поставщику
    "foreign_code",     # код относится к другому SKU
    "unknown_code",     # поставщик не признаёт код, который сам же прислал
    "code_collision",   # код уже выдан другой позиции - поймал наш UNIQUE
    "phantom_error",    # ответил ошибкой, а код на самом деле выдал
)


class SupplierDiscrepancy(Base):
    """Расхождение с поставщиком: ответу не поверили, факт зафиксировали.

    Не отчётность, а рабочий механизм: по числу строк считается поколение
    request_id. Отвергнутый код нельзя переспросить тем же request_id -
    поставщик по идемпотентности вернёт тот же мусор.
    """

    __tablename__ = "supplier_discrepancies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    order_item_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = _now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Один и тот же кривой ответ фиксируется один раз, сколько бы раз
        # обработчик ни повторился.
        UniqueConstraint(
            "order_item_id", "supplier", "request_id", "kind", name="uq_discrepancy_once"
        ),
        CheckConstraint("kind IN " + str(DISCREPANCY_KINDS), name="ck_discrepancy_kind"),
        Index(
            "ix_discrepancies_open",
            "detected_at",
            postgresql_where=(resolved_at.is_(None)),
        ),
    )


# --------------------------------------------------------------------------- #
# Деньги
# --------------------------------------------------------------------------- #
LEDGER_ACCOUNTS = (
    "customer",           # обязательства перед клиентом
    "revenue",            # выручка
    "delivered",          # обязательство закрыто выдачей кода
    "supplier_cost",      # себестоимость закупки кода
    "inventory",          # склад кодов
    "refund",             # возвраты
)


class LedgerEntry(Base):
    """Двойная запись: сумма amount_minor внутри txn_id всегда нулевая.

    Инвариант "оплачено = выдано + возвращено" - следствие схемы счетов:
    оплата ставит обязательство на `customer`, выдача и возврат его гасят.
    У завершённого заказа остаток по `customer` обязан быть нулём.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    txn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Позиция проводки. Для проводок уровня заказа (оплата) пустая строка,
    # а не NULL: NULL не равен сам себе, и UNIQUE перестал бы ловить дубли.
    item_id: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    account: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)  # +debit / -credit
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_ledger_nonzero"),
        CheckConstraint("account IN " + str(LEDGER_ACCOUNTS), name="ck_ledger_account"),
        # Проводка по одному поводу, заказу и позиции не задваивается.
        UniqueConstraint(
            "order_id", "item_id", "kind", "account", name="uq_ledger_once_per_order_kind"
        ),
        Index("ix_ledger_order_item", "order_id", "item_id"),
        Index("ix_ledger_txn", "txn_id"),
    )


ORDER_EVENT_TYPES = (
    "order_created",
    "order_paid",
    "order_payment_failed",
    "item_status",       # переход статуса позиции
    "order_status",      # переход статуса заказа
    "item_refunded",
)


class OrderEvent(Base):
    """Append-only история заказа: что произошло и когда.

    Только дополняется, на UPDATE и DELETE стоит триггер (см. app.db).
    Состояние на прошлый момент - свёртка событий до этого момента.
    Событие пишется в одной транзакции со сменой состояния, иначе появится
    окно, в котором состояние изменилось, а история об этом не знает.
    """

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    item_id: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint("type IN " + str(ORDER_EVENT_TYPES), name="ck_order_events_type"),
        Index("ix_order_events_order", "order_id", "id"),
        Index("ix_order_events_time", "occurred_at"),
    )


class SupplierBudget(Base):
    """Token bucket на запросы к поставщику, общий для всех воркеров.

    В БД, а не в памяти: лимит у поставщика один на всех, и два воркера с
    локальными счётчиками превысят его вдвое. Токен снимается атомарным
    UPDATE, блокировка строки и есть сериализация.
    """

    __tablename__ = "supplier_budgets"

    supplier: Mapped[str] = mapped_column(String(32), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    capacity: Mapped[float] = mapped_column(Float, nullable=False)
    refill_per_sec: Mapped[float] = mapped_column(Float, nullable=False)
    taken_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    throttled_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_supplier_budget_capacity"),
        CheckConstraint("refill_per_sec > 0", name="ck_supplier_budget_refill"),
    )


# --------------------------------------------------------------------------- #
# Очередь фоновых задач
# --------------------------------------------------------------------------- #
class Job(Base):
    """Транзакционный outbox и очередь.

    Задача ставится в одной транзакции со сменой статуса заказа, поэтому
    "оплатили, но задачу потеряли" невозможно. Разбор через FOR UPDATE
    SKIP LOCKED.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Меньше - раньше. Выдача оплаченного заказа обгоняет любую фоновую работу.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
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
        # Порядок разбора очереди: сначала приоритет, потом время.
        Index(
            "ix_jobs_ready",
            "priority",
            "run_at",
            postgresql_where=(state == "pending"),
        ),
    )
