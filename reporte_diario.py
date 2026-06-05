#!/usr/bin/env python3
"""Reporte diario del clasificador IA — top FP/FN del día, costo, candidatos.

Pensado para correr cada día a las 22:00 UTC (18:00 Chile, después del turno
Carolina 9-16 + margen). Guarda el reporte en JSON + Markdown en
`/app/reportes/reporte_YYYY-MM-DD.{json,md}` para que el panel los muestre.

Uso:
    python3 reporte_diario.py [--dia YYYY-MM-DD]     # día Chile específico
    python3 reporte_diario.py                         # día de ayer Chile

NO gasta API. Solo lee clasificador_ia_log y clasificador_ia_backtest.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from db import conectar
from reglas import normalizar
from taxonomia import cargar_taxonomia

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("reporte_diario")

REPORTES_DIR = Path("/app/reportes")
REPORTES_DIR.mkdir(parents=True, exist_ok=True)

_TZ_CHILE = timedelta(hours=4)


def _dia_chile_a_rango_utc(dia_chile: str) -> "tuple[datetime, datetime]":
    """Convierte 'YYYY-MM-DD' (día Chile) a (inicio_utc, fin_utc)."""
    fecha = datetime.strptime(dia_chile, "%Y-%m-%d")
    inicio_utc = datetime.combine(fecha.date(), datetime.min.time()) + _TZ_CHILE
    fin_utc = inicio_utc + timedelta(days=1)
    return inicio_utc, fin_utc


def _metricas_panel(cur, ini: datetime, fin: datetime) -> dict:
    """Métricas del feedback de Carolina ese día (lo que aprobó/corrigió)."""
    cur.execute("""
        SELECT COUNT(*) total,
            SUM(interes_sugerido=1) ia_int,
            SUM(interes_sugerido=0) ia_desc,
            SUM(feedback_correcto=1) acuerdo,
            SUM(interes_sugerido=1 AND feedback_correcto=0
                AND (feedback_pactivo IS NULL OR feedback_pactivo='')) fp,
            SUM(interes_sugerido=0 AND feedback_correcto=0
                AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>'') fn,
            SUM(interes_sugerido=1 AND feedback_correcto=0
                AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
                AND feedback_pactivo<>pactivo_sugerido) cambio_pact
        FROM clasificador_ia_log
        WHERE revisado_en >= %s AND revisado_en < %s AND revisado=1
    """, (ini, fin))
    return cur.fetchone()


def _por_metodo(cur, ini: datetime, fin: datetime) -> list:
    cur.execute("""
        SELECT metodo, COUNT(*) n,
            SUM(feedback_correcto=1) ok,
            SUM(feedback_correcto=0) err
        FROM clasificador_ia_log
        WHERE revisado_en >= %s AND revisado_en < %s AND revisado=1
        GROUP BY metodo ORDER BY n DESC
    """, (ini, fin))
    return cur.fetchall()


def _top_fp(cur, ini: datetime, fin: datetime, lim: int = 15) -> list:
    cur.execute("""
        SELECT pactivo_sugerido p, metodo, COUNT(*) n,
            LEFT(MIN(descripcion), 70) ej
        FROM clasificador_ia_log
        WHERE revisado_en >= %s AND revisado_en < %s
          AND revisado=1 AND feedback_correcto=0
          AND interes_sugerido=1
          AND (feedback_pactivo IS NULL OR feedback_pactivo='')
        GROUP BY p, metodo
        ORDER BY n DESC LIMIT %s
    """, (ini, fin, lim))
    return cur.fetchall()


def _top_fn(cur, ini: datetime, fin: datetime, lim: int = 15) -> list:
    cur.execute("""
        SELECT feedback_pactivo hu, metodo, COUNT(*) n,
            LEFT(MIN(descripcion), 70) ej
        FROM clasificador_ia_log
        WHERE revisado_en >= %s AND revisado_en < %s
          AND revisado=1 AND feedback_correcto=0
          AND interes_sugerido=0
          AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
        GROUP BY hu, metodo
        ORDER BY n DESC LIMIT %s
    """, (ini, fin, lim))
    return cur.fetchall()


def _top_confusiones(cur, ini: datetime, fin: datetime, lim: int = 10) -> list:
    cur.execute("""
        SELECT pactivo_sugerido ia, feedback_pactivo hu, COUNT(*) n,
            LEFT(MIN(descripcion), 70) ej
        FROM clasificador_ia_log
        WHERE revisado_en >= %s AND revisado_en < %s
          AND revisado=1 AND feedback_correcto=0
          AND interes_sugerido=1
          AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
          AND pactivo_sugerido<>feedback_pactivo
        GROUP BY ia, hu
        ORDER BY n DESC LIMIT %s
    """, (ini, fin, lim))
    return cur.fetchall()


def _costo_dia(cur, ini: datetime, fin: datetime) -> dict:
    cur.execute("""
        SELECT COUNT(*) llamadas,
            SUM(costo_usd) usd,
            SUM(tokens_in) toks_in, SUM(tokens_out) toks_out,
            SUM(cache_read_tok) cache_r, SUM(cache_write_tok) cache_w
        FROM clasificador_ia_log
        WHERE creado_en >= %s AND creado_en < %s AND metodo='claude'
    """, (ini, fin))
    return cur.fetchone()


def _candidatos_crud(cur, ini: datetime, activos_norm: set, ndias: int = 30) -> list:
    """Pactivos en feedback humano de últimos N días que NO están en catálogo."""
    cur.execute("""
        SELECT feedback_pactivo p, COUNT(*) n,
            LEFT(MIN(descripcion), 70) ej
        FROM clasificador_ia_log
        WHERE feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
          AND revisado_en >= %s - INTERVAL %s DAY
          AND revisado_en < %s
          AND revisado=1
        GROUP BY p ORDER BY n DESC LIMIT 50
    """, (ini, ndias, ini + timedelta(days=1)))
    return [r for r in cur.fetchall() if normalizar(r["p"]) not in activos_norm]


def generar_reporte(dia_chile: str) -> dict:
    ini, fin = _dia_chile_a_rango_utc(dia_chile)
    tax = cargar_taxonomia()
    activos_norm = {normalizar(p) for p in tax.pactivos}
    rep = {"dia_chile": dia_chile, "generado_en": datetime.now().isoformat(),
           "pactivos_catalogo": len(tax.pactivos)}
    conn = conectar()
    with conn.cursor() as cur:
        rep["panel"] = _metricas_panel(cur, ini, fin)
        rep["por_metodo"] = _por_metodo(cur, ini, fin)
        rep["top_fp"] = _top_fp(cur, ini, fin)
        rep["top_fn"] = _top_fn(cur, ini, fin)
        rep["top_confusiones"] = _top_confusiones(cur, ini, fin)
        rep["costo"] = _costo_dia(cur, ini, fin)
        rep["candidatos_crud"] = _candidatos_crud(cur, ini, activos_norm)
    conn.close()
    return rep


def reporte_markdown(rep: dict) -> str:
    """Versión legible en Markdown."""
    p = rep["panel"]
    total = p["total"] or 1
    pct_ok = (p["acuerdo"] or 0) / total * 100
    cst = rep["costo"]
    out = [
        f"# Reporte {rep['dia_chile']}",
        f"_generado {rep['generado_en']}_  ·  catálogo: {rep['pactivos_catalogo']} pactivos",
        "",
        "## Resumen del panel (Carolina)",
        f"- Revisadas hoy: **{p['total']}**",
        f"- Acierto: **{p['acuerdo'] or 0}/{p['total']} = {pct_ok:.1f}%**",
        f"- FP (int mal): **{p['fp'] or 0}**",
        f"- FN (rescate desc): **{p['fn'] or 0}**",
        f"- Cambio pactivo: **{p['cambio_pact'] or 0}**",
        "",
        "## Costo Claude",
        f"- Llamadas: **{cst['llamadas'] or 0}**  ·  Total: **${cst['usd'] or 0:.4f}**",
        f"- Tokens in/out: {cst['toks_in'] or 0} / {cst['toks_out'] or 0}",
        f"- Cache read/write: {cst['cache_r'] or 0} / {cst['cache_w'] or 0}",
        "",
        "## Por método",
        "| método | n | ok | err |",
        "|---|---:|---:|---:|",
    ]
    for r in rep["por_metodo"]:
        out.append(f"| {r['metodo']} | {r['n']} | {r['ok']} | {r['err']} |")
    out += ["", "## Top FP (IA dijo interés, humano descartó)",
            "| n | IA pact | vía | ej |", "|---:|---|---|---|"]
    for r in rep["top_fp"]:
        out.append(f"| {r['n']} | `{r['p']}` | `{r['metodo']}` | {(r['ej'] or '')[:60]} |")
    out += ["", "## Top FN (IA descartó, humano rescató)",
            "| n | HU pact | vía descarte | ej |", "|---:|---|---|---|"]
    for r in rep["top_fn"]:
        out.append(f"| {r['n']} | `{r['hu']}` | `{r['metodo']}` | {(r['ej'] or '')[:60]} |")
    out += ["", "## Top confusiones de pactivo",
            "| n | IA dijo | Humano | ej |", "|---:|---|---|---|"]
    for r in rep["top_confusiones"]:
        out.append(f"| {r['n']} | `{r['ia']}` | `{r['hu']}` | {(r['ej'] or '')[:60]} |")
    if rep["candidatos_crud"]:
        out += ["", "## Candidatos a CRUD pactivos_extra (no catálogo, ≥2 usos 30d)",
                "| n | pactivo | ej |", "|---:|---|---|"]
        for r in rep["candidatos_crud"][:15]:
            if r["n"] >= 2:
                out.append(f"| {r['n']} | `{r['p']}` | {(r['ej'] or '')[:60]} |")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dia", help="día Chile YYYY-MM-DD (default: ayer)")
    args = parser.parse_args()
    if args.dia:
        dia = args.dia
    else:
        ayer = (datetime.now() - _TZ_CHILE) - timedelta(days=1)
        dia = ayer.strftime("%Y-%m-%d")
    log.info("Generando reporte del %s", dia)
    rep = generar_reporte(dia)
    json_path = REPORTES_DIR / f"reporte_{dia}.json"
    md_path = REPORTES_DIR / f"reporte_{dia}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(reporte_markdown(rep))
    log.info("Guardado: %s y %s", json_path, md_path)
    print(f"\n{reporte_markdown(rep)}")


if __name__ == "__main__":
    main()
