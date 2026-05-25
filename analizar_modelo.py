#!/usr/bin/env python3
"""Análisis del backtest acumulado, perspectiva ML.

Cinco lentes sobre `clasificador_ia_backtest`:
  1) Leakage — descripciones duplicadas que pueden inflar el accuracy reportado
  2) Per-class accuracy — ranking de pactivos con peor acierto (qué le cuesta al modelo)
  3) Reliability diagram — predict_proba ≥ X ¿realmente acierta X%?
  4) Top confusions — qué pactivos confunde con qué otros
  5) Drift por día — la accuracy se mantiene, sube o baja con más datos

Uso:  python3 analizar_modelo.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

from db import conectar


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
    """Barra ASCII de 0..1 → ████████░░░░"""
    n = int(round(p * ancho))
    return "█" * n + "░" * (ancho - n)


# 1) Leakage --------------------------------------------------------------
def leakage():
    print("\n" + "=" * 72)
    print("1) LEAKAGE — descripciones duplicadas en el backtest")
    print("=" * 72)
    total = q("SELECT COUNT(*) n FROM clasificador_ia_backtest")[0]["n"]
    if not total:
        print("  (sin datos)")
        return
    distintos = q("SELECT COUNT(DISTINCT descripcion) n FROM clasificador_ia_backtest")[0]["n"]
    dups = total - distintos
    print(f"  Total filas backtest : {total:,}")
    print(f"  Descripciones únicas : {distintos:,}")
    print(f"  Duplicados (no-únicas): {dups:,}  ({dups/total*100:.1f}%)")
    print()
    print("  Top descripciones repetidas:")
    for r in q(
        "SELECT LEFT(descripcion,90) d, COUNT(*) n FROM clasificador_ia_backtest "
        "GROUP BY descripcion HAVING n > 1 ORDER BY n DESC LIMIT 8"
    ):
        print(f"    ×{r['n']:<4} {r['d']}")
    print()
    print("  Lectura: cada vez que una glosa idéntica aparece, contribuye al accuracy")
    print("  por separado. Si la cascada la resuelve siempre igual, eso INFLA el número.")
    print("  Las clases con mayor duplicación son las más sobre-representadas.")


# 2) Per-class accuracy ----------------------------------------------------
def per_class():
    print("\n" + "=" * 72)
    print("2) PER-CLASS — pactivos con peor accuracy (mínimo 5 muestras)")
    print("=" * 72)
    print("  Cuando ambos (humano + IA) dijeron interés, ¿coincide el pactivo?")
    print()
    # Mejor / peor. Quitamos NULL (cuando no aplica coincide_pactivo).
    peor = q(
        "SELECT ia_pactivo p, COUNT(*) n, SUM(coincide_pactivo) ok, "
        "ROUND(SUM(coincide_pactivo)/COUNT(*)*100,1) acc "
        "FROM clasificador_ia_backtest "
        "WHERE coincide_pactivo IS NOT NULL "
        "GROUP BY ia_pactivo HAVING n >= 5 "
        "ORDER BY acc ASC, n DESC LIMIT 15"
    )
    print("  PEORES — el modelo dice INTERÉS con ese pactivo pero el humano discrepa seguido")
    print(f"  {'pactivo':<40} {'n':>5}  {'acierto':<10}  {'barra'}")
    print(f"  {'-'*40} {'-'*5}  {'-'*10}  {'-'*24}")
    for r in peor:
        p = (r["ok"] or 0) / r["n"]
        print(f"  {(r['p'] or '(NULL)')[:40]:<40} {r['n']:>5}  {r['acc']:>5}%      {bar(p)}")


# 3) Reliability diagram ---------------------------------------------------
def reliability():
    print("\n" + "=" * 72)
    print("3) RELIABILITY — ¿la confianza del modelo es honesta?")
    print("=" * 72)
    print("  Si una predicción tiene proba=0.7, debería acertar ~70% de las veces.")
    print()
    # Buckets de 0.05 → más resolución
    rows = q(
        "SELECT FLOOR(ia_confianza*20)/20 AS bucket, "
        "COUNT(*) n, "
        "AVG(coincide_interes)*100 acc_int, "
        "AVG(coincide_pactivo)*100 acc_pact "
        "FROM clasificador_ia_backtest "
        "WHERE ia_metodo='modelo_pactivo' "
        "GROUP BY bucket ORDER BY bucket"
    )
    if not rows:
        print("  Aún no hay filas resueltas por modelo_pactivo.")
        return
    print("  modelo_pactivo (etapa nueva):")
    print(f"  {'bin':<10} {'n':>5}  {'acc int':>8}  {'acc pact':>9}")
    print(f"  {'-'*10} {'-'*5}  {'-'*8}  {'-'*9}")
    for r in rows:
        ai = f"{r['acc_int']:.0f}%" if r['acc_int'] is not None else "—"
        ap = f"{r['acc_pact']:.0f}%" if r['acc_pact'] is not None else "—"
        b = float(r['bucket'])
        print(f"  {b:.2f}-{b+0.05:.2f}  {r['n']:>5}  {ai:>8}  {ap:>9}")
    print()
    print("  Lectura: si la columna 'acc pact' sigue (más o menos) la magnitud del 'bin',")
    print("  la confianza es honesta. Si los acc son SIEMPRE altos aunque la proba sea baja,")
    print("  el modelo está SUBSTIMANDO su certeza → podríamos bajar el umbral.")


# 4) Top confusions --------------------------------------------------------
def confusiones():
    print("\n" + "=" * 72)
    print("4) CONFUSIONES — cuando se equivoca, ¿con qué pactivo lo confunde?")
    print("=" * 72)
    rows = q(
        "SELECT humano_pactivo h, ia_pactivo i, COUNT(*) n "
        "FROM clasificador_ia_backtest "
        "WHERE coincide_pactivo=0 AND humano_pactivo IS NOT NULL "
        "AND ia_pactivo IS NOT NULL "
        "GROUP BY h, i HAVING n >= 3 ORDER BY n DESC LIMIT 20"
    )
    if not rows:
        print("  Sin confusiones con soporte ≥3.")
        return
    print(f"  {'humano dijo':<30} {'IA dijo':<30} {'×':>5}")
    print(f"  {'-'*30} {'-'*30} {'-'*5}")
    for r in rows:
        print(f"  {(r['h'] or '?')[:30]:<30} {(r['i'] or '?')[:30]:<30} {r['n']:>5}")
    print()
    print("  Lectura: pares que aparecen mucho son CANDIDATOS A UNIFICAR en el catálogo,")
    print("  o señales de que la IA confunde productos parecidos sistemáticamente.")


# 5) Drift por día ---------------------------------------------------------
def drift():
    print("\n" + "=" * 72)
    print("5) DRIFT — accuracy por día (¿se mantiene con más datos?)")
    print("=" * 72)
    rows = q(
        "SELECT DATE(creado_en) d, COUNT(*) n, "
        "ROUND(AVG(coincide_interes)*100,1) acc_int, "
        "ROUND(SUM(coincide_pactivo)/COUNT(coincide_pactivo)*100,1) acc_pact, "
        "ROUND(SUM(costo_usd),2) costo "
        "FROM clasificador_ia_backtest "
        "WHERE creado_en >= NOW() - INTERVAL 14 DAY "
        "GROUP BY DATE(creado_en) ORDER BY d DESC LIMIT 14"
    )
    if not rows:
        print("  (sin datos)")
        return
    print(f"  {'fecha':<12} {'n':>6}  {'int':>6}  {'pact':>6}  {'costo':>8}")
    print(f"  {'-'*12} {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}")
    for r in rows:
        ai = f"{r['acc_int']}%" if r['acc_int'] is not None else "—"
        ap = f"{r['acc_pact']}%" if r['acc_pact'] is not None else "—"
        print(f"  {str(r['d']):<12} {r['n']:>6,}  {ai:>6}  {ap:>6}  ${float(r['costo']):>7.2f}")


def main():
    print(f"Análisis del modelo · {datetime.now():%Y-%m-%d %H:%M}")
    leakage()
    per_class()
    reliability()
    confusiones()
    drift()
    print()


if __name__ == "__main__":
    main()
