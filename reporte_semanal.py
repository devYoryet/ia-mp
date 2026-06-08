#!/usr/bin/env python3
"""Reporte semanal — drift de modelos + propuestas regla→veto + métricas 7d.

Pensado para correr cada lunes a las 12:00 UTC (09:00 Chile, inicio de
semana). Guarda /app/reportes/semanal_YYYY-MM-DD.{json,md} y aparece en
el endpoint /reportes del panel (junto a los diarios).

NO gasta API. Solo lee BD (clasificador_ia_log + clasificador_ia_backtest).
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
log = logging.getLogger("reporte_semanal")

REPORTES_DIR = Path("/app/reportes")
REPORTES_DIR.mkdir(parents=True, exist_ok=True)
_TZ_CHILE = timedelta(hours=4)


def _semana(d_chile: str) -> tuple:
    """Devuelve (lunes_inicio_utc, lunes_siguiente_utc) — semana ISO que contiene
    el día d_chile dado."""
    fecha = datetime.strptime(d_chile, "%Y-%m-%d").date()
    # Lunes anterior o el mismo día si es lunes
    lunes = fecha - timedelta(days=fecha.weekday())
    ini_utc = datetime.combine(lunes, datetime.min.time()) + _TZ_CHILE
    fin_utc = ini_utc + timedelta(days=7)
    return lunes.isoformat(), ini_utc, fin_utc


def generar(d_chile: str) -> dict:
    lunes, ini, fin = _semana(d_chile)
    tax = cargar_taxonomia()
    activos_norm = {normalizar(p) for p in tax.pactivos}
    rep = {"semana_lunes": lunes,
           "ini_utc": ini.isoformat(),
           "fin_utc": fin.isoformat(),
           "generado_en": datetime.now().isoformat(),
           "pactivos_catalogo": len(tax.pactivos)}

    conn = conectar()

    with conn.cursor() as cur:
        # ===== Panel Carolina ====
        cur.execute("""
            SELECT COUNT(*) total,
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
        rep["panel"] = cur.fetchone()

        # ===== Costo semana ====
        cur.execute("""
            SELECT SUM(costo_usd) usd, COUNT(*) llamadas,
                SUM(cache_read_tok) cache_r, SUM(cache_write_tok) cache_w
            FROM clasificador_ia_log
            WHERE creado_en >= %s AND creado_en < %s AND metodo='claude'
        """, (ini, fin))
        rep["costo"] = cur.fetchone()

        # ===== Acierto por día (drift) ====
        cur.execute("""
            SELECT DATE(revisado_en - INTERVAL 4 HOUR) dia,
                COUNT(*) n,
                ROUND(SUM(feedback_correcto=1)/COUNT(*)*100, 2) pct_int
            FROM clasificador_ia_log
            WHERE revisado_en >= %s AND revisado_en < %s AND revisado=1
            GROUP BY dia ORDER BY dia
        """, (ini, fin))
        rep["acierto_por_dia"] = cur.fetchall()

        # ===== Drift de modelos vs ventana 30d ====
        rep["drift"] = {}
        for ventana, label in [(7, "esta_semana"), (30, "ult_30d")]:
            cur.execute(f"""
                SELECT ia_metodo m,
                    COUNT(*) n,
                    ROUND(SUM(coincide_interes)/COUNT(*)*100, 2) pct_int,
                    ROUND(SUM(coincide_pactivo)/NULLIF(COUNT(coincide_pactivo),0)*100, 2) pct_pact
                FROM clasificador_ia_backtest
                WHERE ia_metodo IN ('modelo_pactivo','modelo_descarte',
                                    'modelo_marcas','modelo_marcas_posible')
                  AND creado_en >= NOW() - INTERVAL {ventana} DAY
                GROUP BY m
            """)
            rep["drift"][label] = cur.fetchall()

        # ===== Top FP / FN repetidos (≥5× en 7d) ====
        cur.execute("""
            SELECT pactivo_sugerido p, metodo m, COUNT(*) n,
                LEFT(MIN(descripcion), 80) ej
            FROM clasificador_ia_log
            WHERE revisado_en >= %s AND revisado_en < %s
              AND revisado=1 AND feedback_correcto=0 AND interes_sugerido=1
              AND (feedback_pactivo IS NULL OR feedback_pactivo='')
            GROUP BY p, m HAVING n >= 5
            ORDER BY n DESC LIMIT 20
        """, (ini, fin))
        rep["top_fp_repetidos"] = cur.fetchall()

        cur.execute("""
            SELECT feedback_pactivo hu, metodo m, COUNT(*) n,
                LEFT(MIN(descripcion), 80) ej
            FROM clasificador_ia_log
            WHERE revisado_en >= %s AND revisado_en < %s
              AND revisado=1 AND feedback_correcto=0 AND interes_sugerido=0
              AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
            GROUP BY hu, m HAVING n >= 5
            ORDER BY n DESC LIMIT 20
        """, (ini, fin))
        rep["top_fn_repetidos"] = cur.fetchall()

        # ===== Candidatos a CRUD pactivos_extra ====
        cur.execute("""
            SELECT feedback_pactivo p, COUNT(*) n,
                LEFT(MIN(descripcion), 80) ej
            FROM clasificador_ia_log
            WHERE feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
              AND revisado_en >= %s AND revisado_en < %s AND revisado=1
            GROUP BY p ORDER BY n DESC LIMIT 50
        """, (ini, fin))
        rep["candidatos_crud"] = [r for r in cur.fetchall()
                                   if normalizar(r["p"]) not in activos_norm
                                   and r["n"] >= 3]

        # ===== Patrones recurrentes para sugerir VETOS ====
        # Pares (pactivo_sugerido, keyword común en descripción)
        cur.execute("""
            SELECT pactivo_sugerido p, COUNT(*) n
            FROM clasificador_ia_log
            WHERE revisado_en >= %s AND revisado_en < %s
              AND revisado=1 AND feedback_correcto=0 AND interes_sugerido=1
              AND (feedback_pactivo IS NULL OR feedback_pactivo='')
              AND pactivo_sugerido IS NOT NULL
            GROUP BY p HAVING n >= 5 ORDER BY n DESC
        """, (ini, fin))
        rep["candidatos_veto"] = cur.fetchall()

        # ===== Vetos actuales en BD ====
        cur.execute("""SELECT COUNT(*) n, SUM(activa) act,
            SUM(protegido) prot FROM clasificador_ia_reglas WHERE tipo='veto'""")
        rep["vetos_actuales"] = cur.fetchone()

    conn.close()
    return rep


