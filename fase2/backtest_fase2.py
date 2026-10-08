"""Backtest de la Fase 2 — re-corre el barrido sobre N días SIN ESCRIBIR NADA.

Mide, a $0, cuánto caería en verde / revisión por categoría y señal. Con
`--semilla` usa semilla.py (no necesita las tablas de configuración: sirve para
medir antes de crear nada); sin él, usa la configuración vigente de la BD.
Con `--precision` agrega la tasa de aprobación de lo que el equipo ya revisó:
esa es la precisión MEDIDA (la de docs/06 es inferida sobre muestras).

    python backtest_fase2.py --dias 30 --semilla
    python backtest_fase2.py --dias 30 --muestra 5
    python backtest_fase2.py --precision
"""

from __future__ import annotations

import argparse
import logging
import random
from collections import defaultdict

import barrido
from bd import BD


def _tabla_resultados(st, dias: int) -> None:
    filas = sorted(k for k in st if isinstance(k, tuple) and k[0] == "cat")
    print(f"\n{'categoría':9} {'estado':9} {'señal':18} {'filas':>7} {'/día':>6}")
    for k in filas:
        print(f"{k[1]:9} {k[2]:9} {k[3]:18} {st[k]:7} {st[k] / dias:6.1f}")
    for est in ("verde", "revision"):
        n = sum(v for k, v in st.items() if isinstance(k, tuple) and k[0] == "cat" and k[2] == est)
        print(f"TOTAL {est:9} {n:7} ({n / dias:.1f}/día)")
    print("\npor fuente:", {f"{k[1]}·{k[2]}": v for k, v in sorted(st.items(), key=str)
                            if isinstance(k, tuple) and k[0] == "tabla"})
    print(f"leídas={st['filas_leidas']} descartes={st['descartes']} pendientes={st['pendientes']} "
          f"calzan={st['calzan']} · {st['segundos']} s · reglas {st['version']}")


def _precision(bd: BD) -> None:
    filas = bd.todos(
        "SELECT categoria, senal, estado_auto, decision, COUNT(*) n FROM clasificador_f2_resultado "
        "WHERE decision IS NOT NULL GROUP BY categoria, senal, estado_auto, decision")
    agg = defaultdict(lambda: [0, 0])
    for r in filas:
        agg[(r["categoria"], r["estado_auto"], r["senal"])][0 if r["decision"] == "aprobado" else 1] += r["n"]
    print(f"\nPRECISIÓN MEDIDA (revisiones humanas)\n{'categoría':9} {'estado':9} {'señal':18} {'aprob':>6} {'rech':>6} {'%':>6}")
    for k in sorted(agg):
        a, r = agg[k]
        print(f"{k[0]:9} {k[1]:9} {k[2]:18} {a:6} {r:6} {100 * a / (a + r):5.1f}%")
    if not agg:
        print("(todavía no hay revisiones)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--semilla", action="store_true", help="usar semilla.py en vez de la config de la BD")
    ap.add_argument("--muestra", type=int, default=0, help="filas de ejemplo por categoría/señal")
    ap.add_argument("--precision", action="store_true", help="sólo la precisión medida de lo revisado")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bd = BD()
    if a.precision:
        _precision(bd)
        return
    if a.semilla:
        import semilla
        config = semilla.CATEGORIAS
    else:
        config = barrido.cargar_config(bd)
    muestras = defaultdict(list) if a.muestra else None
    st = barrido.correr(bd, "backtest", a.dias, barrido.cargar_onu(bd), config,
                        escribir=False, leer_previos=False, muestras=muestras)
    _tabla_resultados(st, a.dias)
    if muestras:
        rnd = random.Random(7)
        for k in sorted(muestras):
            v = muestras[k]
            print(f"\n### {k[0]} · {k[1]} (n={len(v)})")
            for t, i, d, o, terms in rnd.sample(v, min(a.muestra, len(v))):
                print(f"  {t[:4]} #{i} | {d} | ONU: {o} | {terms[:40]}")


if __name__ == "__main__":
    main()
