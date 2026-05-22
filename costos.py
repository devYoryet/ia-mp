"""Precios de la API de Anthropic y cálculo de costo por llamada (USD).

La API de Anthropic NO expone el saldo de la cuenta por endpoint; el saldo solo
se ve en console.anthropic.com. Por eso este sistema calcula el gasto él mismo,
a partir de los tokens que devuelve cada respuesta, y lo muestra en el panel."""

from __future__ import annotations

# USD por cada 1.000.000 de tokens (pricing público de Anthropic).
PRECIOS = {
    "claude-opus-4-7":   {"in": 5.00, "out": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00,  "cache_write": 1.25, "cache_read": 0.10},
}


def costo_usd(
    modelo: str,
    tokens_in: int,
    tokens_out: int,
    cache_write: int = 0,
    cache_read: int = 0,
) -> float:
    """Costo en USD de una llamada, según el modelo y los tokens usados."""
    p = PRECIOS.get(modelo) or PRECIOS["claude-haiku-4-5"]
    return (
        tokens_in * p["in"]
        + tokens_out * p["out"]
        + cache_write * p["cache_write"]
        + cache_read * p["cache_read"]
    ) / 1_000_000