def to_md(r: dict) -> str:
    p = r["panel"]
    total = p["total"] or 1
    pct = (p["acuerdo"] or 0) / total * 100
    cst = r["costo"]
    out = [
        f"# Reporte semanal — semana del {r['semana_lunes']}",
        f"_generado {r['generado_en']}_  ·  catálogo: {r['pactivos_catalogo']} pactivos",
        "",
        "## 📊 Resumen Carolina (panel)",
        f"- Revisadas: **{p['total']:,}**",
        f"- Acuerdo: **{p['acuerdo']:,}/{p['total']:,} = {pct:.1f}%**",
        f"- FP (interés mal): **{p['fp'] or 0}**  ·  FN (rescate descarte): **{p['fn'] or 0}**",
        f"- Cambios pactivo: **{p['cambio_pact'] or 0}**",
        "",
        "## 💰 Costo Claude",
        f"- Llamadas: **{cst['llamadas'] or 0:,}**  ·  Total: **${cst['usd'] or 0:.2f}**",
        f"- Cache read/write: {cst['cache_r'] or 0:,} / {cst['cache_w'] or 0:,}",
        "",
        "## 📈 Acierto por día (drift dentro de la semana)",
        "| día | n | %acierto |",
        "|---|---:|---:|",
    ]
    for row in r["acierto_por_dia"]:
        out.append(f"| {row['dia']} | {row['n']:,} | {row['pct_int']}% |")

    out += ["", "## 🤖 Drift modelos (esta semana vs últimos 30d)"]
    for label, ventana_data in [("Esta semana", r["drift"]["esta_semana"]),
                                 ("Últimos 30d", r["drift"]["ult_30d"])]:
        out += [f"### {label}",
                "| método | n | %interés | %pactivo |",
                "|---|---:|---:|---:|"]
        for row in ventana_data:
            out.append(f"| {row['m']} | {row['n']:,} | {row['pct_int']}% | {row['pct_pact']}% |")
        out.append("")

    if r["top_fp_repetidos"]:
        out += ["## ❌ Top FP repetidos (≥5×) — candidatos a veto",
                "| n | IA pact | vía | ej |", "|---:|---|---|---|"]
        for x in r["top_fp_repetidos"]:
            out.append(f"| {x['n']} | `{x['p']}` | `{x['m']}` | {(x['ej'] or '')[:60]} |")
        out.append("")

    if r["top_fn_repetidos"]:
        out += ["## ⚠️ Top FN repetidos (≥5×)",
                "| n | humano pact | vía descarte | ej |", "|---:|---|---|---|"]
        for x in r["top_fn_repetidos"]:
            out.append(f"| {x['n']} | `{x['hu']}` | `{x['m']}` | {(x['ej'] or '')[:60]} |")
        out.append("")

    if r["candidatos_crud"]:
        out += ["## 🛒 Candidatos a CRUD pactivos_extra",
                "_pactivos que Carolina usa pero no están en catálogo_",
                "| n | pactivo | ej |", "|---:|---|---|"]
        for x in r["candidatos_crud"][:15]:
            out.append(f"| {x['n']} | `{x['p']}` | {(x['ej'] or '')[:60]} |")
        out.append("")

    v = r["vetos_actuales"]
    out += [f"## 🛑 Vetos en cascada", f"- Total: {v['n']}  ·  activos: {v['act']}  ·  protegidos: {v['prot']}"]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dia", help="día Chile YYYY-MM-DD (default: hoy)")
    args = parser.parse_args()
    if args.dia:
        dia = args.dia
    else:
        dia = (datetime.now() - _TZ_CHILE).strftime("%Y-%m-%d")
    log.info("Reporte semanal — semana que contiene %s", dia)
    rep = generar(dia)
    lunes = rep["semana_lunes"]
    json_path = REPORTES_DIR / f"semanal_{lunes}.json"
    md_path = REPORTES_DIR / f"semanal_{lunes}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(to_md(rep))
    log.info("Guardado: %s  y  %s", json_path, md_path)
    print(to_md(rep))


if __name__ == "__main__":
    main()
