from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderItemIn(BaseModel):
    sku: str = Field(..., min_length=1, max_length=64)
    qty: int = Field(default=1, ge=1, le=20)


class CreateOrderRequest(BaseModel):
    """Заказ из одного или нескольких товаров.

    `sku` - короткая форма: один товар это частный случай списка.
    """

    sku: str | None = Field(default=None, min_length=1, max_length=64)
    items: list[OrderItemIn] | None = None
    customer_email: str | None = None
    # Свой id заказа даёт идемпотентность создания и позволяет воспроизвести
    # сценарий "вебхук пришёл раньше заказа".
    order_id: str | None = Field(default=None, min_length=3, max_length=40,
                                 pattern=r"^[A-Za-z0-9_\-]+$")

    @model_validator(mode="after")
    def _one_form(self):
        if bool(self.sku) == bool(self.items):
            raise ValueError("provide either 'sku' or 'items', not both")
        return self

    def sku_list(self) -> list[str]:
        """Список SKU: одна позиция на единицу товара."""
        if self.sku:
            return [self.sku]
        return [i.sku for i in self.items for _ in range(i.qty)]


class IssuanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    supplier: str
    request_id: str
    created_at: datetime


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    sku: str
    amount: float
    amount_minor: int
    currency: str
    status: str
    supplier: str | None = None
    delivery_attempts: int = 0
    last_error: str | None = None
    delivered_at: datetime | None = None
    refunded_at: datetime | None = None
    issuance: IssuanceOut | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    amount: float
    amount_minor: int
    currency: str
    status: str
    delivery_attempts: int
    last_error: str | None = None
    created_at: datetime
    paid_at: datetime | None = None
    delivered_at: datetime | None = None
    items: list[OrderItemOut] = []
    # Совместимость с однотоварным контрактом: для заказа из одной позиции
    # это её sku и её выдача, для многотоварного - None.
    sku: str | None = None
    issuance: IssuanceOut | None = None


class PaymentWebhook(BaseModel):
    """Контракт вебхука платёжной системы (из задания)."""

    event_id: str = Field(..., min_length=1, max_length=128)
    order_id: str = Field(..., min_length=1, max_length=40)
    status: str
    amount: float
    currency: str = Field(..., min_length=3, max_length=3)
    created_at: datetime


class WebhookAck(BaseModel):
    accepted: bool
    event_id: str
    order_id: str
    result: str
    order_status: str | None = None
    note: str | None = None


class ProductOut(BaseModel):
    sku: str
    name: str
    type: str
    price: float
    currency: str
    image: str | None
    available: int
    in_stock: bool


class StorefrontPage(BaseModel):
    items: list[ProductOut]
    next_cursor: str | None = None
    took_ms: float
