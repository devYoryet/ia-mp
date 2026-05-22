#!/usr/bin/env python3
"""Monitor del servidor `clasico` durante el run de producción del Clasificador IA.

Cada INTERVALO revisa carga, disco y MySQL de clasico. Si el servidor se degrada
—carga alta sostenida o disco casi lleno— DISPARA: detiene el worker del
clasificador (pkill) y escribe una alarma. Es el circuito de seguridad para no
repetir lo que pasó con el sistema viejo, que agotó el servidor.

Uso:  python3 monitor.py     (corre en loop hasta que se lo mata)
Log:  /tmp/monitor_clasico.log     Alarma: /tmp/ALARMA_CLASICO.txt
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
import time
from pathlib import Path

_BASE = Path("/home/yoryetdev/Conexiones_env")
sys.path.insert(0, str(_BASE / "Conexiones_ssh"))
sys.path.insert(0, str(_BASE / "Clasificador_IA"))

import pymysql  # noqa: E402
from config import config  # noqa: E402
from ssh_exec import run as ssh_run  # noqa: E402

INTERVALO = 180          # segundos entre chequeos
LOAD_MAX = 18.0          # carga 1-min (clasico tiene 12 núcleos; normal ~1)
DISCO_MAX = 98           # % de uso de disco
BREACHES_PARA_DISPARAR = 2  # chequeos malos seguidos antes de disparar
LOG = "/tmp/monitor_clasico.log"
ALARMA = "/tmp/ALARMA_CLASICO.txt"


def _log(linea: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"{ts} {linea}\n")


def mysql_status() -> str:
    try:
        cn = pymysql.connect(
            host=config.db_host, port=config.db_port, user=config.db_user,
            password=config.db_password, connect_timeout=10, read_timeout=10,
        )
        with cn.cursor() as cur:
            cur.execute(
                "SHOW GLOBAL STATUS WHERE Variable_name IN "
                "('Threads_connected','Threads_running')"
            )
            d = {k: v for k, v in cur.fetchall()}
        cn.close()
        return f"conn={d.get('Threads_connected','?')} run={d.get('Threads_running','?')}"
    except Exception as e:  # noqa: BLE001
        return f"MySQL inaccesible ({type(e).__name__})"


def disparar(motivo: str) -> None:
    _log(f"*** DISPARADOR: {motivo} — DETENIENDO el worker ***")
    subprocess.run(["pkill", "-f", "worker.py"], capture_output=True)
    with open(ALARMA, "w") as f:
        f.write(f"{datetime.datetime.now()}  ALARMA: {motivo}\n")


def main() -> None:
    _log(f"monitor iniciado (LOAD_MAX={LOAD_MAX}, DISCO_MAX={DISCO_MAX}%)")
    disparado = False
    breaches = 0
    while True:
        try:
            out, _, _ = ssh_run("clasico", "uptime; df -h / | tail -1", timeout=30)
            ml = re.search(r"load average:\s*([\d.]+)", out)
            md = re.search(r"(\d+)%", out)
            load1 = float(ml.group(1)) if ml else -1.0
            disco = int(md.group(1)) if md else -1
        except Exception as e:  # noqa: BLE001
            # SSH caído suele ser la VPN, no sobrecarga del servidor — no se dispara.
            _log(f"clasico inaccesible por SSH ({type(e).__name__}) — sin disparar (¿VPN?)")
            time.sleep(INTERVALO)
            continue

        mysql = mysql_status()
        malo = (load1 > LOAD_MAX) or (0 <= DISCO_MAX <= disco)
        breaches = breaches + 1 if malo else 0
        estado = "OK" if not malo else f"DEGRADADO ({breaches}/{BREACHES_PARA_DISPARAR})"
        _log(f"load1={load1} disco={disco}% mysql[{mysql}] -> {estado}")

        if not disparado and breaches >= BREACHES_PARA_DISPARAR:
            motivo = (f"carga {load1} > {LOAD_MAX}" if load1 > LOAD_MAX
                      else f"disco {disco}% >= {DISCO_MAX}%")
            disparar(motivo)
            disparado = True
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
