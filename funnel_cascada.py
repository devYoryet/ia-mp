"""Funnel de la cascada — GUARD por etapa, medido contra el log real de producción.

Responde, por cada etapa y sin inferir nada:
  - cuántas filas LE LLEGAN (las que ninguna etapa anterior resolvió)
  - cuántas RESUELVE y con qué salida (interés / descarte)
  - qué pasó con esas filas cuando un humano las revisó, separando:
        FP        = la IA dijo interés y el humano descartó
        pact_malo = interés correcto pero pactivo equivocado
        comp_pres = pactivo correcto, sólo se ajustó comp/presentación  (NO es error de clasificación)
        FN        = la IA descartó y el humano rescató

La distinción comp_pres es la clave: medir con `feedback_correcto=0` a secas
infla el error de las ramas buenas (ej. regla_diccionario→apósito son ajustes
de dosis sobre un pactivo correcto, no clasificaciones malas).

Uso:
    python3 funnel_cascada.py [dias] [tabla]
"""
from __future__ import annotations

import sys

from db import conectar

# Orden REAL de ejecución en cascada.clasificar_fila. Cada entrada es
# (etiqueta, [metodos del log que produce esa etapa]).
ETAPAS = [
    ("0  vetos inicio",      "metodo LIKE 'veto\\_%%'"),
    ("1  cruce_base",        "metodo = 'cruce_base'"),
    ("2  historico",         "metodo = 'historico'"),
    ("3  modelo_adjunto",    "metodo = 'modelo_adjunto'"),
    ("4  descarte_item",     "metodo = 'descarte_item'"),
    ("5  regla_diccionario", "metodo IN ('regla_diccionario','conflicto_regla_modelo')"),
    ("6  modelo_descarte",   "metodo = 'modelo_descarte'"),
    ("7  modelo_pactivo",    "metodo = 'modelo_pactivo'"),
    ("8  modelo_marcas",     "metodo IN ('modelo_marcas','modelo_marcas_posible')"),
    ("9  claude",            "metodo IN ('claude','claude_pact_inactivo')"),
]

_METRICAS = """
    COUNT(*)                                                              AS resuelve,
    SUM(interes_sugerido = 1)                                             AS interes,
    SUM(revisado = 1)                                                     AS revisadas,
    SUM(feedback_correcto = 0 AND interes_sugerido = 1
        AND (feedback_pactivo IS NULL OR feedback_pactivo = ''))          AS fp,
    SUM(feedback_correcto = 0 AND interes_sugerido = 1
        AND feedback_pactivo <> '' AND feedback_pactivo <> pactivo_sugerido) AS pact_malo,
    SUM(feedback_correcto = 0 AND interes_sugerido = 1
        AND feedback_pactivo = pactivo_sugerido)                          AS comp_pres,
    SUM(feedback_correcto = 0 AND interes_sugerido = 0)                   AS fn,
    COALESCE(SUM(costo_usd), 0)                                           AS usd
"""


def funnel(dias: int = 30, tabla: str | None = None) -> list[dict]:
    filtro_tabla = " AND tabla_origen = %s" if tabla else ""
    args_extra = (tabla,) if tabla else ()
    conn = conectar()
    filas = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) n FROM clasificador_ia_log "
                f"WHERE creado_en >= NOW() - INTERVAL %s DAY{filtro_tabla}",
                (dias,) + args_extra,
            )
            restantes = int(cur.fetchone()["n"])
            for etiqueta, cond in ETAPAS:
                cur.execute(
                    f"SELECT {_METRICAS} FROM clasificador_ia_log "
                    f"WHERE creado_en >= NOW() - INTERVAL %s DAY{filtro_tabla} AND {cond}",
                    (dias,) + args_extra,
                )
                r = dict(cur.fetchone())
                r["etapa"] = etiqueta
                r["llegan"] = restantes
                restantes -= int(r["resuelve"])
                filas.append(r)
    finally:
        conn.close()
    return filas


def imprimir(filas: list[dict], dias: int, tabla: str | None) -> None:
    print(f"\n=== FUNNEL DE LA CASCADA · {dias} días · {tabla or 'todas las tablas'} ===\n")
    cab = (f"{'etapa':<22}{'llegan':>9}{'resuelve':>9}{'%corte':>8}{'interés':>9}"
           f"{'revis.':>8}{'FP':>6}{'pact✗':>7}{'c/p':>6}{'FN':>5}{'prec.int':>10}{'USD':>8}")
    print(cab)
    print("-" * len(cab))
    for r in filas:
        llegan, res = int(r["llegan"]), int(r["resuelve"])
        inte, rev = int(r["interes"]), int(r["revisadas"])
        fp, pm, cp, fn = int(r["fp"]), int(r["pact_malo"]), int(r["comp_pres"]), int(r["fn"])
        corte = 100 * res / llegan if llegan else 0
        # precisión de interés: de las filas de interés revisadas, las que el
        # humano NO descartó ni le cambió el pactivo. comp/pres NO cuenta como error.
        prec = f"{100 * (inte - fp - pm) / inte:.1f}%" if inte else "—"
        print(f"{r['etapa']:<22}{llegan:>9,}{res:>9,}{corte:>7.1f}%{inte:>9,}"
              f"{rev:>8,}{fp:>6}{pm:>7}{cp:>6}{fn:>5}{prec:>10}{float(r['usd']):>8.2f}")
    print("-" * len(cab))
    tot_fp = sum(int(r["fp"]) for r in filas)
    tot_pm = sum(int(r["pact_malo"]) for r in filas)
    tot_cp = sum(int(r["comp_pres"]) for r in filas)
    tot_fn = sum(int(r["fn"]) for r in filas)
    print(f"TOTAL errores reales: {tot_fp} FP + {tot_pm} pactivo malo + {tot_fn} FN "
          f"= {tot_fp + tot_pm + tot_fn}   (+ {tot_cp} ajustes de comp/pres, que NO son error)\n")


if __name__ == "__main__":
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    t = sys.argv[2] if len(sys.argv) > 2 else None
    imprimir(funnel(d, t), d, t)
