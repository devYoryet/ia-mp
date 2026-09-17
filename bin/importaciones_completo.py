#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Módulo legacy "Importaciones": carga completa del Excel mensual.

    1. ImportOC.py: Excel -> licitaciones_diarias_total_farma.importaciones_YYYY_MM (tabla de paso)
    2. cargas_legacy.publicar_importaciones: tabla del mes, farma y `fecha`, en clásico y prime

Antes, el panel corría solo el paso 1 (julio 2026 se completó a mano) y le pasaba
un --fecha errado: tomaba el "1" de "v1" como mes (2026-01-01). El mes sale del
nombre del archivo (mes en español + año); sin eso no se carga.

Re-subir el mismo mes reemplaza la tabla de paso (antes se duplicaban filas) y
vuelve a publicar.

Uso: python importaciones_completo.py "<ruta>/Importaciones Agosto 2026 v1.xlsm"
Tokens de fin para el panel: FINALIZADO EXITOSAMENTE / ERROR CRÍTICO
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(AQUI)
sys.path.insert(0, REPO)

import cargas_legacy as cl  # noqa: E402
from cierre_adj import bd  # noqa: E402


def main() -> int:
    archivos = [a for a in sys.argv[1:] if a.lower().endswith((".xlsm", ".xlsx", ".xls"))]
    if not archivos:
        print("ERROR CRÍTICO: no se recibió la ruta del Excel.")
        return 1
    archivo = archivos[0]
    periodo = cl.periodo_desde_nombre(archivo)
    if not periodo:
        print(f"ERROR CRÍTICO: el nombre '{os.path.basename(archivo)}' no trae mes en español y año "
              "(ej. 'Importaciones Agosto 2026 v1.xlsm'). Renombrar y volver a subir.")
        return 1
    print(f"Periodo detectado en el nombre del archivo: {periodo:%Y-%m} ({cl.MESES[periodo.month - 1]} {periodo.year})")

    paso = f"importaciones_{periodo:%Y_%m}"
    cn = bd.clasico()
    try:
        if cl._existe(cn, cl.DB_PASO, paso):
            n = cl._contar(cn, f"SELECT COUNT(*) FROM `{cl.DB_PASO}`.`{paso}`")
            with cn.cursor() as cur:
                cur.execute(f"DROP TABLE `{cl.DB_PASO}`.`{paso}`")
            cn.commit()
            print(f"Tabla de paso {cl.DB_PASO}.{paso} ya existía ({n:,} filas): se reemplaza para no duplicar.")
    finally:
        cn.close()

    print("== Paso 1/2: ImportOC.py (Excel -> tabla de paso)", flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(AQUI, "ImportOC.py"), "--archivo", archivo],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=AQUI,
    )
    leidas = insertadas = None
    for linea in proc.stdout:  # type: ignore[union-attr]
        # "FIN" de ImportOC no es el fin del módulo: se muestra como "FIN paso 1".
        linea = re.sub(r"\bFIN\b", "FIN paso 1", linea.rstrip("\n"))
        print(linea, flush=True)
        m = re.search(r"Filas leídas\s*:\s*([\d,.]+)", linea)
        if m:
            leidas = int(re.sub(r"[^\d]", "", m.group(1)))
        m = re.search(r"Filas insertadas\s*:\s*([\d,.]+)", linea)
        if m:
            insertadas = int(re.sub(r"[^\d]", "", m.group(1)))
    rc = proc.wait()
    if rc != 0 or not leidas or leidas != insertadas:
        print(f"ERROR CRÍTICO: ImportOC.py terminó con código {rc} (filas leídas {leidas}, insertadas {insertadas}). "
              "No se publica.")
        return 1

    cn = bd.clasico()
    try:
        en_paso = cl._contar(cn, f"SELECT COUNT(*) FROM `{cl.DB_PASO}`.`{paso}`")
    finally:
        cn.close()
    if en_paso != leidas:
        print(f"ERROR CRÍTICO: la tabla de paso tiene {en_paso:,} filas y el Excel {leidas:,}. No se publica.")
        return 1
    print(f"Paso 1 OK: {leidas:,} filas del Excel en {cl.DB_PASO}.{paso}", flush=True)

    print("== Paso 2/2: publicación en clásico y prime", flush=True)
    try:
        r = cl.publicar_importaciones(periodo, log=lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR CRÍTICO: {type(exc).__name__}: {exc}")
        return 1
    print(f"Importaciones {cl.MESES[periodo.month - 1]} {periodo.year}: {r['mes']:,} filas y {r['farma']:,} farma "
          "en clásico y prime. FINALIZADO EXITOSAMENTE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
