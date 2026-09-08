# API

Живая документация со схемами и Swagger на `http://localhost:8000/docs`
(и `/redoc`, если больше нравится он). Здесь то же самое, примерами, объяснением поведения в неочевидных случаях: повторы, гонки, частичные отказы.

Базовый URL по умолчанию: `http://localhost:8000`.
Авторизации нет - это тестовое задание; в проде `/admin/*` был бы за отдельным контуром.

## Оглавление

- [Общее: форматы, деньги, ошибки](#общее-форматы-деньги-ошибки)
- [Заказы](#заказы)
- [Вебхук оплаты](#вебхук-оплаты)
- [Каталог и витрина](#каталог-и-витрина)
- [Админ: сверка и наблюдаемость](#админ-сверка-и-наблюдаемость)
- [Админ: деньги и история](#админ-деньги-и-история)
- [Служебное](#служебное)
- [Статусы заказа и позиции](#статусы-заказа-и-позиции)
- [API заглушек-поставщиков](#api-заглушек-поставщиков)
- [Сквозной сценарий целиком](#сквозной-сценарий-целиком)

---

## Общее: форматы, деньги, ошибки

Всё JSON, кодировка UTF-8. Время - ISO 8601 в UTC.

**Про деньги.** Наружу цена отдаётся двумя полями: `amount` (рубли, как в задании -
`500`, `1290.0`) и `amount_minor` (копейки, целое - `50000`, `129000`). Внутри и в
журнале проводок живут только копейки: считать деньги во float нельзя, но и ломать
контракт из задания не хотелось, поэтому наружу отдаётся и то, и другое.

**Ошибки** - стандартный формат FastAPI:

```json
{ "detail": "unknown sku: NOPE-000" }
```

| Код | Когда |
|-----|-------|
| `400` / `422` | тело не прошло валидацию |
| `404` | нет такого заказа, SKU или товара |
| `409` | конфликт: `order_id` занят другим составом, возврат по уже выданному заказу |
| `422` | нет обеих форм заказа или обе сразу, товары в разных валютах, `to` раньше `from` |
| `500` | внутренняя ошибка (в теле - `{"detail": "internal error"}`, подробности в логах) |

**Трассировка.** Каждый ответ содержит заголовок `x-request-id`. Свой можно
передать во входящем запросе тем же заголовком - он попадёт во все строки лога
этого запроса.

---

## Заказы

### `POST /orders` - создать заказ

Две формы. Короткая - один товар, как в первом этапе:

```json
{ "sku": "KEY-CS2-PRIME", "customer_email": "player@example.com" }
```

Полная - несколько товаров:

```json
{
  "items": [
    { "sku": "KEY-CS2-PRIME" },
    { "sku": "KEY-EFT" },
    { "sku": "SUB-YT-3M", "qty": 2 }
  ],
  "customer_email": "player@example.com",
  "order_id": "ord_my_own_id"
}
```

| Поле | Обяз. | Описание |
|------|-------|----------|
| `sku` | одно из двух | SKU из каталога, короткая форма на один товар |
| `items` | одно из двух | список `{sku, qty}`, `qty` от 1 до 20, всего до 50 позиций |
| `customer_email` | нет | куда отправлять коды (в этом ядре только хранится) |
| `order_id` | нет | свой идентификатор, `[A-Za-z0-9_-]{3,40}` |

`sku` и `items` взаимоисключающие: передать оба или ни одного - `422`.
`qty: 2` разворачивается в две отдельные позиции: единица товара - это один код,
и выдаётся он своим запросом к поставщику.

Про `order_id`: Если его не передать, id сгенерируем сами
(`ord_` + 16 hex). Если передать - создание становится идемпотентным: повторный
`POST` с тем же `order_id` и тем же составом корзины вернёт существующий заказ и
`200` вместо `201`, а не заведёт второй. Это же поле позволяет воспроизвести
сценарий «вебхук раньше заказа».

Все товары заказа должны быть в одной валюте, иначе `422`.

**`201 Created`** (или `200 OK`, если заказ уже был):

```json
{
  "id": "ord_1838952e8abb4d34",
  "amount": 5780.0,
  "amount_minor": 578000,
  "currency": "RUB",
  "status": "created",
  "delivery_attempts": 0,
  "last_error": null,
  "created_at": "2026-09-08T10:22:28.204483Z",
  "paid_at": null,
  "delivered_at": null,
  "items": [
    {
      "id": "ord_1838952e8abb4d34.1",
      "position": 1,
      "sku": "KEY-CS2-PRIME",
      "amount": 1290.0,
      "amount_minor": 129000,
      "currency": "RUB",
      "status": "pending",
      "supplier": null,
      "delivery_attempts": 0,
      "last_error": null,
      "delivered_at": null,
      "refunded_at": null,
      "issuance": null
    }
  ],
  "sku": null,
  "issuance": null
}
```

`amount_minor` заказа всегда равен сумме позиций - именно эту величину сверяет
вебхук оплаты.

Идентификатор позиции детерминированный: `<order_id>.<position>`. Он же уходит в
`request_id` к поставщику и в ключ дедупликации фоновой задачи, поэтому переживает
рестарт и совпадает при любом повторе.

Поля `sku` и `issuance` на верхнем уровне - совместимость с однотоварным
контрактом первого этапа. Для заказа из одной позиции там её SKU и её выдача,
для многотоварного - `null`.

Ошибки: `404` - нет такого SKU или он выключен; `409` - `order_id` уже занят
заказом с другим составом; `422` - обе формы сразу, ни одной, или разные валюты.

```bash
curl -sX POST localhost:8000/orders \
  -H 'content-type: application/json' \
  -d '{"items":[{"sku":"KEY-CS2-PRIME"},{"sku":"GIFT-PSN-1000"}]}'
```

### `GET /orders/{order_id}` - заказ, коды и возвраты

Тот же объект. После выдачи у позиции заполняется `issuance`, после возврата -
`refunded_at`. Заказ, где часть товаров выдать не удалось:

```json
{
  "id": "ord_1838952e8abb4d34",
  "amount_minor": 578000,
  "status": "partially_delivered",
  "delivery_attempts": 4,
  "last_error": "2 delivered, 1 refunded",
  "paid_at": "2026-09-08T10:22:36.106492Z",
  "delivered_at": "2026-09-08T10:22:36.872101Z",
  "items": [
    {
      "id": "ord_1838952e8abb4d34.1", "position": 1, "sku": "KEY-CS2-PRIME",
      "amount_minor": 129000, "status": "delivered", "supplier": "a",
      "delivery_attempts": 1, "delivered_at": "2026-09-08T10:22:36.262573Z",
      "refunded_at": null,
      "issuance": {
        "code": "EXTA-KEY-CS2-PRIME-00000", "supplier": "a",
        "request_id": "req_ord_1838952e8abb4d34.1-a",
        "created_at": "2026-09-08T10:22:36.262573Z"
      }
    },
    {
      "id": "ord_1838952e8abb4d34.2", "position": 2, "sku": "KEY-EFT",
      "amount_minor": 349000, "status": "refunded", "supplier": null,
      "delivery_attempts": 2, "last_error": "refunded: supplier cannot fulfil",
      "delivered_at": null, "refunded_at": "2026-09-08T10:22:36.660340Z",
      "issuance": null
    }
  ]
}
```

`issuance` - это факт выдачи. Их у позиции не может быть двух: в БД стоит
`UNIQUE(order_item_id)`, и `UNIQUE(code)` сверх того не даёт одному коду уйти в
два заказа. Даже если приложение попробует, БД не даст.

`last_error` - человекочитаемая причина последней неудачи
(`out_of_stock at all suppliers`, `not fulfillable: sku_not_supported`,
`refunded: supplier cannot fulfil`). При успешной выдаче очищается.

Ошибки: `404`.

---

## Вебхук оплаты

### `POST /webhook/payment`

Контракт ровно тот, что в задании:

```json
{
  "event_id": "evt_a1b2c3",
  "order_id": "ord_00123",
  "status": "paid",
  "amount": 500,
  "currency": "RUB",
  "created_at": "2025-01-01T12:00:00Z"
}
```

`status` - `paid` либо `failed`, иначе `422`. Подпись не проверяется (в задании
это явно исключено).

Ответ **всегда `200`, если событие принято**. Поле `result` говорит, что с ним
произошло:

```json
{
  "accepted": true,
  "event_id": "evt_a1b2c3",
  "order_id": "ord_00123",
  "result": "applied",
  "order_status": "paid",
  "note": null
}
```

| `result` | Что случилось | Что делать платёжке |
|----------|---------------|---------------------|
| `applied` | событие изменило состояние заказа | ничего, доставлено |
| `duplicate` | такой `event_id` уже обработан | ничего |
| `ignored` | событие корректно, но неприменимо - заказ уже не в `created` | ничего |
| `orphan` | заказа ещё нет; событие сохранено и применится, когда он появится | ничего |
| `rejected` | сумма или валюта не совпали с заказом | разбираться людям |

`5xx` отдаётся только при реальном сбое БД. Тогда ретрай платёжки как раз и нужен -
а вот ретраить то, что мы уже успешно сохранили, смысла нет, поэтому «сирота» и
«дубликат» это `200`.

**Гарантии, которые здесь держатся:**

- *At-least-once.* `event_id` - первичный ключ таблицы событий. Повтор физически
  не может быть обработан дважды.
- *Параллельные вебхуки.* 50 запросов с разными `event_id` по одному заказу
  выстраиваются в очередь на `SELECT ... FOR UPDATE` строки заказа. Переход
  `created -> paid` разрешён только из `created`, поэтому применяется ровно один,
  остальные 49 отвечают `ignored`. Проверяется тестом
  `test_50_parallel_webhooks_issue_exactly_once`.
- *Не по порядку.* `failed`, пришедший после `paid` или после выдачи, не откатывает
  заказ: он логируется как аномалия и получает `ignored`.
- *Быстрый ответ.* Внутри обработчика только БД. К поставщику ходит фоновый воркер,
  а задача на выдачу ставится в той же транзакции, что и смена статуса.

```bash
curl -sX POST localhost:8000/webhook/payment \
  -H 'content-type: application/json' \
  -d '{"event_id":"evt_1","order_id":"ord_59947ae410fb4f30","status":"paid",
       "amount":399,"currency":"RUB","created_at":"2026-01-01T12:00:00Z"}'
```

Удобнее не собирать JSON руками, а взять эмулятор платёжки:

```bash
python -m scripts.payment_sim pay  --order ord_xxx
python -m scripts.payment_sim pay  --order ord_xxx --status failed
python -m scripts.payment_sim race --order ord_xxx --concurrency 50
python -m scripts.payment_sim race --order ord_xxx --concurrency 50 --same-event
```

---

## Каталог и витрина

### `GET /catalog/storefront` - список товаров с остатками

Тот самый «горячий» запрос витрины.

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `type` | - | `topup` \| `key` \| `subscription` \| `giftcard` |
| `in_stock` | `false` | показывать только то, что есть в наличии |
| `cursor` | - | последний `sku` предыдущей страницы |
| `limit` | `50` | 1..200 |

```json
{
  "items": [
    {
      "sku": "KEY-CS2-PRIME",
      "name": "CS2 Prime Status ключ",
      "type": "key",
      "price": 1290.0,
      "currency": "RUB",
      "image": "assets/cs2.png",
      "available": 8,
      "in_stock": true
    }
  ],
  "next_cursor": "KEY-EFT",
  "took_ms": 1.47
}
```

Пагинация - keyset, а не OFFSET: `next_cursor` это `sku` последнего элемента
страницы, его и надо передать в `cursor` за следующей. Когда страница пришла
неполной, `next_cursor` будет `null` - это конец выборки. Разница с OFFSET на
50k SKU: 0.18 мс против 35 мс на десятитысячной позиции.

`available` берётся из снимка остатков `sku_stock`, который фоновая задача
синхронизирует с поставщиков. То есть это витринное «есть/нет», а не резерв:
источник истины по остатку - поставщик, и окончательно всё решается в момент
выдачи. Для витрины такой компромисс нормальный, для резервирования - нет.

```bash
curl -s "localhost:8000/catalog/storefront?type=key&in_stock=true&limit=20"
curl -s "localhost:8000/catalog/storefront?type=key&cursor=KEY-EFT&limit=20"
```

### `GET /catalog/storefront/explain` - план выполнения

Параметры те же. Возвращает сам SQL и `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`
того же запроса - удобно проверять на проде, что план не «уплыл» после роста
каталога или изменения статистики.

```json
{ "sql": "SELECT p.sku, ...", "plan": [ { "Plan": { "Node Type": "Limit", "...": "..." } } ] }
```

На что смотреть: `Node Type` должен быть `Index Only Scan`, `Heap Fetches` - ноль,
`Sort` и `Seq Scan` не должно быть вовсе. Ровно это проверяет
`tests/test_stage5_catalog.py`.

### `GET /catalog/products/{sku}` - карточка товара

Один элемент того же вида, что в `items`. `404`, если SKU нет или он выключен.

---

## Админ: сверка и наблюдаемость

### `GET /admin/reconciliation` - отчёт сверки

Параметры: `grace_seconds` (по умолчанию `STUCK_ORDER_SECONDS`, обычно 30) -
сколько времени даём заказу на нормальную выдачу, прежде чем считать его
проблемным; `limit` (1..1000).

```json
{
  "generated_at": "2026-09-08T10:25:52Z",
  "grace_seconds": 0,
  "paid_not_delivered":            { "count": 0, "items": [] },
  "delivered_not_paid":            { "count": 0, "items": [] },
  "stuck_orders":                  { "count": 0, "items": [] },
  "unresolved_supplier_attempts":  { "count": 0, "items": [] },
  "orphan_payment_events":         { "count": 0, "items": [] },
  "ledger": {
    "total_minor": 0,
    "balanced": true,
    "unbalanced_transactions": [],
    "by_account": [
      { "account": "customer",      "total":        0 },
      { "account": "delivered",     "total":  1629000 },
      { "account": "refund",        "total":   349000 },
      { "account": "revenue",       "total": -1978000 },
      { "account": "supplier_cost", "total":  1140300 },
      { "account": "inventory",     "total": -1140300 }
    ]
  },
  "money": {
    "paid_minor": 1978000,
    "delivered_minor": 1629000,
    "refunded_minor": 349000,
    "open_minor": 0,
    "equation": "1978000 = 1629000 + 349000 + 0",
    "balanced": true,
    "unsettled_finished_orders": { "count": 0, "items": [] }
  },
  "supplier_discrepancies": {
    "by_kind": [ { "kind": "duplicate_code", "total": 1, "open": 0 } ],
    "open":            { "count": 0, "items": [] },
    "needs_attention": { "count": 0, "items": [] }
  },
  "healthy": true
}
```

Что означает каждый раздел:

- **`paid_not_delivered`** - деньги взяли, товар не отдали. Потеря для клиента,
  самая болезненная категория. Заказ старше `grace_seconds`, оплачен, есть
  позиции, не закрытые ни кодом, ни возвратом.
- **`delivered_not_paid`** - товар отдали, денег нет. Потеря для нас. В норме
  всегда пусто: выдача возможна только из `paid`.
- **`stuck_orders`** - оплачен, не финализирован, давно не обновлялся. Их
  подхватывает фоновый «доводчик».
- **`unresolved_supplier_attempts`** - попытки в состоянии `in_flight` или
  `unknown`, по которым нет выдачи. Исход у внешней системы неизвестен, и
  трогать их вслепую нельзя.
- **`orphan_payment_events`** - вебхуки, для которых заказ так и не появился.
- **`ledger.balanced`** - сумма журнала двойной записи равна нулю.
- **`money.balanced`** - выполняется ли `оплачено = выдано + возвращено + ещё не
  закрыто`, и нет ли завершённых заказов с незакрытым обязательством.
- **`supplier_discrepancies.needs_attention`** - открытые расхождения по уже
  закрытым позициям. В норме пусто: расхождение закрывается, когда позиция
  закрыта кодом или деньгами.

`healthy` - короткий вердикт: нет расхождений товара, журнал сходится, денежное
уравнение выполняется, неразобранных расхождений с поставщиками нет.

```bash
curl -s "localhost:8000/admin/reconciliation?grace_seconds=0" | python3 -m json.tool
```

### `GET /admin/orders/{order_id}/timeline` - вся история заказа

Одним ответом: заказ, позиции, вебхуки, попытки к поставщикам, расхождения,
выдачи, проводки. Первое, что открываешь, когда разбираешь инцидент.

```json
{
  "order": { "id": "ord_51295b242a9b44b0", "status": "delivered", "...": "..." },
  "items": [
    { "id": "ord_51295b242a9b44b0.1", "position": 1, "sku": "STEAM-TOPUP-1000",
      "amount_minor": 100000, "status": "delivered", "supplier": "b",
      "delivery_attempts": 1, "delivered_at": "...", "refunded_at": null }
  ],
  "payment_events": [
    { "event_id": "evt_d3", "status": "paid", "processing_state": "applied",
      "note": "created to paid", "event_created_at": "...", "received_at": "..." }
  ],
  "supplier_attempts": [
    { "order_item_id": "ord_51295b242a9b44b0.1", "supplier": "a",
      "request_id": "req_ord_51295b242a9b44b0.1-a", "attempt_no": 1,
      "state": "ok", "http_status": 200, "code": "EXTA-STEAM-TOPUP-1000-00000",
      "latency_ms": 6, "started_at": "...", "finished_at": "..." },
    { "order_item_id": "ord_51295b242a9b44b0.1", "supplier": "b",
      "request_id": "req_ord_51295b242a9b44b0.1-b", "attempt_no": 1,
      "state": "ok", "http_status": 200, "code": "P3EI-W8UO-9B4K",
      "latency_ms": 6, "started_at": "...", "finished_at": "..." }
  ],
  "supplier_discrepancies": [
    { "order_item_id": "ord_51295b242a9b44b0.1", "supplier": "a",
      "request_id": "req_ord_51295b242a9b44b0.1-a", "kind": "duplicate_code",
      "code": "EXTA-STEAM-TOPUP-1000-00000",
      "detail": "code belongs to req_ord_3df5e7ec011745da.1-a",
      "detected_at": "...", "resolved_at": "...", "resolution": "reissued" }
  ],
  "issuances": [
    { "order_item_id": "ord_51295b242a9b44b0.1", "sku": "STEAM-TOPUP-1000",
      "code": "P3EI-W8UO-9B4K", "supplier": "b",
      "request_id": "req_ord_51295b242a9b44b0.1-b", "created_at": "..." }
  ],
  "issuance": { "...": "то же самое, если позиция одна; иначе null" },
  "ledger_entries": [
    { "kind": "payment_captured", "item_id": "",                       "account": "customer",      "amount_minor":  100000 },
    { "kind": "payment_captured", "item_id": "",                       "account": "revenue",       "amount_minor": -100000 },
    { "kind": "delivery_settled", "item_id": "ord_51295b242a9b44b0.1", "account": "delivered",     "amount_minor":  100000 },
    { "kind": "delivery_settled", "item_id": "ord_51295b242a9b44b0.1", "account": "customer",      "amount_minor": -100000 },
    { "kind": "delivery_cost",    "item_id": "ord_51295b242a9b44b0.1", "account": "supplier_cost", "amount_minor":   70000 },
    { "kind": "delivery_cost",    "item_id": "ord_51295b242a9b44b0.1", "account": "inventory",     "amount_minor":  -70000 }
  ]
}
```

Этот пример читается так: поставщик A вернул код, который уже принадлежал
другому запросу; мы это увидели, зафиксировали расхождение, клиенту его не
отдали и взяли код у B. Выдача осталась одна.

`state` у попытки:

| `state` | Значение |
|---------|----------|
| `in_flight` | запрос отправлен, ответа ещё нет (или процесс упал во время вызова) |
| `ok` | поставщик вернул код |
| `failed` | поставщик точно НЕ выдавал код: соединение отвергнуто либо явная ошибка |
| `unknown` | таймаут чтения. Код мог быть выдан. Не отказ |

Разница между `failed` и `unknown` - это разница между «безопасно переключиться
на резервного» и «нельзя».

`kind` у расхождения:

| `kind` | Что случилось |
|--------|---------------|
| `duplicate_code` | код уже принадлежит другому запросу к поставщику |
| `foreign_code` | код относится к другому SKU |
| `unknown_code` | поставщик не признаёт код, который сам же прислал |
| `code_collision` | код уже выдан другой позиции, поймал `issuances.code` UNIQUE |
| `phantom_error` | ответил ошибкой, а код на самом деле выдал |

`resolved_at` заполняется, когда позиция закрыта кодом (`reissued`,
`code recovered by status probe`) или деньгами (`item refunded`).

### `POST /admin/orders/{order_id}/redeliver` - добить заказ руками

Ставит в очередь ту же самую задачу выдачи, что и обычный путь. Дёргать можно
сколько угодно раз: если задача уже висит или заказ уже выдан, ответ будет
`{"enqueued": false}` и ничего не произойдёт.

```json
{ "order_id": "ord_xxx", "enqueued": true }
```

Ошибки: `404` - нет заказа; `409` - заказ не оплачен.

### `POST /admin/orders/{order_id}/refund` - возврат

Возвращает деньги за всё невыданное в заказе: по позиции проводка
`refund` +сумма, `customer` -сумма, статус позиции `refunded`, статус заказа
пересчитывается. Выданные позиции остаются у покупателя.

Это ручная кнопка для того же кода, который в фоне отрабатывает сам. Повтор
ничего не задваивает: позиция уже `refunded`, а `UNIQUE(order_id, item_id, kind,
account)` не даст провести вторую проводку.

```json
{
  "order_id": "ord_xxx",
  "refunded": true,
  "items_refunded": ["ord_xxx.2"],
  "order_status": "partially_delivered"
}
```

Ошибки: `404`; `409` - заказ не был оплачен или уже выдан целиком (выданное
возвращаем руками, автоматом такое делать нельзя).

### `GET /admin/ledger/balance` - баланс журнала

Тот же блок `ledger`, что и в сверке, отдельной ручкой - чтобы вешать на алерт.

### `GET /admin/discrepancies` - расхождения с поставщиками

Параметр `limit`. Сводка по видам, открытые расхождения и отдельно те, что
требуют внимания.

```json
{
  "by_kind": [ { "kind": "duplicate_code", "total": 1, "open": 0 } ],
  "open":            { "count": 0, "items": [] },
  "needs_attention": { "count": 0, "items": [] }
}
```

Открытым расхождение остаётся, только пока позиция не закрыта кодом или
деньгами - разбор идёт сам. `needs_attention` - открытые расхождения по уже
закрытым позициям, в норме там пусто.

### `GET /admin/queue` - прогресс очереди и лимиты

```json
{
  "queue": {
    "waiting": 17,
    "by_kind": [
      { "kind": "deliver_order", "state": "running", "priority": 0, "n": 17,
        "oldest_seconds": 0.1 }
    ]
  },
  "paid_items": { "total": 29, "delivered": 11, "refunded": 1, "in_progress": 17 },
  "suppliers": [
    { "supplier": "a", "tokens_available": 1.0, "capacity": 1.0,
      "rate_per_min": 540.0, "taken_total": 13, "throttled_total": 105 },
    { "supplier": "b", "tokens_available": 1.0, "capacity": 1.0,
      "rate_per_min": 540.0, "taken_total": 3, "throttled_total": 0 }
  ]
}
```

`paid_items` считает позиции оплаченных заказов: сколько всего, сколько выдано,
сколько закрыто возвратом и сколько ещё в работе. `throttled_total` - сколько раз
задача уходила ждать своего окна вместо запроса к поставщику; это не ошибки.

### `GET /admin/jobs` - очередь

Параметры: `state` (`pending` | `running` | `done` | `failed`), `limit`.

```json
{ "items": [
  { "id": 36, "kind": "deliver_order", "dedupe_key": "ord_9af79b6850f942fd",
    "state": "done", "priority": 0, "attempts": 1, "max_attempts": 25,
    "run_at": "2026-09-08T10:25:32.659237Z", "last_error": null }
] }
```

Виды задач и приоритеты (меньше - раньше):

| `kind` | `priority` | Что делает |
|--------|-----------|------------|
| `deliver_order` | 0 | выдача позиций оплаченного заказа |
| `refund_item` | 10 | возврат за невыданную позицию |
| `apply_orphan_events` | 20 | досылка вебхуков, пришедших раньше заказа |
| `sync_stock` | 50 | обновление витринных остатков |

### `GET /admin/stats` - агрегаты

Заказы по статусам, события по результатам обработки, попытки по поставщикам и
исходам, общее число выдач.

---

## Админ: деньги и история

### `GET /admin/money` - инвариант одной ручкой

```json
{
  "paid_minor": 1978000,
  "delivered_minor": 1629000,
  "refunded_minor": 349000,
  "open_minor": 0,
  "equation": "1978000 = 1629000 + 349000 + 0",
  "balanced": true,
  "unsettled_finished_orders": { "count": 0, "items": [] }
}
```

`open_minor` - деньги, за которые мы ещё не отчитались ни кодом, ни возвратом,
то есть заказы в работе. `unsettled_finished_orders` - завершённые заказы, у
которых обязательство перед клиентом не закрылось в ноль; каждая такая строка
означает реальную дыру.

### `GET /admin/orders/{order_id}/as-of` - состояние на прошлый момент

Параметр `at` - момент в ISO-8601 (без зоны считается UTC).

Состояние собирается свёрткой append-only истории, а не чтением текущих строк:
`orders` и `order_items` переписывает каждый шаг выдачи, а история переписыванию
не подлежит.

```json
{
  "as_of": "2026-09-08T10:22:35+00:00",
  "order": {
    "id": "ord_1838952e8abb4d34",
    "status": "created",
    "amount_minor": 578000,
    "currency": "RUB",
    "items": [
      { "id": "ord_1838952e8abb4d34.1", "sku": "KEY-CS2-PRIME",
        "amount_minor": 129000, "status": "pending" }
    ]
  },
  "money": { "paid_minor": 0, "delivered_minor": 0, "refunded_minor": 0,
             "open_minor": 0, "equation": "0 = 0 + 0 + 0", "balanced": true },
  "events_applied": 1,
  "final": false
}
```

Секундой позже тот же заказ выглядит так:

```json
{
  "order": { "status": "partially_delivered", "items": [
    { "sku": "KEY-CS2-PRIME", "status": "delivered",
      "code": "EXTA-KEY-CS2-PRIME-00000", "supplier": "a" },
    { "sku": "KEY-EFT",       "status": "refunded" },
    { "sku": "GIFT-PSN-1000", "status": "delivered",
      "code": "EXTA-GIFT-PSN-1000-00000", "supplier": "a" } ] },
  "money": { "equation": "578000 = 229000 + 349000 + 0", "balanced": true },
  "events_applied": 16,
  "final": true
}
```

Ошибки: `404` - на этот момент заказа ещё не существовало (или его нет вовсе).

### `GET /admin/money/as-of` - деньги на прошлый момент

Параметр `at`. Тот же денежный блок, но по всей базе на указанный момент.
Инвариант выполняется на любом срезе, а не только на текущем.

### `GET /admin/reports/period` - итоги за период

Параметры `from` и `to` (ISO-8601). `to` должен быть позже `from`, иначе `422`.

```json
{
  "from": "2026-09-08T10:22:35.981000+00:00",
  "to":   "2026-09-08T10:25:52+00:00",
  "money": {
    "paid_minor": 1978000,
    "delivered_minor": 1629000,
    "refunded_minor": 349000,
    "cost_minor": 1140300,
    "open_delta_minor": 0,
    "equation": "1978000 = 1629000 + 349000 + 0",
    "balanced": true
  },
  "orders_touched": 27,
  "events": [
    { "type": "item_refunded", "n": 1 },
    { "type": "item_status",   "n": 301 },
    { "type": "order_created", "n": 26 },
    { "type": "order_paid",    "n": 27 },
    { "type": "order_status",  "n": 27 }
  ]
}
```

`open_delta_minor` - прирост незакрытых обязательств за период: заказы,
оплаченные внутри окна, но выданные или возвращённые уже за его границей.
Итог за период равен разности двух срезов `/admin/money/as-of` - одно и то же
число, посчитанное двумя независимыми способами.

---

## Служебное

`GET /health` -> `{"status": "ok"}`. Никаких проверок зависимостей внутри: это
liveness, а не readiness.

`GET /docs`, `GET /redoc`, `GET /openapi.json` - Swagger, ReDoc и сама схема.

---

## Статусы заказа и позиции

Заказ - агрегат над позициями, его статус считается из их статусов.

```
позиция:  pending ─> delivering ─> delivered
                          │
                          ├─> out_of_stock ────┐  (завоз)
                          ├─> delivery_failed ─┤  (поставщик ожил)  ─> delivered
                          └─> unavailable ─────┴─> refunded

заказ:    created ─> paid ─> delivering ─> delivered            (выдали всё)
             │                          └─> partially_delivered (часть вернули)
             │                          └─> refunded            (не выдали ничего)
             └─> payment_failed
```

| Статус заказа | Смысл | Финальный? |
|---------------|-------|-----------|
| `created` | заказ создан, ждём оплату | нет |
| `paid` | оплата подтверждена, выдача поставлена в очередь | нет |
| `delivering` | идёт получение кодов у поставщиков | нет |
| `delivered` | выданы все позиции | **да** |
| `partially_delivered` | часть выдана, за остальное деньги возвращены | **да** |
| `refunded` | не выдано ничего, деньги возвращены целиком | **да** |
| `payment_failed` | оплата не прошла | **да** |
| `out_of_stock` | оплачено, но кодов нет в наличии | нет, восстановимый |
| `delivery_failed` | выдать не удалось | нет, восстановимый |

| Статус позиции | Смысл |
|----------------|-------|
| `pending` | ждёт оплаты или своей очереди на выдачу |
| `delivering` | идёт запрос к поставщику |
| `delivered` | код у покупателя |
| `out_of_stock` | ни у одного поставщика нет кода, но завезут |
| `delivery_failed` | выдать не удалось, повтор имеет смысл |
| `unavailable` | выдать нельзя в принципе, ждать нечего |
| `refunded` | деньги за позицию возвращены |

Позиция закрывается ровно одним способом: кодом либо деньгами. Отсюда и
сходимость `оплачено = выдано + возвращено`.

Из финальных статусов система не выходит: повторная оплата уже выданного заказа
no-op, повторная выдача тоже. Восстановимые статусы не требуют ручного
вмешательства: фоновая задача продолжает попытки с бэкоффом, а когда бюджет
попыток исчерпан, невыданное закрывается возвратом и заказ приходит к финалу сам.

Отдельно стоит `delivering` с `last_error = "unresolved supplier outcome: ..."`.
Это не ошибка, а честное «мы не знаем, выдал поставщик код или нет». Пока не
выясним - не переключаемся на резервного и не выдаём второй код.

---

## API заглушек-поставщиков

Отдельные сервисы: A на `:9101`, B на `:9102`. У каждого свои пути (`/a/...`,
`/b/...`) и свой пул кодов. Контракт `POST /issue` - из задания, остальное это
управление хаосом для тестов.

### `POST /{s}/issue` - выдать код

```json
{ "request_id": "req_00123-1", "sku": "STEAM-TOPUP-500", "order_id": "ord_00123" }
```

`200`:

```json
{ "status": "ok", "request_id": "req_00123-1", "code": "LFXC-TNCS-BPCD" }
```

Ошибка (`409` для `out_of_stock`, `500`/`503` для сбоев):

```json
{ "detail": { "status": "error", "reason": "out_of_stock" } }
```

**Ключевое поведение:** повтор с тем же `request_id` возвращает тот же самый код,
а не выдаёт новый. Заголовок `X-Replayed: 1` подсказывает, что это был повтор.
Именно на этом держится безопасность ретраев после таймаута.

### `GET /{s}/issue/{request_id}` - выяснить исход

Тот самый статус-запрос, которым разрешается неопределённость после таймаута.

| Ответ | Значение для клиента |
|-------|----------------------|
| `200 {"status":"ok","code":"..."}` | код был выдан, забираем его |
| `404 {"status":"not_found"}` | такого запроса не было, код точно не выдавался - можно идти к другому поставщику |
| таймаут / `504` | исход по-прежнему неизвестен, ждём и повторяем |

### `GET /{s}/codes/{code}` - кому принадлежит код

Единственный внешний способ проверить ответ поставщика: сам ответ может быть
каким угодно, а пул ключей врать не умеет. По этой ручке ядро отвергает чужие
коды до того, как они уйдут покупателю.

```json
{ "code": "EXTA-STEAM-TOPUP-1000-00000", "sku": "STEAM-TOPUP-1000",
  "state": "issued", "request_id": "req_ord_3df5e7ec011745da.1-a" }
```

`404`, если поставщик такого кода не знает. Для ядра это тоже расхождение
(`unknown_code`): код, который поставщик прислал, но не признаёт, клиенту не
уходит.

### `GET /{s}/_stats` - счётчики лимита

```json
{ "supplier": "a", "rate_limit": 20, "rate_window_seconds": 1.0,
  "accepted": 24, "rejected_429": 0, "over_limit": 0, "max_in_window": 5 }
```

Считает сама заглушка, а не клиент: «лимит не превышен» должно быть словом
поставщика. `POST /{s}/_stats/reset` обнуляет.

### `GET /{s}/stock` - остатки

```json
{ "supplier": "a", "stock": { "KEY-CS2-PRIME": 8, "KEY-EFT": 6 } }
```

Отсюда фоновая задача `sync_stock` наполняет витринный снимок.

### `POST /{s}/_control` - режим работы

Тело - частичный патч, все поля необязательные.

| Поле | По умолчанию | Что делает |
|------|--------------|-----------|
| `mode` | `random` | `ok`, `error_5xx`, `out_of_stock`, `timeout`, `timeout_after_issue`, `refuse`, `duplicate_code`, `foreign_code`, `error_after_issue`, `random` |
| `error_rate` | `0.0` | доля 5xx в режиме `random` |
| `timeout_rate` | `0.0` | доля таймаутов в режиме `random` |
| `timeout_after_issue_share` | `0.5` | какая часть таймаутов приходится на «код уже выдан» |
| `hang_seconds` | `10.0` | сколько висеть в режимах с таймаутом |
| `latency_ms` | `0` | искусственная задержка на каждый запрос |
| `out_of_stock_skus` | `[]` | какие SKU считать распроданными (лечится завозом) |
| `unsupported_skus` | `[]` | какие SKU не продаём вовсе: `410`, отказ навсегда |
| `probe_hangs` | `false` | статус-запрос тоже зависает: исход становится неразрешимым |
| `rate_limit` | `0` | сколько запросов на выдачу принимать за окно, `0` - без лимита |
| `rate_window_seconds` | `60.0` | длина скользящего окна лимита |

Режимы, ради которых всё это затевалось:

- `timeout_after_issue` - **ловушка таймаута**. Поставщик фиксирует выдачу в своей
  базе и только потом «зависает». Код существует, но клиент об этом не знает.
- `timeout` - зависание без выдачи. Отличить его от предыдущего снаружи нельзя -
  в этом и проблема; помогает только статус-запрос.
- `refuse` - мгновенный `503`. Поставщик ответил, значит точно ничего не выдал.
- `probe_hangs` + `timeout_after_issue` - худший случай: исход выяснить нечем.
  Единственное безопасное поведение - ждать и повторять тем же `request_id`.

Режимы недобросовестного поставщика, ответу в них верить нельзя:

- `duplicate_code` - отдаёт код, уже выданный другому запросу. Пул при этом не
  трогается: настоящий владелец кода остаётся прежним, иначе подлог нельзя было
  бы обнаружить снаружи.
- `foreign_code` - отдаёт код от другого товара.
- `error_after_issue` - записывает выдачу у себя и отвечает `500`. По ответу
  кажется, что выдачи не было; уход к резервному поставщику дал бы второй код.

Отдельно от `out_of_stock` стоит `unsupported_skus`: `410 sku_not_supported`.
Первое лечится завозом, второе не лечится ничем - на этой разнице ядро решает,
ждать дальше или возвращать деньги.

```bash
curl -sX POST localhost:9101/a/_control -H 'content-type: application/json' \
  -d '{"mode":"timeout_after_issue","hang_seconds":5}'

curl -sX POST localhost:9101/a/_control -H 'content-type: application/json' \
  -d '{"mode":"random","error_rate":0.4,"timeout_rate":0.3}'

curl -s localhost:9101/a/_control            # посмотреть текущие настройки
```

### `POST /{s}/_restock` - пополнить остаток

```json
{ "sku": "KEY-EFT", "count": 10, "prefix": "RSTK" }
```

Добавляет `count` свежих кодов. Так воспроизводится восстановление из
`out_of_stock` без перезапуска чего бы то ни было.

---

## Сквозной сценарий целиком

```bash
# 1. заказ из трёх товаров, один из которых поставщики не продают
curl -sX POST localhost:9101/a/_control -H 'content-type: application/json' \
  -d '{"unsupported_skus":["KEY-EFT"]}'
curl -sX POST localhost:9102/b/_control -H 'content-type: application/json' \
  -d '{"unsupported_skus":["KEY-EFT"]}'

ORDER=$(curl -sX POST localhost:8000/orders -H 'content-type: application/json' \
  -d '{"items":[{"sku":"KEY-CS2-PRIME"},{"sku":"KEY-EFT"},{"sku":"GIFT-PSN-1000"}]}' \
  | jq -r .id)

# 2. платёжка присылает вебхук (тут - эмулятор)
python -m scripts.payment_sim pay --order $ORDER

# 3. через пару секунд заказ закрыт: два кода выданы, за третий деньги вернулись
curl -s localhost:8000/orders/$ORDER \
  | jq '{status, items: [.items[] | {sku, status, code: .issuance.code}]}'
# "partially_delivered", два кода и одна позиция в "refunded"

# 4. смотрим, как именно это произошло
curl -s localhost:8000/admin/orders/$ORDER/timeline \
  | jq '{items, attempts: .supplier_attempts, discrepancies: .supplier_discrepancies}'

# 5. проверяем, что деньги сходятся
curl -s localhost:8000/admin/money | jq '{equation, balanced}'
# { "equation": "578000 = 229000 + 349000 + 0", "balanced": true }

# 6. и что было с заказом минуту назад
curl -s "localhost:8000/admin/orders/$ORDER/as-of?at=$(date -u -d '1 minute ago' +%Y-%m-%dT%H:%M:%SZ)" \
  | jq '{status: .order.status, money: .money.equation}'
```

Сценарии посложнее - гонки, ловушка таймаута, фолбэк, пустой остаток,
недобросовестный поставщик, всплеск под лимитом - собраны в `make race`,
`make trap`, `make fallback`, `make stock` и в тестах
(`make test-partial`, `make test-untrusted`, `make test-burst`,
`make test-history`). Подробности в README.
