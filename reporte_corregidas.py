#!/usr/bin/env python3
"""Reporte diario de CORREGIDAS — el oro del feedback humano.

Para presentar a las 10:30 AM Chile (14:30 UTC) cada día. Analiza las
clasificaciones que Carolina/admins corrigieron en las últimas 24h y las
cruza contra los vetos/reglas ACTIVOS en BD para detectar:

  - Patrones recurrentes NO cubiertos por vetos (proponer nuevo veto)
  - Vetos sobre-restrictivos (humanos rescataron muchas filas que un veto
    bloqueó → veto está vetando producto válido)
  - Reglas Claude con FP recurrente
  - Correcciones de pactivo (mismo interés, distinto pactivo)

NO gasta API. Solo lee `clasificador_ia_log` + `clasificador_ia_reglas`.
Output: /app/reportes/corregidas_YYYY-MM-DD.{json,md}.

Uso:
    python3 reporte_corregidas.py [--dia YYYY-MM-DD]
    python3 reporte_corregidas.py                       # ayer Chile (default)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from db import conectar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("reporte_corregidas")

REPORTES_DIR = Path("/app/reportes")
REPORTES_DIR.mkdir(parents=True, exist_ok=True)

_TZ_CHILE = timedelta(hours=4)


def _rango_24h(dia_chile: str) -> tuple[datetime, datetime]:
    """Día Chile YYYY-MM-DD → (inicio_utc, fin_utc) del día completo."""
    fecha = datetime.strptime(dia_chile, "%Y-%m-%d")
    inicio_utc = datetime.combine(fecha.date(), datetime.min.time()) + _TZ_CHILE
    fin_utc = inicio_utc + timedelta(days=1)
    return inicio_utc, fin_utc


def cargar_vetos_activos(conn) -> list[dict]:
    """Lee los vetos activos de BD con regex compilada."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, nombre, aplica_a, regex_pattern, protegido "
            "FROM clasificador_ia_reglas WHERE tipo='veto' AND activa=1"
        )
        vetos = cur.fetchall()
    out = []
    for v in vetos:
        try:
            pat = re.compile(v["regex_pattern"], re.IGNORECASE)
        except re.error as exc:
            log.warning("Regex inválida en veto %s: %s", v["nombre"], exc)
            continue
        out.append({"id": v["id"], "nombre": v["nombre"],
                    "aplica_a": v["aplica_a"], "pat": pat,
                    "protegido": v["protegido"]})
    return out


def matchear_vetos(texto: str, vetos: list[dict]) -> list[str]:
    """Devuelve los nombres de vetos cuyo regex matchea el texto."""
    if not texto:
        return []
    return [v["nombre"] for v in vetos if v["pat"].search(texto)]


