#!/usr/bin/env python3
"""Monitor de salud del host gestor_oc — corre por cron cada 5 min.

Cada ejecución mide CPU, RAM, swap, disco y procesos clave (worker, panel,
backtest), y appendea una fila CSV en `/opt/ia-mp/monitor/salud.csv`. Sin
dependencias externas — solo stdlib + el python del host.

Después de unos días el CSV tiene N=288 muestras/día, suficiente para ver:
- ¿La RAM del worker crece monotonicamente (memory leak) o estabiliza?
- ¿Vale la pena el restart diario o no?
- ¿El CPU spike-ea o queda sostenido?
- ¿Algún container se reinicia solo (oom-kill)?

Setup en gestor_oc (una sola vez):
    mkdir -p /opt/ia-mp/monitor
    crontab -e
    # Agregar:
    */5 * * * * /usr/bin/python3 /opt/ia-mp/monitor_salud.py
"""

from __future__ import annotations

import csv
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

LOG = Path("/opt/ia-mp/monitor/salud.csv")
CONTAINERS = ("ia-mp-worker-1", "ia-mp-panel-1", "ia-mp-backtest-1")


def cpu_pct() -> float:
    """Uso CPU del HOST como % (snapshot de 0.5s sobre /proc/stat)."""
    def snap() -> list[int]:
        with open("/proc/stat") as f:
            return [int(x) for x in f.readline().split()[1:]]
    a = snap()
    time.sleep(0.5)
    b = snap()
    d = [b[i] - a[i] for i in range(len(a))]
    total = sum(d)
    idle = d[3]  # idle column
    return round((total - idle) / total * 100, 1) if total else 0.0


def mem_mb() -> dict:
    """Total / used / available / swap del host, en MB."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])  # kB
    return {
        "total": info["MemTotal"] // 1024,
        "available": info.get("MemAvailable", info["MemFree"]) // 1024,
        "used": (info["MemTotal"] - info.get("MemAvailable", info["MemFree"])) // 1024,
        "swap_used": (info["SwapTotal"] - info["SwapFree"]) // 1024,
    }


def disk_pct(path: str = "/") -> int:
    """% de uso del FS."""
    s = subprocess.check_output(["df", "-P", path]).decode().splitlines()[-1].split()
    return int(s[4].rstrip("%"))


def docker_stats() -> dict:
    """Por contenedor: %CPU y MEM_USAGE MB. Si docker no responde, {} vacío."""
    out: dict[str, dict] = {}
    try:
        # --no-stream para snapshot único; formato fijo, parseable
        raw = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            timeout=8,
        ).decode().strip().splitlines()
        for line in raw:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name, cpu, mem = parts
            try:
                cpu_f = float(cpu.rstrip("%"))
            except ValueError:
                cpu_f = 0.0
            # MEM_USAGE viene como "1.234GiB / 7.8GiB" — tomamos el primer número
            mem_str = mem.split("/")[0].strip()
            mem_mb = _to_mb(mem_str)
            out[name] = {"cpu": cpu_f, "mem_mb": mem_mb}
    except Exception:  # noqa: BLE001
        pass
    return out


def _to_mb(s: str) -> float:
    """'1.234GiB' / '512MiB' / '0B' → MB. Mejor esfuerzo."""
    s = s.strip()
    for suf, mul in (("GiB", 1024.0), ("MiB", 1.0),
                     ("KiB", 1.0 / 1024), ("B", 1.0 / (1024 * 1024)),
                     ("GB", 1000.0), ("MB", 1.0), ("kB", 1.0 / 1024)):
        if s.endswith(suf):
            try:
                return round(float(s[: -len(suf)]) * mul, 1)
            except ValueError:
                return 0.0
    return 0.0


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    nueva = not LOG.exists()
    cpu = cpu_pct()
    mem = mem_mb()
    disk = disk_pct("/")
    docks = docker_stats()
    fila = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "host_cpu_pct": cpu,
        "host_mem_total_mb": mem["total"],
        "host_mem_used_mb": mem["used"],
        "host_mem_available_mb": mem["available"],
        "host_swap_used_mb": mem["swap_used"],
        "host_disk_root_pct": disk,
    }
    for c in CONTAINERS:
        d = docks.get(c, {})
        fila[f"{c}_cpu_pct"] = d.get("cpu", 0.0)
        fila[f"{c}_mem_mb"] = d.get("mem_mb", 0.0)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fila.keys()))
        if nueva:
            w.writeheader()
        w.writerow(fila)


if __name__ == "__main__":
    main()
