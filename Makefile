PY ?= .venv/bin/python
API ?= http://127.0.0.1:8000

.PHONY: help venv seed up down logs test race trap fallback stock catalog explain clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## создать окружение и поставить зависимости
	python3 -m venv .venv && $(PY) -m pip install -q -U pip && $(PY) -m pip install -q -r requirements.txt

seed: ## создать схемы и залить каталог + пул ключей
	$(PY) -m scripts.seed --reset

up: ## поднять локально: поставщик A, поставщик B, API с воркером
	./scripts/run_stack.sh

down: ## остановить локальный стек
	-@for f in .run/*.pid; do kill $$(cat $$f) 2>/dev/null || true; done; rm -rf .run

test: ## прогнать все тесты (поднимает свой стек на тестовых портах и БД)
	$(PY) -m pytest tests/

test-race: ## только этап 2 - гонки и идемпотентность
	$(PY) -m pytest tests/test_stage2_race.py

test-suppliers: ## только этап 3 - таймауты и fallback
	$(PY) -m pytest tests/test_stage3_suppliers.py

race: ## живая проверка: заказ + 50 параллельных вебхуков
	$(PY) -m scripts.payment_sim --api $(API) scenario --sku KEY-CS2-PRIME --concurrency 50

race-same-event: ## живая проверка: 50 вебхуков с ОДНИМ event_id
	$(PY) -m scripts.payment_sim --api $(API) scenario --sku KEY-GTA5 --concurrency 50 --same-event

trap: ## живая проверка: ловушка таймаута (A выдаёт код и зависает)
	./scripts/demo_trap.sh

fallback: ## живая проверка: A недоступен -> fallback на B
	./scripts/demo_fallback.sh

stock: ## живая проверка: пустой остаток и восстановление
	./scripts/demo_out_of_stock.sh

catalog: ## этап 5: сгенерировать 50k SKU и замерить витрину
	$(PY) -m scripts.load_catalog --count 50000

explain: ## этап 5: только план и замеры на текущем каталоге
	$(PY) -m scripts.load_catalog --explain

reconcile: ## отчёт сверки
	@curl -s $(API)/admin/reconciliation | $(PY) -m json.tool

clean: down ## удалить тестовые БД и временные файлы
	-dropdb --if-exists gamestore_core_test
	-dropdb --if-exists gamestore_suppliers_test
	rm -rf .run .pytest_cache
