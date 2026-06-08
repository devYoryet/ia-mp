#!/usr/bin/env python3
"""Backtest masivo SIN gastar Claude API — REGLA absoluta del proyecto.

Re-clasifica filas humanas conocidas usando SOLO ramas deterministas de la
cascada. Si una fila terminaría llegando a Claude, NO se llama: se registra
como `skip_claude` y se compara contra el resultado de Claude que YA está
guardado en `clasificador_ia_log` (para esa misma fila_id, si existe).

Métricas que sí se calculan sin API:
  - acierto interés / pactivo / comp / pres por rama determinista
  - distribución por método (cuánto cae a Claude)
  - FP / FN por rama
  - efecto de cambios en vetos / reglas / modelos

NO sustituye al backtest "real" puntual: para validar un cambio que afecta
a Claude (regla de prompt, ejemplos), correr `bin/backtest_puntual.py` con
TOPE explícito y autorización.

Uso:
    python3 backtest_sin_claude.py --dia 2026-06-04
    python3 backtest_sin_claude.py --desde 2026-06-01 --hasta 2026-06-07

Salida: /app/reportes/backtest_sin_claude_<rango>.{json,md}
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from db import conectar
from reglas import normalizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("backtest_sin_claude")

REPORTES_DIR = Path("/app/reportes")
REPORTES_DIR.mkdir(parents=True, exist_ok=True)
_TZ_CHILE = timedelta(hours=4)


def _rango_utc(desde: str, hasta: str | None = None) -> "tuple[datetime, datetime]":
    """Convierte fechas Chile a rango UTC."""
    d_ini = datetime.strptime(desde, "%Y-%m-%d").date()
    d_fin = datetime.strptime(hasta, "%Y-%m-%d").date() if hasta else d_ini
    ini = datetime.combine(d_ini, datetime.min.time()) + _TZ_CHILE
    fin = datetime.combine(d_fin + timedelta(days=1), datetime.min.time()) + _TZ_CHILE
    return ini, fin


def _cargar_recursos() -> dict:
    """Igual que worker.py pero sin contexto de Claude (no se usa)."""
    from cruce_base import cargar_cruce_base
    from descarte_items import cargar_descartes
    from descarte_modelo import cargar_modelo_descarte
    from modelo_marcas import cargar_modelo_marcas
    from modelo_pactivo import cargar_modelo_pactivo
    from preclasificador import precargar_comp_pres
    from reglas import indexar_combinaciones, indexar_inverso_pactivos, indexar_pactivos
    from taxonomia import cargar_taxonomia

    log.info("Cargando recursos (sin contexto/ejemplos — no se usa Claude)...")
    tax = cargar_taxonomia()
    precargar_comp_pres(["compra_agil", "Licitaciones_diarias"])
    return dict(
        taxonomia=tax,
        pactivos_norm=indexar_pactivos(tax.pactivos),
        combinaciones=indexar_combinaciones(tax.pactivos),
        indice_inverso=indexar_inverso_pactivos(tax.pactivos),
        descartes=cargar_descartes(),
        cruce=cargar_cruce_base(),
        modelo_descarte=cargar_modelo_descarte(),
        modelo_pactivo=cargar_modelo_pactivo(),
        modelo_marcas=cargar_modelo_marcas(),
    )


def _clasificar_sin_claude(rec: dict, fila: dict, tabla: str):
    """Llama a la cascada SOLO en sus ramas deterministas (sin Claude).
    Si la cascada caería en Claude, devolvemos un objeto con metodo='skip_claude'.

    Implementación: monkey-patch del módulo `clasificador_claude` para que su
    función `clasificar` no llame a la API; devuelve un Clasificacion con
    interes=None que provoca que la rama Claude no asigne nada y termine sin
    resultado válido. Para evitar el costo, sobrescribimos la función.
    """
    from cascada import clasificar_fila
    import clasificador_claude as cc

    # Sentinel: una clasificación "vacía" que la cascada interpretará como
    # "Claude no llegó a una conclusión". La cascada igual devuelve un Resultado
    # con metodo='claude' y interes=None — lo reetiquetamos.
    class _ClaudeBloqueado(Exception):
        pass

    def _bloqueado(*a, **kw):
        raise _ClaudeBloqueado()

    orig = cc.clasificar
    cc.clasificar = _bloqueado
    try:
        res = clasificar_fila(
            tabla, fila, rec["taxonomia"], rec["pactivos_norm"],
            rec["descartes"], rec["cruce"], rec["combinaciones"],
            rec["modelo_descarte"], "",  # contexto vacío
            indice_inverso=rec["indice_inverso"],
            modelo_pactivo=rec["modelo_pactivo"],
            marcas_texto="",
            modelo_marcas=rec["modelo_marcas"],
        )
        return res
    except _ClaudeBloqueado:
        return None  # señal: caería a Claude
    finally:
        cc.clasificar = orig


def backtest(desde: str, hasta: str | None = None) -> dict:
    ini, fin = _rango_utc(desde, hasta)
    rec = _cargar_recursos()
    log.info("Rango UTC: %s a %s  (%d pactivos)", ini, fin, len(rec["taxonomia"].pactivos))

    # 1) Cargar filas revisadas por humano en el rango
    conn = conectar()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT log.id log_id, log.tabla_origen, log.fila_id, log.descripcion,
                log.metodo metodo_ant, log.pactivo_sugerido pact_ant,
                log.interes_sugerido int_ant,
                log.feedback_correcto fc, log.feedback_pactivo hu_pact
            FROM clasificador_ia_log log
            WHERE log.revisado=1
              AND log.revisado_en >= %s AND log.revisado_en < %s
            ORDER BY log.id""", (ini, fin))
        filas = cur.fetchall()
    log.info("Filas revisadas: %d", len(filas))

    # 2) Cargar contexto (Titulo/VINCULOS) por chunks
    ids_por_tabla = defaultdict(list)
    for r in filas: ids_por_tabla[r["tabla_origen"]].append(r["fila_id"])
    ctx = {}
    with conn.cursor() as cur:
        for tab, ids in ids_por_tabla.items():
            for i in range(0, len(ids), 5000):
                chunk = ids[i:i+5000]
                cur.execute(
                    f"SELECT id, Titulo, VINCULOS, Descripcion FROM `{tab}` "
                    f"WHERE id IN ({','.join(str(i) for i in chunk)})")
                for x in cur.fetchall():
                    ctx[(tab, x["id"])] = x
    conn.close()

    # 3) Bucle de clasificación
    contador_metodo = Counter()
    contador_clase = Counter()
    fp_por_metodo = Counter()
    fn_por_metodo = Counter()
    confusion_pact = Counter()  # (ia, hu)
    skip_claude = 0
    skip_compare_con_anterior = 0

    for i, log_row in enumerate(filas):
        if i % 5000 == 0 and i:
            log.info("  procesadas %d/%d (skip Claude: %d)", i, len(filas), skip_claude)
        x = ctx.get((log_row["tabla_origen"], log_row["fila_id"]))
        if not x: continue
        fila = {
            "id": log_row["fila_id"],
            "Descripcion": x["Descripcion"] or log_row["descripcion"],
            "Titulo": x["Titulo"] or "",
            "VINCULOS": x["VINCULOS"] or "",
        }
        try:
            res = _clasificar_sin_claude(rec, fila, log_row["tabla_origen"])
        except Exception as e:
            contador_metodo["ERROR"] += 1
            continue

        # Si la cascada hubiera llamado a Claude, usamos el resultado anterior
        # ya guardado (esto NO gasta API — es el dato histórico de cuando se
        # clasificó en producción).
        if res is None:
            skip_claude += 1
            # Comparar el RESULTADO HISTÓRICO de Claude (no la cascada nueva)
            metodo = log_row["metodo_ant"]
            int_ia = log_row["int_ant"]
            pact_ia = log_row["pact_ant"]
            skip_compare_con_anterior += 1
        else:
            metodo = res.metodo
            int_ia = res.interes
            pact_ia = res.pactivo

        contador_metodo[metodo] += 1

        # Sentido del feedback humano
        if log_row["fc"] == 1:
            hu_int = log_row["int_ant"]
            hu_pact = log_row["pact_ant"]
        else:
            hu_int = 1 if (log_row["hu_pact"] and log_row["hu_pact"].strip()) else 0
            hu_pact = log_row["hu_pact"]

        # Etiquetar clase
        if int_ia == 1 and hu_int == 1:
            clase = "TP_int"
            if pact_ia and hu_pact and pact_ia.strip().lower() != hu_pact.strip().lower():
                confusion_pact[(pact_ia, hu_pact)] += 1
        elif int_ia == 0 and hu_int == 0: clase = "TN_int"
        elif int_ia == 1 and hu_int == 0:
            clase = "FP"; fp_por_metodo[metodo] += 1
        else:
            clase = "FN"; fn_por_metodo[metodo] += 1
        contador_clase[clase] += 1

    return {
        "rango": f"{desde} a {hasta or desde}",
        "ini_utc": ini.isoformat(), "fin_utc": fin.isoformat(),
        "filas_total": len(filas),
        "procesadas": sum(contador_clase.values()),
        "skip_claude_count": skip_claude,
        "skip_usaron_resultado_anterior": skip_compare_con_anterior,
        "contador_clase": dict(contador_clase),
        "contador_metodo": dict(contador_metodo),
        "fp_por_metodo": dict(fp_por_metodo),
        "fn_por_metodo": dict(fn_por_metodo),
        "top_confusiones": [
            {"ia": k[0], "hu": k[1], "n": v}
            for k, v in confusion_pact.most_common(20)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dia", help="día Chile YYYY-MM-DD")
    parser.add_argument("--desde", help="desde día Chile YYYY-MM-DD")
    parser.add_argument("--hasta", help="hasta día Chile YYYY-MM-DD")
    args = parser.parse_args()
    if args.dia:
        desde, hasta = args.dia, args.dia
    elif args.desde:
        desde = args.desde; hasta = args.hasta or args.desde
    else:
        ayer = (datetime.now() - _TZ_CHILE) - timedelta(days=1)
        desde = hasta = ayer.strftime("%Y-%m-%d")

    log.info("Backtest SIN Claude — %s a %s", desde, hasta)
    res = backtest(desde, hasta)
    nombre = desde if desde == hasta else f"{desde}__{hasta}"
    out = REPORTES_DIR / f"backtest_sin_claude_{nombre}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    log.info("Guardado: %s", out)
    # Resumen
    total = res["procesadas"] or 1
    print(f"\n=== Backtest SIN Claude  {res['rango']}  ===")
    print(f"  filas total: {res['filas_total']}  procesadas: {res['procesadas']}")
    print(f"  cayeron a Claude (no llamado, comparado vs histórico): {res['skip_claude_count']}")
    print(f"\n  clase:")
    for k, v in res["contador_clase"].items():
        print(f"    {k:<8} {v:>6}  ({v/total*100:.1f}%)")
    print(f"\n  método:")
    for k, v in sorted(res["contador_metodo"].items(), key=lambda x: -x[1]):
        print(f"    {k:<28} {v:>6}")


if __name__ == "__main__":
    main()
