#!/usr/bin/env python3
"""Análisis profundo IA vs humano en PRODUCCIÓN — qué patrones se ven, qué reglas
darían ROI (eliminan FP sin crear FN) y qué hay que pasar a negocio.

A diferencia de `analizar_modelo.py` (que mira el backtest contra el histórico),
este mira la PRODUCCIÓN VIVA: las filas que el bot pre-clasificó en los últimos
días Y un humano REAL del equipo (Fernando, Evelyn, Benjamín, etc.) acaba de
revisar vía la app legacy `gestor_licitaciones` en clasico.

Output:
  - Métricas globales y cross matrix
  - Top FP por pactivo IA (qué nos genera ruido)
  - Top FN por método IA (qué se pierde y dónde)
  - "ROI de auto-reglas": para cada pactivo problemático, ganancia neta si se
    convirtiera en regla "ese pactivo → descarte automático"
  - Acierto por revisor humano (¿el equipo es consistente?)
  - Por método IA y por confianza
"""

from __future__ import annotations

import sys
from datetime import datetime

from db import conectar

DIAS = 3   # ventana de análisis (últimos N días)


def q(sql: str, args=()) -> list:
    c = conectar()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    finally:
        c.close()


def pct(ok, t):
    return f"{ok/t*100:.1f}%" if t else "—"


def bar(p, ancho=24):
    n = int(round(p * ancho))
    return "█" * n + "░" * (ancho - n)


def head(t):
    print("\n" + "=" * 78); print(t); print("=" * 78)


# Universo: filas que el bot pre-clasificó (clasificador_ia_log) Y un humano
# real (no Bot/IA_/Bot Eliminado) decidió en la tabla origen.
def universo_sql(tabla: str) -> str:
    return f"""
SELECT
  log.id, log.tabla_origen, log.fila_id, log.descripcion,
  log.interes_sugerido AS ia_int, log.pactivo_sugerido AS ia_pact,
  log.metodo AS ia_metodo, log.confianza AS ia_conf,
  t.estado_gestor AS h_int, t.pactivo AS h_pact, t.nombre_clasificador AS revisor,
  t.fecha_clasificacion AS h_ts
FROM clasificador_ia_log log
JOIN `{tabla}` t ON t.id = log.fila_id
WHERE log.tabla_origen = '{tabla}'
  AND t.nombre_clasificador IS NOT NULL
  AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
  AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
"""


