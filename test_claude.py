#!/usr/bin/env python3
"""Prueba directa del clasificador con Claude — fuerza llamadas a la API
para verificar el camino completo (messages.parse, modelo, costo)."""

from __future__ import annotations

import clasificador_claude as cc
from taxonomia import cargar_taxonomia

CASOS = [
    (
        "Servicio de capacitación en manejo de drones para funcionarios",
        "Capacitación",
        "ADQUISICION DE SERVICIO DE CAPACITACION PILOTO DE DRONES",
    ),
    (
        "Solución fisiológica de cloruro de sodio 0,9% matraz 1000 ml estéril",
        "Insumo clínico",
        "Medicamentos para programas - Prod: suero fisiologico 0,9%",
    ),
]


def main() -> None:
    tax = cargar_taxonomia()
    print(f"Taxonomía: {len(tax.pactivos)} pactivos\n")
    for desc, tit, vinc in CASOS:
        c, uso = cc.clasificar(desc, tit, vinc, tax)
        print(f"IN : {desc[:64]}")
        print(f"OUT: interes={c.interes} pactivo={c.pactivo} "
              f"composicion={c.composicion} presentacion={c.presentacion} "
              f"confianza={c.confianza}")
        print(f"     razon: {c.razon}")
        print(f"     fuera_de_lista={c.pactivo_fuera_de_lista} "
              f"propuesto={c.pactivo_propuesto}")
        print(f"     tokens: in={uso.tokens_in} out={uso.tokens_out} "
              f"cache_write={uso.cache_write} cache_read={uso.cache_read}")
        print(f"     COSTO: ${uso.costo_usd:.5f}\n")


if __name__ == "__main__":
    main()
