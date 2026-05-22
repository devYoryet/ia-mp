"""Conexión MySQL al servidor clásico (10.0.0.69)."""

from __future__ import annotations

import logging
import time

import pymysql
from pymysql.cursors import DictCursor

from config import config

_log = logging.getLogger("db")

# Errores de pymysql que indican una conexión perdida (corte transitorio de
# red/VPN), no un error de SQL — son los que vale la pena reintentar.
_ERRORES_CONEXION = (pymysql.err.OperationalError, pymysql.err.InterfaceError)


def _abrir(database: str | None = None, autocommit: bool = False):
    return pymysql.connect(
        host=config.db_host,
        port=config.db_port,
        user=config.db_user,
        password=config.db_password,
        database=database or config.db_name,
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=90,
        write_timeout=90,
        autocommit=autocommit,
        cursorclass=DictCursor,
    )


def conectar(database: str | None = None):
    """Conexión nueva — uso puntual (p. ej. el panel web, una sola por request)."""
    return _abrir(database)


_worker_conn = None


def conexion_worker():
    """Conexión COMPARTIDA y persistente del worker.

    El worker es un proceso secuencial: usa UNA sola conexión para todas las
    filas, en vez de abrir una por fila. Eso evita saturar el servidor y que un
    firewall bloquee la IP por exceso de conexiones. Se reconecta sola si cae."""
    global _worker_conn
    if _worker_conn is not None:
        try:
            _worker_conn.ping(reconnect=True)
            return _worker_conn
        except Exception:
            try:
                _worker_conn.close()
            except Exception:
                pass
            _worker_conn = None
    _worker_conn = _abrir(autocommit=True)
    return _worker_conn


def reintentar(fn, intentos: int = 6, espera: int = 5, espera_max: int = 60):
    """Ejecuta `fn()`; ante una caída de conexión a MySQL (corte transitorio de
    red/VPN) descarta la conexión compartida y reintenta con espera creciente.

    Así un corte breve no aborta el worker: una pasada larga (p. ej. el backtest)
    sobrevive a un blip de un par de minutos. Si se agotan los intentos, propaga
    el error — que lo maneje el llamador (circuito de corte del worker)."""
    global _worker_conn
    for intento in range(1, intentos + 1):
        try:
            return fn()
        except _ERRORES_CONEXION as exc:
            if intento >= intentos:
                raise
            _log.warning(
                "MySQL inaccesible (%s) — reintento %d/%d en %ds",
                (exc.args[0] if exc.args else exc), intento, intentos - 1, espera,
            )
            try:
                if _worker_conn is not None:
                    _worker_conn.close()
            except Exception:
                pass
            _worker_conn = None
            time.sleep(espera)
            espera = min(espera * 2, espera_max)