def main():
    print(f"Análisis profundo PRODUCCIÓN · ventana últimos {DIAS} días · {datetime.now():%Y-%m-%d %H:%M}")

    # ---------------- A) Universo y cross matrix ----------------
    head("1) UNIVERSO de comparación IA vs humano (últimos N días)")
    for tabla in ("compra_agil", "Licitaciones_diarias"):
        sql = (
            "SELECT COUNT(*) n, "
            "SUM(log.interes_sugerido = t.estado_gestor) acuerdo_int, "
            "SUM(log.interes_sugerido = 1 AND t.estado_gestor = 1 "
            "    AND log.pactivo_sugerido = t.pactivo) acuerdo_pact_estricto, "
            "SUM(log.interes_sugerido = 1 AND t.estado_gestor = 1) ambos_int, "
            "SUM(log.interes_sugerido = 0 AND t.estado_gestor = 1) fn, "
            "SUM(log.interes_sugerido = 1 AND t.estado_gestor = 0) fp "
            f"FROM clasificador_ia_log log JOIN `{tabla}` t ON t.id = log.fila_id "
            f"WHERE log.tabla_origen = '{tabla}' "
            "AND t.nombre_clasificador IS NOT NULL "
            "AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)' "
            f"AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY"
        )
        r = q(sql)[0]
        n = r["n"] or 0
        if not n:
            print(f"  {tabla:<24} sin filas comparables")
            continue
        print(f"\n  {tabla}: {n:,} filas comparables")
        print(f"    Acierto INTERÉS : {(r['acuerdo_int'] or 0):>5,} / {n:<7,} = {pct(r['acuerdo_int'], n)}")
        if r['ambos_int']:
            print(f"    Acierto PACTIVO : {(r['acuerdo_pact_estricto'] or 0):>5,} / {r['ambos_int']:<7,} = {pct(r['acuerdo_pact_estricto'], r['ambos_int'])} (estricto)")
        print(f"    FALSOS POSITIVOS: {r['fp'] or 0:>5,}   (IA dijo interés, humano descartó)")
        print(f"    FALSOS NEGATIVOS: {r['fn'] or 0:>5,}   (IA descartó, humano lo quería) ← caro")

    # ---------------- B) Top FP por pactivo IA: ranking del ruido ----------------
    head("2) TOP FP por pactivo que la IA SUGIRIÓ — el ruido que hay que filtrar")
    print("  Pactivo IA donde el humano descartó más (potenciales reglas auto):\n")
    rows = q(f"""
        SELECT log.pactivo_sugerido p,
               COUNT(*) n,
               SUM(t.estado_gestor = 0) fp,
               SUM(t.estado_gestor = 1) tp,
               ROUND(SUM(t.estado_gestor = 0)/COUNT(*)*100, 1) pct_fp
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND log.interes_sugerido = 1
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY log.pactivo_sugerido
        HAVING n >= 5
        ORDER BY fp DESC LIMIT 30
    """)
    print(f"    {'pactivo IA':<36}  {'n':>5}  {'FP':>5}  {'TP':>5}  {'%FP':>5}   {'barra FP'}")
    print(f"    {'-'*36}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*5}   {'-'*24}")
    for r in rows[:20]:
        p = r["fp"] / r["n"] if r["n"] else 0
        print(f"    {(r['p'] or '(NULL)')[:36]:<36}  {r['n']:>5}  {r['fp'] or 0:>5}  "
              f"{r['tp'] or 0:>5}  {(r['pct_fp'] or 0):>4}%   {bar(p)}")

    # ---------------- C) ROI de auto-reglas ----------------
    head("3) ROI de AUTO-REGLAS — \"pactivo X → descarte automático\"")
    print("  Para cada pactivo problemático, ¿cuántos FP eliminamos? ¿cuántos FN creamos?")
    print("  Recomendable si FP_eliminados muy alto y FN_creados muy bajo o cero.\n")
    candidatos = q(f"""
        SELECT log.pactivo_sugerido p,
               COUNT(*) n,
               SUM(t.estado_gestor = 0) fp_eliminados,
               SUM(t.estado_gestor = 1) fn_creados
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND log.interes_sugerido = 1
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY log.pactivo_sugerido
        HAVING n >= 10 AND fp_eliminados >= 0.8 * n
        ORDER BY fp_eliminados DESC LIMIT 15
    """)
    print(f"    {'pactivo IA':<32}  {'n':>5}  {'FP elim':>8}  {'FN nuevos':>10}  {'ganancia neta':>13}")
    print(f"    {'-'*32}  {'-'*5}  {'-'*8}  {'-'*10}  {'-'*13}")
    ganancia_total = 0
    for r in candidatos:
        fp = r["fp_eliminados"] or 0
        fn = r["fn_creados"] or 0
        neto = fp - fn * 50    # 1 FN cuesta como 50 FP (perder venta vs revisar)
        ganancia_total += max(neto, 0)
        rec = "✓ AUTO" if fn == 0 else ("✓ AUTO con guardia" if fn <= 1 else "⚠ revisar")
        print(f"    {(r['p'] or '?')[:32]:<32}  {r['n']:>5}  {fp:>8}  {fn:>10}  {neto:>10}  {rec}")
    print(f"\n  → Si aprobás todas estas reglas, eliminás ~{ganancia_total} revisiones "
          f"humanas en {DIAS} días.")

    # ---------------- D) Top FN por método IA: dónde se pierde ----------------
    head("4) FALSOS NEGATIVOS — IA descartó algo que el humano quería")
    print("  Por etapa de la cascada (qué módulo está descartando ventas):\n")
    rows = q(f"""
        SELECT log.metodo,
               COUNT(*) n_descartes,
               SUM(t.estado_gestor = 1) fn,
               ROUND(SUM(t.estado_gestor=1)/COUNT(*)*100, 2) pct_fn
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND log.interes_sugerido = 0
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY log.metodo
        ORDER BY n_descartes DESC
    """)
    print(f"    {'método (descartó)':<28}  {'descartes':>9}  {'FN':>4}  {'%FN':>6}")
    print(f"    {'-'*28}  {'-'*9}  {'-'*4}  {'-'*6}")
    for r in rows:
        print(f"    {str(r['metodo']):<28}  {r['n_descartes']:>9,}  {r['fn'] or 0:>4}  {r['pct_fn'] or 0:>5}%")

    # Top humano_pactivo perdido (qué pactivos se nos escaparon)
    print("\n  Qué pactivos humanos se nos escaparon más (humano dijo X, IA descartó):\n")
    rows = q(f"""
        SELECT t.pactivo p, log.metodo, COUNT(*) n
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND log.interes_sugerido = 0 AND t.estado_gestor = 1
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY t.pactivo, log.metodo
        HAVING n >= 2 ORDER BY n DESC LIMIT 15
    """)
    if rows:
        print(f"    {'pactivo humano':<36}  {'método IA':<20}  {'n':>3}")
        print(f"    {'-'*36}  {'-'*20}  {'-'*3}")
        for r in rows:
            print(f"    {(r['p'] or '?')[:36]:<36}  {str(r['metodo']):<20}  {r['n']:>3}")
    else:
        print("    (todas las pérdidas son únicas — no hay patrón sistemático con >=2)")

    # ---------------- E) Acierto por revisor ----------------
    head("5) ACIERTO por REVISOR humano — ¿el equipo es consistente?")
    print("  Si dos revisores tienen acierto IA muy distinto, hay inconsistencia humana.\n")
    rows = q(f"""
        SELECT t.nombre_clasificador rev,
               COUNT(*) n,
               ROUND(SUM(log.interes_sugerido = t.estado_gestor)/COUNT(*)*100, 1) acc_int,
               SUM(log.interes_sugerido = 1 AND t.estado_gestor = 0) fp,
               SUM(log.interes_sugerido = 0 AND t.estado_gestor = 1) fn
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY t.nombre_clasificador HAVING n >= 50
        ORDER BY n DESC
    """)
    print(f"    {'revisor':<30}  {'filas':>6}  {'acc int':>8}  {'FP':>5}  {'FN':>3}")
    print(f"    {'-'*30}  {'-'*6}  {'-'*8}  {'-'*5}  {'-'*3}")
    for r in rows:
        print(f"    {(r['rev'] or '?')[:30]:<30}  {r['n']:>6,}  {r['acc_int']}%  "
              f"{r['fp'] or 0:>5}  {r['fn'] or 0:>3}")

    # ---------------- F) Acierto por método IA + por confianza ----------------
    head("6) ACIERTO por método IA + correlación con confianza")
    rows = q(f"""
        SELECT log.metodo,
               COUNT(*) n,
               ROUND(SUM(log.interes_sugerido = t.estado_gestor)/COUNT(*)*100, 1) acc_int,
               ROUND(AVG(log.confianza), 3) conf_avg
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY log.metodo ORDER BY n DESC
    """)
    print(f"    {'método':<28}  {'n':>6}  {'acc int':>8}  {'conf prom':>10}")
    print(f"    {'-'*28}  {'-'*6}  {'-'*8}  {'-'*10}")
    for r in rows:
        print(f"    {str(r['metodo']):<28}  {r['n']:>6,}  {r['acc_int']}%  {r['conf_avg']}")
    print("\n  Bandas de confianza para INTERÉS-sugeridas:")
    rows = q(f"""
        SELECT
            CASE
              WHEN log.confianza >= 0.9 THEN '0.90+'
              WHEN log.confianza >= 0.7 THEN '0.70-0.90'
              WHEN log.confianza >= 0.5 THEN '0.50-0.70'
              ELSE '< 0.50'
            END bin,
            COUNT(*) n,
            ROUND(SUM(t.estado_gestor=1)/COUNT(*)*100, 1) acc_int
        FROM clasificador_ia_log log
        JOIN compra_agil t ON t.id = log.fila_id
        WHERE log.tabla_origen = 'compra_agil'
          AND log.interes_sugerido = 1
          AND t.nombre_clasificador IS NOT NULL
          AND t.nombre_clasificador NOT REGEXP '^(Bot|BOT|IA_|Bot Eliminado)'
          AND t.fecha_clasificacion >= NOW() - INTERVAL {DIAS} DAY
        GROUP BY bin ORDER BY bin DESC
    """)
    print(f"    {'banda conf':<12}  {'n':>6}  {'% quedó interés':>16}")
    for r in rows:
        print(f"    {str(r['bin']):<12}  {r['n']:>6,}  {r['acc_int']}%")


if __name__ == "__main__":
    main()