def generar(dia_chile: str) -> dict:
    ini, fin = _rango_24h(dia_chile)
    log.info("Generando reporte CORREGIDAS para %s (%s a %s UTC)", dia_chile, ini, fin)
    conn = conectar()
    vetos = cargar_vetos_activos(conn)
    log.info("Vetos activos cargados: %d", len(vetos))

    rep: dict = {
        "dia_chile": dia_chile,
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "vetos_activos_n": len(vetos),
    }

    with conn.cursor() as cur:
        # -------- TOTALES --------
        cur.execute("""
            SELECT COUNT(*) total,
              SUM(feedback_correcto=1) acuerdo,
              SUM(feedback_correcto=0 AND feedback_pactivo IS NOT NULL
                  AND feedback_pactivo<>'') corrigio_pact,
              SUM(feedback_correcto=0 AND (feedback_pactivo IS NULL OR feedback_pactivo='')
                  AND interes_sugerido=1) rebajo_a_descarte,
              SUM(feedback_correcto=0 AND feedback_pactivo IS NOT NULL
                  AND feedback_pactivo<>'' AND interes_sugerido=0) rescato
            FROM clasificador_ia_log
            WHERE revisado=1 AND revisado_en >= %s AND revisado_en < %s
        """, (ini, fin))
        rep["totales"] = cur.fetchone()
        total = rep["totales"]["total"] or 1
        rep["acuerdo_pct"] = round((rep["totales"]["acuerdo"] or 0) / total * 100, 2)

        # -------- SECCIÓN 1: REBAJES (IA interés → humano descarte) --------
        # Es el top de FP. Para cada uno, contar y cruzar contra vetos
        # para ver si algún veto debería haberlo capturado.
        cur.execute("""
            SELECT pactivo_sugerido p_IA, metodo via, COUNT(*) n,
              LEFT(MIN(descripcion), 120) ej_min,
              GROUP_CONCAT(DISTINCT LEFT(descripcion,80) SEPARATOR ' || ') ejemplos
            FROM clasificador_ia_log
            WHERE revisado=1 AND interes_sugerido=1 AND feedback_correcto=0
              AND (feedback_pactivo IS NULL OR feedback_pactivo='')
              AND revisado_en >= %s AND revisado_en < %s
            GROUP BY p_IA, via
            ORDER BY n DESC LIMIT 25
        """, (ini, fin))
        rebajes = []
        for r in cur.fetchall():
            # Para una muestra (la primera glosa), ver si algún veto matchea
            vetos_que_deberian = matchear_vetos(r["ej_min"] or "", vetos)
            rebajes.append({
                "pactivo_ia": r["p_IA"], "via": r["via"], "n": int(r["n"]),
                "ejemplo": r["ej_min"],
                "vetos_que_matchean_ejemplo": vetos_que_deberian,
            })
        rep["top_rebajes"] = rebajes

        # -------- SECCIÓN 2: RESCATES (IA descarte → humano interés) --------
        # El humano dice que SÍ era interés. Si la IA descartó por un veto,
        # ese veto está sobre-actuando. Capturamos `metodo` para saber.
        cur.execute("""
            SELECT feedback_pactivo p, metodo via, COUNT(*) n,
              LEFT(MIN(descripcion), 120) ej_min
            FROM clasificador_ia_log
            WHERE revisado=1 AND interes_sugerido=0 AND feedback_correcto=0
              AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
              AND revisado_en >= %s AND revisado_en < %s
            GROUP BY p, via ORDER BY n DESC LIMIT 25
        """, (ini, fin))
        rep["top_rescates"] = [dict(r) for r in cur.fetchall()]

        # -------- SECCIÓN 3: CORRECCIONES DE PACTIVO --------
        cur.execute("""
            SELECT pactivo_sugerido p_IA, feedback_pactivo p_hum,
              metodo via, COUNT(*) n, LEFT(MIN(descripcion), 100) ej
            FROM clasificador_ia_log
            WHERE revisado=1 AND feedback_correcto=0
              AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>''
              AND pactivo_sugerido <> feedback_pactivo
              AND revisado_en >= %s AND revisado_en < %s
            GROUP BY p_IA, p_hum, via HAVING n >= 2
            ORDER BY n DESC LIMIT 20
        """, (ini, fin))
        rep["top_correcciones"] = [dict(r) for r in cur.fetchall()]

        # -------- SECCIÓN 4: EFECTIVIDAD DE VETOS --------
        # Cuántas filas bloqueó cada veto en 24h, cuántas fueron rescatadas.
        # `metodo` registra el veto que actuó (ej: 'veto_no_farma_puntuales_2').
        cur.execute("""
            SELECT metodo veto, COUNT(*) total_bloqueadas,
              SUM(revisado=1 AND interes_sugerido=0 AND feedback_correcto=0
                  AND feedback_pactivo IS NOT NULL AND feedback_pactivo<>'') rescatadas
            FROM clasificador_ia_log
            WHERE metodo LIKE 'veto_%%'
              AND creado_en >= %s AND creado_en < %s
            GROUP BY veto HAVING total_bloqueadas >= 5
            ORDER BY total_bloqueadas DESC LIMIT 30
        """, (ini, fin))
        vetos_efectividad = []
        for r in cur.fetchall():
            total = int(r["total_bloqueadas"])
            resc = int(r["rescatadas"] or 0)
            ratio = resc / total if total else 0
            vetos_efectividad.append({
                "veto": r["veto"], "total_bloqueadas": total,
                "rescatadas_por_humano": resc,
                "ratio_falso_positivo": round(ratio, 3),
                "sospechoso": ratio >= 0.20,  # >=20% rescate = revisar
            })
        rep["efectividad_vetos"] = vetos_efectividad

    conn.close()
    return rep


