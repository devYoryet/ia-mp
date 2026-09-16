#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cierre Adjudicadas completo (módulo legacy /legacy/cierre-adjudicadas).

Cierra el MES ANTERIOR de licitaciones adjudicadas de Mercado Público y deja
clásico, OC y prime alineados, con validación fila a fila. Reemplaza la cadena
que corría desde un portátil Windows (cierre_adjudicadas/, servidor_cierre/).

Modos:
    validar      solo lectura, sin API (V1, V3, V5-V9)            ~2 min por mes
    validar-api  solo lectura + API de Mercado Público (V2, V4)    ~1,5 h la 1ª vez (cachea)
    cierre       escribe: listado, actas, prime, resumen, OC, consulta5, Fecha;
                 repara meses anteriores con diferencias y valida al final

Qué hace el cierre de un mes M (revisa M y los MESES_REVISION-1 anteriores):
    1. listado de M desde la API -> listado_api (lo que el portal no listó)
    2. actas pendientes de M; rezagadas de meses anteriores con tope de tiempo
    3. actas de M verificadas contra la API (las que entran a consulta5 y las
       marcadas sin filas); las incompletas se vuelven a bajar
    4. por mes: Licitaciones clásico -> prime; resumen (6 tablas en M; en meses
       anteriores solo lo que no calza); OC (consulta1/3/5) y consulta5 -> prime
       cuando no calza con la regla; fila en test_matias.Fecha
    5. validación final y reporte; sello .cierre_adjudicadas_<YYYYMM>.done si M
       queda sin fallas

Uso:
    python cierre_adjudicadas_completo.py --modo validar --mes 2026-08
    python cierre_adjudicadas_completo.py --modo cierre            # mes anterior
    python cierre_adjudicadas_completo.py --modo cierre --forzar   # ignora el sello

Cron en gestor_oc (el panel corre con network_mode: host para llegar al MySQL local):
    0 7 1  * * cd /opt/ia-mp && docker compose exec -T panel python /app/cierre_adjudicadas_completo.py --modo cierre
    0 7 15 * * cd /opt/ia-mp && docker compose exec -T panel python /app/cierre_adjudicadas_completo.py --modo cierre --forzar
El día 15 repite el cierre del mes anterior: publicados y cerrados son una foto
del día en que se calculan; el batch histórico corría ~el 15 (medido: julio
2026 en tabla 4.511 proveedores = corte 15-08; con corte día 1 serían 2.073).

Salida: log en <LEGACY_TEMP_DIR>/cierre_adjudicadas.log (lo lee el panel),
copia en cierre_adjudicadas/logs/ y reporte JSON por mes en
cierre_adjudicadas/reportes/. Código: 0 sin fallas · 1 con fallas · 2 error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from cierre_adj import bd, correo, pasos, reglas, validaciones as v

TEMP_DIR = Path(os.getenv("LEGACY_TEMP_DIR", "/host/storage/temp"))
DIR_TRABAJO = TEMP_DIR / "cierre_adjudicadas"
LOG_PANEL = TEMP_DIR / "cierre_adjudicadas.log"
PID_FILE = TEMP_DIR / ".cierre-adjudicadas.pid"
MESES_REVISION = int(os.getenv("ADJ_MESES_REVISION", "6"))
TOPE_REZAGADAS_MIN = float(os.getenv("ADJ_TOPE_REZAGADAS_MIN", "120"))

# Tokens que el panel reconoce como fin del proceso.
TOK_FIN = "CIERRE ADJUDICADAS TERMINADO"
TOK_ERROR = "ERROR CRITICO"

_logs: list = []
_lineas: list[str] = []   # para el correo de resumen
_candado = None           # conexión que retiene GET_LOCK durante toda la corrida


def log(msg: str = "") -> None:
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    _lineas.append(linea)
    # Lanzado desde el panel, stdout ya va al mismo log: no duplicar.
    if not os.getenv("CIERRE_SIN_STDOUT"):
        print(linea, flush=True)
    for fh in _logs:
        try:
            fh.write(linea + "\n")
            fh.flush()
        except OSError:
            pass


