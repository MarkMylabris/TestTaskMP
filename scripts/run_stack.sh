#!/usr/bin/env bash
# Локальный запуск всего стека: два поставщика + API (со встроенным воркером).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
mkdir -p .run
$PY -m uvicorn suppliers.main:app --host 127.0.0.1 --port 9101 --log-level warning & echo $! > .run/sup_a.pid
$PY -m uvicorn suppliers.main:app --host 127.0.0.1 --port 9102 --log-level warning & echo $! > .run/sup_b.pid
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning & echo $! > .run/api.pid
echo "supplier A: http://127.0.0.1:9101/a   supplier B: http://127.0.0.1:9102/b   api: http://127.0.0.1:8000/docs"
wait
