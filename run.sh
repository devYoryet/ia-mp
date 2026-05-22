#!/usr/bin/env bash
# Atajos del Clasificador IA.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  test)   MODO=test python3 worker.py --once ;;        # una pasada de backtest
  worker) python3 worker.py ;;                          # loop continuo (según MODO de .env)
  api)    exec uvicorn api.main:app --host 0.0.0.0 --port 8800 ;;  # panel web
  *)
    echo "Uso: $0 {test|worker|api}"
    echo "  test    una pasada de backtest (compara IA vs personas)"
    echo "  worker  loop continuo del clasificador"
    echo "  api     panel web de revisión en el puerto 8800"
    exit 1 ;;
esac
