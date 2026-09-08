"""Клиент к поставщикам: таймауты, ретраи с бэкоффом, фолбэк с A на B.

Исходов у запроса три, а не два:

  ok               код получен
  definite_failure точно не выдавал (соединение отвергнуто либо явная ошибка)
  unknown          read timeout, исход неизвестен

Read timeout - не отказ, а отсутствие информации: поставщик мог выдать код, а
ответ потерялся. Считать это отказом и уйти к резервному - выдать два кода.
Поэтому фолбэк разрешён только из definite_failure; unknown сначала
разрешается статус-запросом GET /{s}/issue/{request_id}.

Отдельно от исхода - причина. "Нет в наличии" лечится завозом, "я такое не
продаю" не лечится ничем: второе помечается permanent, позиция закрывается
возвратом.

Ответу поставщика верить нельзя: он может прислать чужой код, код от другого
товара или ответить ошибкой, выдав код. Отсюда три правила:

  - любой код проверяется по владельцу (GET /{s}/codes/{code}), не прошедший
    проверку клиенту не уходит;
  - перед фолбэком после явной ошибки спрашиваем статус по request_id, иначе
    "ошибка после выдачи" превратится во второй код у второго поставщика;
  - отвергнутый код переспрашивается под следующим поколением request_id: по
    идемпотентности тот же request_id вернёт тот же мусор. Счётчик поколений -
    число зафиксированных расхождений, поэтому переживает рестарт.

request_id стабилен для пары (позиция, поставщик): единица выдачи - позиция,
у каждой свой независимый разговор с поставщиком.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import SupplierAttempt, SupplierDiscrepancy
from app.services import ratelimit

log = get_logger("supplier")

Kind = Literal["ok", "definite_failure", "unknown", "throttled"]

SUPPLIER_URLS = {"a": settings.supplier_a_url, "b": settings.supplier_b_url}
SUPPLIER_ORDER = ("a", "b")


# Причины, по которым повторять запрос бессмысленно навсегда.
PERMANENT_REASONS = frozenset({"sku_not_supported"})


@dataclass(slots=True)
class Outcome:
    kind: Kind
    supplier: str
    request_id: str
    code: str | None = None
    reason: str | None = None
    http_status: int | None = None
    permanent: bool = False
    retry_after: float | None = None


@dataclass(slots=True)
class DeliveryOutcome:
    kind: Literal["ok", "out_of_stock", "unavailable", "failed", "unknown", "throttled"]
    code: str | None = None
    supplier: str | None = None
    request_id: str | None = None
    reason: str | None = None
    retry_after: float | None = None


def request_id_for(item_id: str, supplier: str, generation: int = 1) -> str:
    """Детерминированный request_id по позиции и поколению.

    Не UUID: обязан совпасть при повторе и пережить рестарт, иначе
    идемпотентность поставщика бесполезна.
    """
    base = f"req_{item_id}-{supplier}"
    return base if generation <= 1 else f"{base}-g{generation}"


async def _generation(item_id: str, supplier: str) -> int:
    """Поколение = число отвергнутых ответов этого поставщика плюс один."""
    async with session_scope() as s:
        n = await s.scalar(
            text(
                "SELECT count(*) FROM supplier_discrepancies "
                " WHERE order_item_id = :iid AND supplier = :sup"
            ),
            {"iid": item_id, "sup": supplier},
        )
    return int(n or 0) + 1


async def record_discrepancy(
    *,
    order_id: str,
    item_id: str,
    supplier: str,
    request_id: str,
    kind: str,
    code: str | None = None,
    detail: str | None = None,
    resolution: str | None = None,
) -> None:
    """Зафиксировать расхождение с поставщиком. Идемпотентно."""
    async with session_scope() as s:
        await s.execute(
            pg_insert(SupplierDiscrepancy)
            .values(
                order_id=order_id,
                order_item_id=item_id,
                supplier=supplier,
                request_id=request_id,
                kind=kind,
                code=code,
                detail=detail,
                resolved_at=func.now() if resolution else None,
                resolution=resolution,
            )
            .on_conflict_do_nothing(constraint="uq_discrepancy_once")
        )
    log.error(
        "supplier.discrepancy", supplier=supplier, item_id=item_id,
        request_id=request_id, kind=kind, code=code, detail=detail,
    )


async def _record_attempt_start(
    request_id: str, order_id: str, item_id: str, supplier: str, attempt_no: int
):
    """Журнал намерения: пишем ДО запроса и коммитим сразу.

    Умрём в момент вызова - останется `in_flight`, и восстановление будет
    знать, что исход неизвестен.
    """
    async with session_scope() as s:
        s.add(
            SupplierAttempt(
                request_id=request_id,
                order_id=order_id,
                order_item_id=item_id,
                supplier=supplier,
                attempt_no=attempt_no,
                state="in_flight",
            )
        )


async def _record_attempt_end(
    request_id: str,
    attempt_no: int,
    state: str,
    *,
    code: str | None = None,
    reason: str | None = None,
    http_status: int | None = None,
    latency_ms: int | None = None,
):
    async with session_scope() as s:
        await s.execute(
            text(
                """
                UPDATE supplier_attempts
                   SET state=:st, code=:code, reason=:reason, http_status=:hs,
                       latency_ms=:lat, finished_at=now()
                 WHERE request_id=:rid AND attempt_no=:no
                """
            ),
            {
                "st": state, "code": code, "reason": reason, "hs": http_status,
                "lat": latency_ms, "rid": request_id, "no": attempt_no,
            },
        )


class SupplierClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owned = client is None

    async def __aenter__(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=settings.supplier_connect_timeout,
                    read=settings.supplier_read_timeout,
                    write=settings.supplier_read_timeout,
                    pool=settings.supplier_read_timeout,
                )
            )
        return self

    async def __aexit__(self, *exc):
        if self._owned and self._client is not None:
            await self._client.aclose()

    # --------------------------------------------------------------- #
    async def _call_issue(
        self, supplier: str, order_id: str, item_id: str, sku: str,
        request_id: str, attempt_no: int,
    ) -> Outcome:
        # Лимит проверяется до журнала попыток: ожидание своего окна - не
        # попытка выдачи, и бюджет попыток на неё тратить нельзя.
        wait = await ratelimit.take(supplier)
        if wait is not None:
            return Outcome(
                "throttled", supplier, request_id,
                reason=f"supplier rate limit, wait {wait:.2f}s", retry_after=wait,
            )

        url = f"{SUPPLIER_URLS[supplier]}/{supplier}/issue"
        await _record_attempt_start(request_id, order_id, item_id, supplier, attempt_no)
        started = datetime.now(timezone.utc)

        def elapsed_ms() -> int:
            return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

        try:
            r = await self._client.post(
                url, json={"request_id": request_id, "sku": sku, "order_id": order_id}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Соединение не установлено: запрос не дошёл, код не выдан.
            await _record_attempt_end(
                request_id, attempt_no, "failed", reason=f"connect: {exc!r}",
                latency_ms=elapsed_ms(),
            )
            return Outcome("definite_failure", supplier, request_id, reason="unreachable")
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                httpx.RemoteProtocolError) as exc:
            # Таймаут не отказ: поставщик мог успеть выдать код.
            await _record_attempt_end(
                request_id, attempt_no, "unknown", reason=f"timeout: {exc!r}",
                latency_ms=elapsed_ms(),
            )
            log.warning(
                "supplier.timeout", supplier=supplier, order_id=order_id,
                request_id=request_id, attempt=attempt_no,
                note="outcome unknown, code may have been issued",
            )
            return Outcome("unknown", supplier, request_id, reason="timeout")

        latency = elapsed_ms()
        if r.status_code == 200:
            body = r.json()
            code = body.get("code")
            await _record_attempt_end(
                request_id, attempt_no, "ok", code=code, http_status=200, latency_ms=latency
            )
            return Outcome("ok", supplier, request_id, code=code, http_status=200)

        reason = _reason_of(r)
        if r.status_code == 429:
            # Свой лимитер это должен был предотвратить. Раз не предотвратил,
            # верим поставщику и ждём, а не считаем отказом.
            await _record_attempt_end(
                request_id, attempt_no, "failed", reason=reason,
                http_status=429, latency_ms=latency,
            )
            retry_after = r.headers.get("Retry-After")
            log.warning(
                "supplier.rate_limited", supplier=supplier, item_id=item_id,
                request_id=request_id, retry_after=retry_after,
            )
            return Outcome(
                "throttled", supplier, request_id, reason=reason, http_status=429,
                retry_after=float(retry_after) if retry_after else 1.0,
            )

        if r.status_code == 504:
            # Шлюзовой таймаут: исход неизвестен.
            await _record_attempt_end(
                request_id, attempt_no, "unknown", reason=reason,
                http_status=r.status_code, latency_ms=latency,
            )
            return Outcome("unknown", supplier, request_id, reason=reason, http_status=504)

        # Явная ошибка: поставщик зафиксировал исход по этому request_id.
        await _record_attempt_end(
            request_id, attempt_no, "failed", reason=reason,
            http_status=r.status_code, latency_ms=latency,
        )
        return Outcome(
            "definite_failure", supplier, request_id, reason=reason,
            http_status=r.status_code,
            permanent=reason in PERMANENT_REASONS or r.status_code == 410,
        )

    async def owner_of(self, supplier: str, code: str) -> tuple[str, dict | None]:
        """Кому принадлежит код по данным поставщика.

        ("found", запись), ("missing", None) или ("unverified", None).
        Последнее - не отказ, а отсутствие информации.
        """
        url = f"{SUPPLIER_URLS[supplier]}/{supplier}/codes/{code}"
        try:
            r = await self._client.get(url)
        except httpx.HTTPError as exc:
            log.warning("supplier.verify_failed", supplier=supplier, error=repr(exc))
            return "unverified", None
        if r.status_code == 200:
            return "found", r.json()
        if r.status_code == 404:
            return "missing", None
        return "unverified", None

    async def _accept(
        self, outcome: Outcome, order_id: str, item_id: str, sku: str
    ) -> Outcome:
        """Принять код, только если он наш и от нужного товара.

        Проверка внешняя: ответ может быть каким угодно, пул ключей врать не
        умеет.
        """
        supplier, code, request_id = outcome.supplier, outcome.code, outcome.request_id
        if not code:
            return Outcome(
                "definite_failure", supplier, request_id, reason="empty code from supplier"
            )

        state, rec = await self.owner_of(supplier, code)
        if state == "unverified":
            # Проверить не смогли - выдавать нельзя, но и отказом это не является.
            return Outcome("unknown", supplier, request_id, reason="code not verifiable")

        kind = detail = None
        if state == "missing":
            kind = "unknown_code"
            detail = "supplier does not know the code it returned"
        elif rec.get("request_id") not in (request_id, None):
            kind = "duplicate_code"
            detail = f"code belongs to {rec.get('request_id')}"
        elif rec.get("sku") != sku:
            kind = "foreign_code"
            detail = f"code is for sku {rec.get('sku')}, ordered {sku}"

        if kind is None:
            return outcome

        await record_discrepancy(
            order_id=order_id, item_id=item_id, supplier=supplier,
            request_id=request_id, kind=kind, code=code, detail=detail,
        )
        # Ответ этого поставщика исчерпан: сейчас идём к следующему, а повтор
        # к этому пойдёт под новым поколением request_id.
        return Outcome(
            "definite_failure", supplier, request_id, reason=f"rejected: {kind}"
        )

    async def probe(self, supplier: str, request_id: str) -> Outcome:
        """Выдавал ли поставщик код по этому request_id."""
        url = f"{SUPPLIER_URLS[supplier]}/{supplier}/issue/{request_id}"
        try:
            r = await self._client.get(url)
        except httpx.HTTPError as exc:
            return Outcome("unknown", supplier, request_id, reason=f"probe failed: {exc!r}")
        if r.status_code == 200:
            return Outcome("ok", supplier, request_id, code=r.json().get("code"), http_status=200)
        if r.status_code == 404:
            # Такого request_id поставщик не знает: он ничего не выдавал.
            return Outcome(
                "definite_failure", supplier, request_id, reason="never_issued", http_status=404
            )
        return Outcome("unknown", supplier, request_id, reason=_reason_of(r))

    async def _try_supplier(
        self, supplier: str, order_id: str, item_id: str, sku: str
    ) -> Outcome:
        """Повторы с бэкоффом к одному поставщику под одним request_id."""
        request_id = request_id_for(item_id, supplier, await _generation(item_id, supplier))
        last = Outcome("definite_failure", supplier, request_id, reason="no attempts")
        base_attempt = await _next_attempt_no(request_id)

        for i in range(settings.supplier_max_attempts):
            attempt_no = base_attempt + i
            last = await self._call_issue(
                supplier, order_id, item_id, sku, request_id, attempt_no
            )
            if last.kind == "ok":
                return await self._accept(last, order_id, item_id, sku)
            if last.kind == "throttled":
                return last  # мест в лимите нет: ждём, а не идём к другому
            if last.kind == "definite_failure" and (
                last.reason == "out_of_stock" or last.permanent
            ):
                return last  # ретраить бессмысленно, нужен другой поставщик
            if i < settings.supplier_max_attempts - 1:
                delay = min(
                    settings.supplier_backoff_max,
                    settings.supplier_backoff_base * (2 ** i),
                )
                await asyncio.sleep(delay * (0.5 + random.random()))  # jitter

        if last.kind == "unknown" or not (last.permanent or last.reason == "out_of_stock"):
            # Ни таймаут, ни явная ошибка не доказывают, что кода нет:
            # поставщик мог выдать его и не донести ответ. Уйти к следующему,
            # не выяснив этого, значит выдать клиенту второй код.
            resolved = await self.probe(supplier, request_id)
            log.info(
                "supplier.probe", supplier=supplier, item_id=item_id,
                request_id=request_id, after=last.kind, resolved=resolved.kind,
                reason=resolved.reason,
            )
            if resolved.kind == "ok":
                if last.kind == "definite_failure":
                    # Ответил ошибкой, а код выдал. Расхождение закрыто сразу.
                    await record_discrepancy(
                        order_id=order_id, item_id=item_id, supplier=supplier,
                        request_id=request_id, kind="phantom_error", code=resolved.code,
                        detail=f"error response '{last.reason}' but code was issued",
                        resolution="code recovered by status probe",
                    )
                await _record_attempt_end(
                    request_id, base_attempt + settings.supplier_max_attempts - 1,
                    "ok", code=resolved.code, reason="resolved by probe",
                )
                return await self._accept(resolved, order_id, item_id, sku)
            if last.kind == "unknown":
                return resolved
        return last

    # --------------------------------------------------------------- #
    async def acquire_code(self, order_id: str, item_id: str, sku: str) -> DeliveryOutcome:
        """Получить код на позицию: A, при точном отказе B. Ровно один код."""
        # Сначала проверяем, не висит ли уже выданный код у поставщиков.
        settled = await self._settle_known(order_id, item_id, sku)
        if settled is not None:
            return settled

        last_reason = None
        permanent_everywhere = True
        for supplier in SUPPLIER_ORDER:
            outcome = await self._try_supplier(supplier, order_id, item_id, sku)
            if outcome.kind == "ok":
                return DeliveryOutcome(
                    "ok", code=outcome.code, supplier=supplier, request_id=outcome.request_id
                )
            if outcome.kind == "throttled":
                # Лимит - не отказ: задача вернётся в очередь и дождётся окна.
                return DeliveryOutcome(
                    "throttled", supplier=supplier, request_id=outcome.request_id,
                    reason=outcome.reason, retry_after=outcome.retry_after,
                )
            if outcome.kind == "unknown":
                # Исход не выяснен: фолбэк запрещён.
                log.warning(
                    "supplier.unresolved", supplier=supplier, item_id=item_id,
                    request_id=outcome.request_id,
                    note="fallback blocked to avoid double issuance",
                )
                return DeliveryOutcome(
                    "unknown", supplier=supplier, request_id=outcome.request_id,
                    reason=outcome.reason,
                )
            last_reason = outcome.reason
            permanent_everywhere = permanent_everywhere and outcome.permanent
            log.info(
                "supplier.failover", supplier=supplier, item_id=item_id,
                reason=outcome.reason, next=("b" if supplier == "a" else None),
            )

        if last_reason == "out_of_stock":
            return DeliveryOutcome("out_of_stock", reason=last_reason)
        if permanent_everywhere:
            # Никто такое не продаёт, ждать нечего.
            return DeliveryOutcome("unavailable", reason=last_reason or "not supported")
        return DeliveryOutcome("failed", reason=last_reason or "all suppliers failed")

    async def _settle_known(
        self, order_id: str, item_id: str, sku: str
    ) -> DeliveryOutcome | None:
        """Выяснить исход незакрытых попыток до нового запроса.

        Путь восстановления после падения: `in_flight` и `unknown` означают
        "возможно, код уже выдан".
        """
        async with session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT DISTINCT supplier, request_id
                          FROM supplier_attempts
                         WHERE order_item_id = :iid AND state IN ('in_flight','unknown','ok')
                        """
                    ),
                    {"iid": item_id},
                )
            ).all()
        for supplier, request_id in rows:
            resolved = await self.probe(supplier, request_id)
            if resolved.kind == "ok":
                checked = await self._accept(resolved, order_id, item_id, sku)
                if checked.kind != "ok":
                    # Восстановленный код не наш: идём обычным путём под
                    # новым поколением.
                    continue
                log.info(
                    "supplier.recovered_code", supplier=supplier, item_id=item_id,
                    request_id=request_id,
                )
                await _record_attempt_end(
                    request_id, await _last_attempt_no(request_id), "ok",
                    code=checked.code, reason="resolved before retry",
                )
                return DeliveryOutcome(
                    "ok", code=checked.code, supplier=supplier, request_id=request_id
                )
            if resolved.kind == "unknown":
                return DeliveryOutcome(
                    "unknown", supplier=supplier, request_id=request_id, reason=resolved.reason
                )
            # definite_failure: идём дальше по обычному сценарию
        return None


def _reason_of(r: httpx.Response) -> str:
    try:
        body = r.json()
    except Exception:
        return f"http_{r.status_code}"
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        return detail.get("reason") or f"http_{r.status_code}"
    return str(detail)[:200]


async def _next_attempt_no(request_id: str) -> int:
    async with session_scope() as s:
        n = await s.scalar(
            text("SELECT COALESCE(MAX(attempt_no),0) FROM supplier_attempts WHERE request_id=:r"),
            {"r": request_id},
        )
    return int(n or 0) + 1


async def _last_attempt_no(request_id: str) -> int:
    async with session_scope() as s:
        n = await s.scalar(
            text("SELECT COALESCE(MAX(attempt_no),0) FROM supplier_attempts WHERE request_id=:r"),
            {"r": request_id},
        )
    return int(n or 1)