def _tag_veto(v: list[str]) -> str:
    return f"⚠ DEBERÍA MATCHEAR: {', '.join(v)}" if v else ""


def to_md(rep: dict) -> str:
    t = rep["totales"]
    out = [
        f"# 📋 Reporte CORREGIDAS — {rep['dia_chile']}",
        f"_generado {rep['generado_en']}  ·  {rep['vetos_activos_n']} vetos activos_",
        "",
        "## 📊 Resumen del día",
        f"- Revisadas total: **{t['total']:,}**",
        f"- Acuerdo IA: **{t['acuerdo'] or 0:,} ({rep['acuerdo_pct']}%)**",
        f"- 🔻 Rebajes a descarte (IA dijo interés, humano descarte): **{t['rebajo_a_descarte'] or 0}**",
        f"- 🔺 Rescates (IA descarte, humano interés): **{t['rescato'] or 0}**",
        f"- ✏ Correcciones de pactivo: **{t['corrigio_pact'] or 0}**",
        "",
        "## 🔻 TOP rebajes — IA clasificó interés, humano descartó",
        "_Acción típica: nuevo veto o refinar regla Claude_",
        "",
        "| n | pactivo IA | vía | ejemplo | vetos que deberían matchear |",
        "|---:|---|---|---|---|",
    ]
    for r in rep["top_rebajes"]:
        ej = (r["ejemplo"] or "")[:80].replace("|", "\\|")
        vd = ", ".join(r["vetos_que_matchean_ejemplo"]) or "—"
        out.append(f"| {r['n']} | `{r['pactivo_ia']}` | {r['via']} | {ej} | {vd} |")

    out += ["",
            "## 🔺 TOP rescates — IA descartó, humano rescató (puede ser veto sobre-restrictivo)",
            "_Si la vía es `veto_*`, ese veto está sobre-actuando_",
            "",
            "| n | pactivo humano | vía IA | ejemplo |",
            "|---:|---|---|---|"]
    for r in rep["top_rescates"]:
        ej = (r["ej_min"] or "")[:80].replace("|", "\\|")
        out.append(f"| {r['n']} | `{r['p']}` | {r['via']} | {ej} |")

    out += ["",
            "## ✏ TOP correcciones — mismo interés, pactivo diferente",
            "",
            "| n | IA dijo | humano dijo | vía | ejemplo |",
            "|---:|---|---|---|---|"]
    for r in rep["top_correcciones"]:
        ej = (r["ej"] or "")[:80].replace("|", "\\|")
        out.append(f"| {r['n']} | `{r['p_IA']}` | `{r['p_hum']}` | {r['via']} | {ej} |")

    if rep["efectividad_vetos"]:
        out += ["",
                "## 🛡 Efectividad de vetos (últimas 24h)",
                "_Ratio FP ≥ 0.20 = veto sospechoso, considerar refinar o desactivar_",
                "",
                "| veto | bloqueó | humano rescató | % FP | ⚠ |",
                "|---|---:|---:|---:|---|"]
        for v in rep["efectividad_vetos"]:
            warn = "⚠ REVISAR" if v["sospechoso"] else ""
            out.append(
                f"| `{v['veto']}` | {v['total_bloqueadas']} | "
                f"{v['rescatadas_por_humano']} | {v['ratio_falso_positivo']*100:.1f}% | {warn} |"
            )

    out += ["", "---", "_Próximo reporte mañana 10:30 Chile_"]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dia", help="día Chile YYYY-MM-DD (default: ayer)")
    args = parser.parse_args()
    if args.dia:
        dia = args.dia
    else:
        # ayer Chile (cron 10:30 Chile reporta el día calendario previo)
        ahora_chile = datetime.now() - _TZ_CHILE
        dia = (ahora_chile - timedelta(days=1)).strftime("%Y-%m-%d")

    rep = generar(dia)
    json_path = REPORTES_DIR / f"corregidas_{dia}.json"
    md_path = REPORTES_DIR / f"corregidas_{dia}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    md_path.write_text(to_md(rep))
    log.info("Guardado: %s  y  %s", json_path, md_path)
    print(to_md(rep))


if __name__ == "__main__":
    main()
