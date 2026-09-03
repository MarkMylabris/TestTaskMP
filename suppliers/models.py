"""Состояние заглушек. База отдельная: поставщик - внешняя система, а не наша таблица."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SupplierKey(Base):
    __tablename__ = "supplier_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier: Mapped[str] = mapped_column(String(8), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="available")
    request_id: Mapped[str | None] = mapped_column(String(128))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("code", name="uq_supplier_keys_code"),
        CheckConstraint("state IN ('available','issued')", name="ck_supplier_keys_state"),
        Index(
            "ix_supplier_keys_pool",
            "supplier",
            "sku",
            "id",
            postgresql_where=(state == "available"),
        ),
    )


class SupplierRequest(Base):
    """Идемпотентность поставщика: один request_id -> один и тот же ответ навсегда."""

    __tablename__ = "supplier_requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    supplier: Mapped[str] = mapped_column(String(8), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | error
    code: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
