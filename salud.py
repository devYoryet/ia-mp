#!/usr/bin/env python3
"""Chequeo de salud del sistema — corre por cron cada hora en gestor_oc.

Mide TODO en una pantalla: containers, host (RAM/CPU/disco), MySQL clasico
(conexiones, tamaños), accuracy del backtest acumulado, distribución por
etapa, costo de Claude vs presupuesto, falsos negativos en producción.

Tres outputs en /opt/ia-mp/monitor/:
  - salud.txt   : el reporte humano-legible más reciente (sobreescribe)
  - salud.jsonl : una línea JSON por ejecución (historial completo)
  - ALERTAS.txt : se appendea SOLO si algo está en rojo (revisar primero)

Guardrail de costo: si el gasto de Claude últimos 7d supera BUDGET_BACKTEST_USD,
PARA el container `backtest` automáticamente (`docker compose stop backtest`) y
escribe alerta. Para reanudar después del análisis: `docker compose start backtest`.

Setup en gestor_oc (una sola vez):
    apt-get install -y python3-pymysql
    crontab -e
    0 * * * * /usr/bin/python3 /opt/ia-mp/salud.py 2>>/opt/ia-mp/monitor/salud.err
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pymysql  # apt: python3-pymysql

# --- Config ----------------------------------------------------------------
DIR = Path("/opt/ia-mp/monitor")
DIR.mkdir(parents=True, exist_ok=True)

# Credenciales MySQL — la imagen del worker tiene /opt/ia-mp/.env con esto;
# si no, falla limpio.
_ENV = {}
try:
    for line in Path("/opt/ia-mp/.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            _ENV[k.strip()] = v.strip().strip('"').strip("'")
except FileNotFoundError:
    pass

MYSQL = dict(
    host=_ENV.get("MYSQL_HOST", "10.0.0.69"),
    port=int(_ENV.get("MYSQL_PORT", "3306")),
    user=_ENV.get("MYSQL_USER", "root"),
    password=_ENV.get("MYSQL_PASSWORD", ""),
    database=_ENV.get("MYSQL_DB", "licitaciones_diarias_total_farma"),
    charset="utf8mb4",
    connect_timeout=10,
)

BUDGET_TOTAL = float(_ENV.get("BUDGET_USD", "350"))
# Presupuesto reservado para el experimento de backtest paralelo de 7 días.
# Se compara contra el costo neto últimos 7d (test + ajuste, excluye prod).
BUDGET_BACKTEST = float(_ENV.get("BUDGET_BACKTEST_USD", "65"))
# Umbrales diarios de PRODUCCIÓN — alerta + crítica. Proyectados a mes:
#   $12/día × 30 = $360/mes  → al filo del presupuesto, ALERTA
#   $20/día × 30 = $600/mes  → fuera de control, ALERTA CRÍTICA (no apaga prod
#                              automáticamente: producción es producción, lo
#                              decide el humano viendo el contexto)
PROD_24H_ALERTA = float(_ENV.get("PROD_24H_ALERTA_USD", "12"))
PROD_24H_CRITICA = float(_ENV.get("PROD_24H_CRITICA_USD", "20"))


# --- Helpers ---------------------------------------------------------------
def _sh(cmd: str, timeout: int = 8) -> str:
    """Comando shell, captura stdout. '' si falla — para no abortar el report."""
    try:
        return subprocess.check_output(
            cmd, shell=True, timeout=timeout, text=True, stderr=subprocess.STDOUT
        )
    except Exception:  # noqa: BLE001
        return ""


def _conn():
    return pymysql.connect(**MYSQL, cursorclass=pymysql.cursors.DictCursor)


def _query(sql: str, args=()) -> list:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    except Exception as e:  # noqa: BLE001
        return [{"_err": str(e)}]


# --- Métricas: HOST gestor_oc ---------------------------------------------
def metricas_host() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])  # kB
    disk_pct = int(_sh("df -P / | tail -1").split()[4].rstrip("%") or 0)
    # CPU 1-min loadavg / cores
    la = float(open("/proc/loadavg").read().split()[0])
    cores = os.cpu_count() or 1
    return {
        "mem_total_mb": info["MemTotal"] // 1024,
        "mem_available_mb": info.get("MemAvailable", info["MemFree"]) // 1024,
        "swap_used_mb": (info["SwapTotal"] - info["SwapFree"]) // 1024,
        "disk_root_pct": disk_pct,
        "load_1min": la,
        "cores": cores,
        "load_per_core": round(la / cores, 2),
    }


# --- Métricas: CONTAINERS --------------------------------------------------
def metricas_containers() -> dict:
    """Estado de los 3 containers + sus restart counts."""
    esperados = ("ia-mp-worker-1", "ia-mp-panel-1", "ia-mp-backtest-1")
    out = {}
    ps = _sh("docker ps -a --format '{{.Names}}\\t{{.Status}}'").strip().splitlines()
    estados = dict(line.split("\t", 1) for line in ps if "\t" in line)
    for c in esperados:
        st = estados.get(c, "MISSING")
        out[c] = {"status": st, "up": st.startswith("Up")}
        # restart count — si el container se está reiniciando solo (oom-kill) es señal mala
        restarts = _sh(f"docker inspect -f '{{{{.RestartCount}}}}' {c}").strip()
        out[c]["restart_count"] = int(restarts) if restarts.isdigit() else None
    return out


# --- Métricas: MYSQL clasico ----------------------------------------------
def metricas_mysql() -> dict:
    out = {}
    # connections, max_connections
    var = _query("SHOW GLOBAL STATUS WHERE Variable_name IN "
                 "('Threads_connected', 'Max_used_connections', 'Aborted_connects')")
    out["status"] = {r["Variable_name"]: r["Value"] for r in var if "_err" not in r}
    maxc = _query("SHOW VARIABLES LIKE 'max_connections'")
    if maxc and "Value" in maxc[0]:
        out["max_connections"] = int(maxc[0]["Value"])
    # tamaños de las tablas relevantes
    tab = _query(
        "SELECT table_name AS t, table_rows AS rows, "
        "ROUND(data_length/1024/1024,1) AS data_mb, "
        "ROUND(index_length/1024/1024,1) AS idx_mb "
        "FROM information_schema.tables "
        "WHERE table_schema = %s "
        "AND table_name LIKE 'clasificador_ia_%'",
        (MYSQL["database"],),
    )
    out["tablas"] = tab
    return out


# --- Métricas: BACKTEST accuracy & cobertura ------------------------------
def metricas_backtest() -> dict:
    """Acierto y cobertura del backtest acumulado. Compara últimas 24h vs 7d
    para detectar drift (si 24h < 7d significa que el modelo viene peor que
    el promedio acumulado)."""
    out = {}
    out["totales_24h"] = _query(
        "SELECT COUNT(*) n, "
        "SUM(coincide_interes) ci, "
        "SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp, "
        "SUM(costo_usd) costo "
        "FROM clasificador_ia_backtest WHERE creado_en >= NOW() - INTERVAL 24 HOUR"
    )[0]
    out["totales_7d"] = _query(
        "SELECT COUNT(*) n, "
        "SUM(coincide_interes) ci, "
        "SUM(coincide_pactivo) cp, COUNT(coincide_pactivo) ncp, "
        "SUM(costo_usd) costo "
        "FROM clasificador_ia_backtest WHERE creado_en >= NOW() - INTERVAL 7 DAY"
    )[0]
    # distribución por método (últimas 24h) — para confirmar que modelo_pactivo aporta
    out["por_metodo_24h"] = _query(
        "SELECT ia_metodo, COUNT(*) n, "
        "ROUND(AVG(coincide_interes)*100,1) acc_int, "
        "ROUND(SUM(coincide_pactivo)/COUNT(coincide_pactivo)*100,1) acc_pact, "
        "ROUND(SUM(costo_usd),4) costo "
        "FROM clasificador_ia_backtest WHERE creado_en >= NOW() - INTERVAL 24 HOUR "
        "GROUP BY ia_metodo ORDER BY n DESC"
    )
    out["ultimo_registro"] = _query(
        "SELECT MAX(creado_en) ts FROM clasificador_ia_backtest"
    )[0]
    return out


# --- Métricas: COSTO Claude ------------------------------------------------
def metricas_costo() -> dict:
    out = {}
    out["acumulado"] = float(_query(
        "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos"
    )[0]["c"])
    out["ult_24h_test"] = float(_query(
        "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos "
        "WHERE creado_en >= NOW() - INTERVAL 24 HOUR AND contexto = 'test'"
    )[0]["c"])
    out["ult_7d_test"] = float(_query(
        "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos "
        "WHERE creado_en >= NOW() - INTERVAL 7 DAY AND contexto = 'test'"
    )[0]["c"])
    out["ult_24h_prod"] = float(_query(
        "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos "
        "WHERE creado_en >= NOW() - INTERVAL 24 HOUR AND contexto = 'produccion'"
    )[0]["c"])
    out["ult_7d_prod"] = float(_query(
        "SELECT IFNULL(SUM(costo_usd),0) c FROM clasificador_ia_costos "
        "WHERE creado_en >= NOW() - INTERVAL 7 DAY AND contexto = 'produccion'"
    )[0]["c"])
    # Patrón horario producción últimas 24h — para ver de un vistazo si hubo
    # un pico fuera de horario laboral (8-22h es lo normal).
    out["por_hora_prod_24h"] = _query(
        "SELECT DATE_FORMAT(creado_en,'%H') h, COUNT(*) n, "
        "ROUND(SUM(costo_usd),3) c FROM clasificador_ia_costos "
        "WHERE creado_en >= NOW() - INTERVAL 24 HOUR AND contexto='produccion' "
        "GROUP BY h ORDER BY h"
    )
    return out


# --- Métricas: PRODUCCIÓN backlog y feedback humano -----------------------
def metricas_produccion() -> dict:
    out = {}
    for t in ("compra_agil", "Licitaciones_diarias"):
        pend = _query(
            f"SELECT COUNT(*) n FROM `{t}` WHERE estado_gestor IS NULL "
            f"AND (nombre_clasificador IS NULL OR nombre_clasificador='')"
        )
        out[f"{t}_pendientes"] = pend[0]["n"] if pend and "n" in pend[0] else None
    # falsos negativos: humano corrigió o descartó lo que el bot proponía
    fn = _query(
        "SELECT COUNT(*) n FROM clasificador_ia_log "
        "WHERE revisado=1 AND feedback_correcto=0 "
        "AND revisado_en >= NOW() - INTERVAL 7 DAY"
    )
    out["fn_7d"] = fn[0]["n"] if fn and "n" in fn[0] else None
    rev = _query(
        "SELECT COUNT(*) n FROM clasificador_ia_log "
        "WHERE revisado=1 AND revisado_en >= NOW() - INTERVAL 7 DAY"
    )
    out["revisadas_7d"] = rev[0]["n"] if rev and "n" in rev[0] else None
    return out


# --- Evaluación: cuáles métricas están en rojo ----------------------------
def evaluar(snap: dict) -> list[str]:
    """Devuelve lista de alertas (texto humano). Vacía si todo OK."""
    alertas: list[str] = []
    h = snap["host"]
    if h["mem_available_mb"] < 800:
        alertas.append(f"RAM available CRÍTICA: {h['mem_available_mb']} MB (umbral 800)")
    # Umbral swap 2500 MB — con 3 containers + modelo de pactivo en RAM (~700MB
    # cada uno) y el caché de queries de MySQL, un poco de swap es esperable. La
    # señal de problema es swap CRECIENDO sin parar, no el valor absoluto.
    if h["swap_used_mb"] > 2500:
        alertas.append(f"Swap alto: {h['swap_used_mb']} MB (umbral 2500)")
    if h["disk_root_pct"] > 85:
        alertas.append(f"Disco /: {h['disk_root_pct']}% (umbral 85%)")
    if h["load_per_core"] > 2.5:
        alertas.append(f"Load avg {h['load_1min']} ({h['load_per_core']} por core, umbral 2.5)")

    for c, info in snap["containers"].items():
        if not info["up"]:
            alertas.append(f"Container {c} CAÍDO: {info['status']}")
        if (info.get("restart_count") or 0) > 3:
            alertas.append(f"Container {c} reinició {info['restart_count']} veces (oom?)")

    mysql = snap["mysql"]
    if "max_connections" in mysql:
        usadas = int(mysql["status"].get("Threads_connected", 0))
        if usadas > 0.7 * mysql["max_connections"]:
            alertas.append(f"MySQL conexiones: {usadas}/{mysql['max_connections']} (>70%)")

    costo_test_7d = snap["costo"]["ult_7d_test"]
    if costo_test_7d > BUDGET_BACKTEST:
        alertas.append(
            f"COSTO BACKTEST 7d: ${costo_test_7d:.2f} > presupuesto ${BUDGET_BACKTEST} "
            f"→ se va a parar el container backtest"
        )

    # Vigilancia de PRODUCCIÓN — alerta (no apaga). Producción la decide el
    # humano, no un script automático. Pero queremos enterarnos rápido si
    # algo se descarrila.
    costo_prod_24h = snap["costo"]["ult_24h_prod"]
    if costo_prod_24h > PROD_24H_CRITICA:
        alertas.append(
            f"COSTO PRODUCCIÓN 24h: ${costo_prod_24h:.2f} > CRÍTICO ${PROD_24H_CRITICA} "
            f"(proyección ${costo_prod_24h * 30:.0f}/mes — fuera de presupuesto)"
        )
    elif costo_prod_24h > PROD_24H_ALERTA:
        alertas.append(
            f"Producción 24h: ${costo_prod_24h:.2f} > alerta ${PROD_24H_ALERTA} "
            f"(proyección ${costo_prod_24h * 30:.0f}/mes — al filo del presupuesto)"
        )

    # drift accuracy: si últimas 24h - 7d < -3pt, algo viene peor
    bt = snap["backtest"]
    if bt["totales_24h"]["ncp"] and bt["totales_7d"]["ncp"]:
        a24 = (bt["totales_24h"]["cp"] or 0) / bt["totales_24h"]["ncp"] * 100
        a7 = (bt["totales_7d"]["cp"] or 0) / bt["totales_7d"]["ncp"] * 100
        if a24 < a7 - 3:
            alertas.append(f"Drift accuracy pact: 24h={a24:.1f}% vs 7d={a7:.1f}% (cae >3pt)")

    # último backtest hace > 30 min → backtest container colgado
    ult = bt.get("ultimo_registro", {}).get("ts")
    if ult and (datetime.now() - ult) > timedelta(minutes=30):
        alertas.append(f"Último backtest hace {datetime.now()-ult} (umbral 30 min)")

    return alertas


# --- Render texto ---------------------------------------------------------
def render(snap: dict, alertas: list[str]) -> str:
    h = snap["host"]
    cs = snap["containers"]
    mysql = snap["mysql"]
    bt = snap["backtest"]
    costo = snap["costo"]
    prod = snap["produccion"]

    def _fila(d: dict, key: str, fmt="{}", suf=""):
        v = d.get(key)
        return f"{fmt.format(v)}{suf}" if v is not None else "—"

    out = [f"=== SALUD CLASIFICADOR IA · {snap['ts']} ===", ""]

    if alertas:
        out.append("⚠ ALERTAS:")
        for a in alertas:
            out.append(f"  • {a}")
        out.append("")
    else:
        out.append("✓ Todo en verde")
        out.append("")

    out.append("HOST gestor_oc")
    out.append(f"  RAM available: {h['mem_available_mb']:,} MB · Swap usada: {h['swap_used_mb']:,} MB")
    out.append(f"  Disco /: {h['disk_root_pct']}%  ·  Load: {h['load_1min']} ({h['load_per_core']}/core)")
    out.append("")

    out.append("CONTAINERS")
    for name, info in cs.items():
        marca = "✓" if info["up"] else "✗"
        rest = f" (restarts={info['restart_count']})" if info.get('restart_count') else ""
        out.append(f"  {marca} {name}: {info['status']}{rest}")
    out.append("")

    out.append("MYSQL clasico")
    if "status" in mysql:
        st = mysql["status"]
        out.append(f"  conexiones: {st.get('Threads_connected','—')}/{mysql.get('max_connections','—')}"
                   f"  ·  pico histórico: {st.get('Max_used_connections','—')}"
                   f"  ·  aborted: {st.get('Aborted_connects','—')}")
    out.append("  tablas del clasificador:")
    for r in mysql.get("tablas", []):
        if "_err" in r: continue
        out.append(f"    {r['t']:<32} rows={r['rows']:>10,}  data={r['data_mb'] or 0:>6} MB  idx={r['idx_mb'] or 0:>6} MB")
    out.append("")

    out.append("BACKTEST")
    t24 = bt["totales_24h"]
    t7 = bt["totales_7d"]
    if t24["ncp"]:
        out.append(f"  Últimas 24h: {t24['n']:,} filas · acierto interés {(t24['ci'] or 0)/t24['n']*100:.1f}%"
                   f" · acierto pact {(t24['cp'] or 0)/t24['ncp']*100:.1f}% (sobre {t24['ncp']})"
                   f" · costo ${float(t24['costo'] or 0):.2f}")
    if t7["ncp"]:
        out.append(f"  Últimos 7d : {t7['n']:,} filas · acierto interés {(t7['ci'] or 0)/t7['n']*100:.1f}%"
                   f" · acierto pact {(t7['cp'] or 0)/t7['ncp']*100:.1f}% (sobre {t7['ncp']})"
                   f" · costo ${float(t7['costo'] or 0):.2f}")
    out.append("  por método (24h):")
    for r in bt.get("por_metodo_24h", [])[:8]:
        if "_err" in r: continue
        out.append(f"    {str(r['ia_metodo']):<26} n={r['n']:<6} int={r['acc_int']}%  pact={r['acc_pact']}%  ${float(r['costo'] or 0):.4f}")
    out.append("")

    out.append("COSTO Claude")
    out.append(f"  Acumulado total      : ${costo['acumulado']:.2f} / ${BUDGET_TOTAL} mensual")
    out.append(f"  Backtest últimos 24h : ${costo['ult_24h_test']:.2f}")
    out.append(f"  Backtest últimos 7d  : ${costo['ult_7d_test']:.2f} / ${BUDGET_BACKTEST} reservado")
    out.append(f"  Producción últimos 24h: ${costo['ult_24h_prod']:.2f}  "
               f"(proyección ${costo['ult_24h_prod']*30:.0f}/mes vs ${BUDGET_TOTAL:.0f})")
    out.append(f"  Producción últimos 7d : ${costo['ult_7d_prod']:.2f}")
    # Patrón horario: muestra si el costo de prod se concentró fuera de horario
    # laboral (sospechoso) o respeta el ciclo 8-22h del scraping (normal).
    if costo.get("por_hora_prod_24h"):
        out.append("  Producción por hora (24h, solo horas con actividad):")
        for r in costo["por_hora_prod_24h"]:
            if (r.get('n') or 0) == 0:
                continue
            out.append(f"    {r['h']}h  {r['n']:>4} calls  ${float(r['c'] or 0):.3f}")
    out.append("")

    out.append("PRODUCCIÓN")
    out.append(f"  Pendientes compra_agil: {_fila(prod, 'compra_agil_pendientes', '{:,}')}")
    out.append(f"  Pendientes Licitaciones: {_fila(prod, 'Licitaciones_diarias_pendientes', '{:,}')}")
    if prod.get("revisadas_7d"):
        ratio_fn = (prod['fn_7d'] or 0) / prod['revisadas_7d'] * 100
        out.append(f"  Revisadas 7d: {prod['revisadas_7d']:,} · falsos negativos: "
                   f"{prod['fn_7d']:,} ({ratio_fn:.1f}%)")
    out.append("")

    return "\n".join(out)


# --- Guardrails — acciones automáticas -------------------------------------
def acciones_guardrail(snap: dict, alertas: list[str]) -> list[str]:
    """Ejecuta acciones correctivas automáticas y devuelve qué hizo."""
    hechas: list[str] = []
    # Cost guardrail: si test 7d > presupuesto y backtest está vivo, lo detiene
    if snap["costo"]["ult_7d_test"] > BUDGET_BACKTEST:
        bt_up = snap["containers"]["ia-mp-backtest-1"]["up"]
        if bt_up:
            r = _sh("cd /opt/ia-mp && docker compose stop backtest 2>&1", timeout=30)
            hechas.append(f"docker compose stop backtest  →  {r.strip() or 'OK'}")
    return hechas


# --- Main ------------------------------------------------------------------
def main() -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    snap = {
        "ts": ts,
        "host": metricas_host(),
        "containers": metricas_containers(),
        "mysql": metricas_mysql(),
        "backtest": metricas_backtest(),
        "costo": metricas_costo(),
        "produccion": metricas_produccion(),
    }
    alertas = evaluar(snap)
    hechas = acciones_guardrail(snap, alertas)
    if hechas:
        alertas.append("[ACCIONES] " + " | ".join(hechas))

    texto = render(snap, alertas)
    (DIR / "salud.txt").write_text(texto)
    # historial — una línea JSON por corrida (orjson hubiera ahorrado deps; stdlib basta)
    with open(DIR / "salud.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "alertas": alertas, "snap": snap}, default=str) + "\n")
    # alertas — solo se appendea si hubo
    if alertas:
        with open(DIR / "ALERTAS.txt", "a") as f:
            f.write(f"\n--- {ts} ---\n")
            for a in alertas:
                f.write(f"• {a}\n")

    # Print al cron output (se redirige a salud.err si hay error, sino al void)
    print(texto)


if __name__ == "__main__":
    main()