def _otro_proceso_vivo() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid in (os.getpid(), os.getppid()):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def _sello(mes: str) -> Path:
    return TEMP_DIR / f".cierre_adjudicadas_{mes.replace('-', '')}.done"


def _guardar_reporte(mes: str, modo: str, resultados: list, acciones: list, inicio: str) -> None:
    d = DIR_TRABAJO / "reportes"
    d.mkdir(parents=True, exist_ok=True)
    datos = {
        "mes": mes, "modo": modo, "inicio": inicio, "fin": v.ahora(),
        "estado": v.resumen_estados(resultados),
        "resultados": [r.dict() for r in resultados],
        "acciones": acciones,
    }
    (d / f"mes_{mes}.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1, default=str))


def validar_mes(C, P, O, m: str, mes_cerrado: bool, api_mes: dict | None = None,
                v4: v.Resultado | None = None) -> tuple[list, dict]:
    """Corre las validaciones de un mes. Devuelve (resultados, esperado_consultas)."""
    res = [v.v1_dias_descargados(C, m)]
    res.append(v.v2_listado_vs_api(C, m, api_mes))
    res.append(v.v3_actas(C, m))
    res.append(v4 or v.Resultado("V4", "Actas completas (ítems adjudicados API == BD)",
                                 resumen="sin consultar la API (usar 'validar con API')"))
    res.append(v.v5_prime_licitaciones(C, P, m))
    res.append(v.v6_resumen(C, P, m, mes_cerrado))
    esp = v.esperado_consultas(C, m)
    res.append(v.v7_oc(O, m, esp))
    res.append(v.v8_prime_consulta5(P, m, esp))
    res.append(v.v9_fecha(P, m))
    for r in res:
        icono = {"ok": "OK   ", "falla": "FALLA", "aviso": "AVISO", "omitida": "  -  "}[r.estado]
        log(f"   [{icono}] {r.id} {r.titulo}: {r.resumen}")
    return res, esp


def universo_v4(C, mes: str, esp: dict) -> list[str]:
    """Licitaciones de `mes` que se verifican contra la API: las que entran a
    consulta5 (las que venden los reportes) y las marcadas sin filas."""
    i_adq = bd.COLS_LICITACIONES.index("ADQUISICION")
    estado = v.estado_actas(C, mes)
    return sorted({f[i_adq] for f in esp["c3"]} | set(estado["marcadas_sin_filas"]))


def correr(a) -> int:
    mes = a.mes
    meses = bd.meses_hacia_atras(mes, 1 if a.solo_mes else MESES_REVISION)
    ini_mes, fin_mes = bd.rango_mes(mes)
    cache = DIR_TRABAJO / "cache"
    inicio = v.ahora()
    log("=" * 78)
    log(f"CIERRE ADJUDICADAS COMPLETO · modo={a.modo} · mes={mes} · revisión {meses[-1]} → {meses[0]}")
    log("=" * 78)

    if a.modo == "cierre" and _sello(mes).exists() and not a.forzar:
        log(f"El mes {mes} ya está cerrado ({_sello(mes).read_text().strip()}). Usar --forzar para repetir.")
        return 0

    try:
        C, P, O = bd.clasico(), bd.prime(), bd.oc()
    except Exception as exc:  # noqa: BLE001
        log(f"{TOK_ERROR}: sin conexión a MySQL ({exc})")
        return 2
    log("Conexiones OK: clásico, prime y OC")

    # Candado en MySQL (clásico): vale entre contenedores y servidores, a
    # diferencia del PID (el cron corre en un contenedor aparte del panel).
    global _candado
    _candado = bd.clasico()
    if bd.uno(_candado, "SELECT GET_LOCK('cierre_adjudicadas_completo', 0)")[0] != 1:
        log(f"Ya hay otro cierre de adjudicadas corriendo (candado MySQL tomado); no se lanza otro. {TOK_FIN}")
        return 0

    acciones: list = []
    api_mes = None
    v4_mes = None

    if a.modo in ("validar-api", "cierre"):
        log(f"-- Listado de {mes} en la API de Mercado Público")
        api_mes = v.api_listado_mes(mes, cache / f"api_listado_{mes}.json", log)

    if a.modo == "cierre":
        # 1-2. listado y actas
        nuevas = pasos.listado_desde_api(C, api_mes, log)
        acciones.append({"paso": "listado desde API", "mes": mes, "nuevas": nuevas})
        pasos.reconciliar_marcas(C, bd.rango_mes(meses[-1])[0], fin_mes, log)
        pend = pasos.pendientes_actas(C, ini_mes, fin_mes)
        log(f"-- Actas pendientes de {mes}: {len(pend)}")
        acciones.append({"paso": "actas del mes", "mes": mes, **pasos.descargar_actas(C, pend, None, log)})
        if not a.sin_rezagadas and len(meses) > 1:
            dia_antes = (date.fromisoformat(ini_mes) - timedelta(days=1)).isoformat()
            rez = pasos.pendientes_actas(C, bd.rango_mes(meses[-1])[0], dia_antes)
            log(f"-- Actas rezagadas {meses[-1]} → {dia_antes}: {len(rez)} (tope {TOPE_REZAGADAS_MIN:.0f} min)")
            acciones.append({"paso": "actas rezagadas", **pasos.descargar_actas(C, rez, TOPE_REZAGADAS_MIN, log)})

    if a.modo in ("validar-api", "cierre") and not a.sin_verificar_actas:
        # 3. actas del mes contra la API
        esp_mes = v.esperado_consultas(C, mes)
        codigos = universo_v4(C, mes, esp_mes)
        log(f"-- Verificación de actas de {mes} contra la API ({len(codigos)} licitaciones)")
        v4_mes = v.v4_actas_vs_api(C, codigos, cache / f"api_actas_ok_{mes}.json", log)
        if a.modo == "cierre" and v4_mes.detalle.get("incompletas"):
            redescargar = [(x["codigo"], x.get("fecha_adjudicacion") or ini_mes) for x in v4_mes.detalle["incompletas"]]
            log(f"   {len(redescargar)} actas no calzan con la API: se vuelven a bajar")
            acciones.append({"paso": "actas incompletas", **pasos.descargar_actas(C, redescargar, None, log)})
            v4_mes = v.v4_actas_vs_api(C, codigos, cache / f"api_actas_ok_{mes}.json", log)
        log(f"   [{v4_mes.estado.upper():5}] V4 {v4_mes.resumen}")

    if a.modo == "cierre":
        # 4. publicación por mes (el mes cerrado primero)
        for m in meses:
            log(f"-- Publicación {m}")
            n = pasos.sincronizar_prime(C, P, m, log)
            if n:
                acciones.append({"paso": "prime Licitaciones", "mes": m, "filas": n})
            if m == mes:
                pasos.escribir_resumen(C, P, m, [t for t, *_ in reglas.TABLAS_RESUMEN], DIR_TRABAJO / "respaldos", log)
                acciones.append({"paso": "resumen 6 tablas", "mes": m})
            else:
                r6 = v.v6_resumen(C, P, m, mes_cerrado=False)
                recalc, copiar = [], []
                for t, _c, _s, foto in reglas.TABLAS_RESUMEN:
                    comp = r6.detalle["tablas"][t]
                    if not foto and not comp["clasico"]["iguales"]:
                        recalc.append(t)
                    elif not comp["prime"]["iguales"]:
                        copiar.append(t)
                if recalc:
                    pasos.escribir_resumen(C, P, m, recalc, DIR_TRABAJO / "respaldos", log)
                    acciones.append({"paso": "resumen recalculado", "mes": m, "tablas": recalc})
                if copiar:
                    pasos.copiar_resumen_a_prime(C, P, m, copiar, DIR_TRABAJO / "respaldos", log)
                    acciones.append({"paso": "resumen copiado a prime", "mes": m, "tablas": copiar})
            esp = v.esperado_consultas(C, m)
            if m == mes or v.v7_oc(O, m, esp).estado != "ok" or v.v8_prime_consulta5(P, m, esp).estado != "ok":
                pasos.recalcular_oc(C, O, m, esp, log)
                pasos.publicar_consulta5(O, P, m, esp, log)
                acciones.append({"paso": "OC consulta1/3/5 + prime consulta5", "mes": m, "filas": len(esp["c5"])})
            else:
                log(f"   OC y prime consulta5 {m}: ya calzan con la regla")
            pasos.asegurar_fecha(P, m, log)

    # 5. validación final
    estado_mes = "ok"
    por_mes: dict = {}
    for m in meses:
        log(f"-- Validación {m}")
        res, _ = validar_mes(C, P, O, m, mes_cerrado=(m == mes and a.modo == "cierre"),
                             api_mes=api_mes if m == mes else None, v4=v4_mes if m == mes else None)
        _guardar_reporte(m, a.modo, res, [x for x in acciones if x.get("mes") in (m, None)], inicio)
        por_mes[m] = res
        if m == mes:
            estado_mes = v.resumen_estados(res)
    for cn in (C, P, O):
        try:
            cn.close()
        except Exception:  # noqa: BLE001
            pass

    if a.modo == "cierre" and estado_mes != "falla":
        _sello(mes).write_text(f"cerrado {v.ahora()} · estado {estado_mes}")
        log(f"Mes {mes} CERRADO ({estado_mes}). Sello escrito.")
    if a.modo == "cierre":
        resumen_acciones = [{k: (len(val) if isinstance(val, list) and k in ("nuevas", "incompletas") else val)
                             for k, val in x.items()} for x in acciones]
        correo.enviar(f"Cierre Adjudicadas {mes}: {estado_mes.upper()}",
                      correo.cuerpo_html(mes, estado_mes, por_mes, resumen_acciones, _lineas), log)
    log(f"{TOK_FIN} · mes {mes} · estado {estado_mes.upper()}")
    return 1 if estado_mes == "falla" else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Cierre Adjudicadas completo")
    ap.add_argument("--modo", choices=("validar", "validar-api", "cierre"), default="validar")
    ap.add_argument("--mes", metavar="YYYY-MM", default=None, help="mes a cerrar (por defecto el anterior)")
    ap.add_argument("--forzar", action="store_true", help="repite el cierre aunque exista el sello")
    ap.add_argument("--solo-mes", action="store_true", help="no revisa ni repara meses anteriores")
    ap.add_argument("--sin-rezagadas", action="store_true", help="no baja actas pendientes de meses anteriores")
    ap.add_argument("--sin-verificar-actas", action="store_true",
                    help="no compara las actas con la API (V4); útil para repetir un cierre ya verificado")
    a = ap.parse_args()
    a.mes = a.mes or bd.mes_anterior()
    try:
        bd.rango_mes(a.mes)
    except ValueError:
        ap.error("--mes debe ser YYYY-MM")

    DIR_TRABAJO.mkdir(parents=True, exist_ok=True)
    (DIR_TRABAJO / "logs").mkdir(exist_ok=True)
    otro = _otro_proceso_vivo()
    if otro:
        print(f"Ya hay un cierre de adjudicadas corriendo (PID {otro}); no se lanza otro.")
        return 0
    PID_FILE.write_text(str(os.getpid()))
    _logs.append(open(LOG_PANEL, "w", encoding="utf-8"))
    _logs.append(open(DIR_TRABAJO / "logs" / f"{datetime.now():%Y%m%d_%H%M%S}_{a.modo}_{a.mes}.log", "w", encoding="utf-8"))
    t0 = time.monotonic()
    try:
        rc = correr(a)
    except Exception as exc:  # noqa: BLE001
        log(f"{TOK_ERROR}: {type(exc).__name__}: {exc}")
        for linea in traceback.format_exc().splitlines():
            log("   " + linea)
        log(f"{TOK_FIN} · con error")
        rc = 2
    finally:
        log(f"Duración: {(time.monotonic() - t0) / 60:.1f} min")
        for fh in _logs:
            fh.close()
        if _candado is not None:
            try:
                _candado.close()  # libera GET_LOCK
            except Exception:  # noqa: BLE001
                pass
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
