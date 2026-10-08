"""Acceso a MySQL de la Fase 2, con tres candados para no tocar nada existente.

1. Usuario propio (`ia_fase2`): MySQL le da SELECT sobre el schema y escritura
   SÓLO en clasificador_f2_resultado / _onu_nombre / _corridas. Aunque el
   código tuviera un bug, MySQL rechaza (error 1142) cualquier escritura en
   compra_agil, Licitaciones_diarias, clasificador_ia_log, etc.
2. Dos conexiones: la de LECTURA abre la sesión en modo `READ ONLY`, así que ni
   siquiera puede escribir en las tablas propias; la de ESCRITURA sólo acepta
   sentencias cuyo destino sea una tabla propia (`validar_escritura`), y lo
   verifica ANTES de mandar nada a MySQL.
3. El servicio se niega a correr con 'root' (`exigir_usuario_restringido`).

Las lecturas usan READ COMMITTED + autocommit (sin snapshots largos) y
MAX_EXECUTION_TIME: una consulta que se pase de tiempo la corta MySQL.

Configuración por entorno: F2_MYSQL_HOST, F2_MYSQL_PORT, F2_MYSQL_USER,
F2_MYSQL_PASSWORD, F2_MYSQL_DB. Sin python-dotenv: el container recibe el
entorno por env_file; en local se puede apuntar F2_ENV_FILE a un archivo.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("fase2.bd")

TABLAS_PROPIAS = frozenset({
    "clasificador_f2_resultado",
    "clasificador_f2_onu_nombre",
    "clasificador_f2_corridas",
})
_DESTINO = re.compile(
    r"^\s*(?:INSERT\s+(?:IGNORE\s+)?INTO|UPDATE)\s+`?(?:[A-Za-z0-9_]+`?\.`?)?([A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)
_SOLO_LECTURA = re.compile(r"^\s*(SELECT|SHOW|EXPLAIN)\b", re.IGNORECASE)
_TIEMPO_MAX_MS = int(os.getenv("F2_MAX_EXECUTION_MS", "60000"))


def validar_escritura(sql: str) -> str:
    """Devuelve la tabla destino si es propia; si no, PermissionError.
    Sólo INSERT/UPDATE (nunca DELETE, REPLACE, DDL)."""
    m = _DESTINO.match(sql)
    if not m:
        raise PermissionError(f"Fase 2: sentencia de escritura no permitida: {sql[:80]!r}")
    tabla = m.group(1)
    if tabla not in TABLAS_PROPIAS:
        raise PermissionError(f"Fase 2: escritura en tabla ajena bloqueada: {tabla}")
    return tabla


def validar_lectura(sql: str) -> None:
    if not _SOLO_LECTURA.match(sql):
        raise PermissionError(f"Fase 2: la conexión de lectura sólo hace SELECT: {sql[:80]!r}")


def _cargar_env_file() -> None:
    ruta = os.getenv("F2_ENV_FILE")
    if not ruta or not os.path.exists(ruta):
        return
    for linea in open(ruta, encoding="utf-8"):
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parametros(prefijo: str = "F2_MYSQL") -> dict:
    _cargar_env_file()
    faltan = [k for k in ("HOST", "USER", "PASSWORD") if not os.getenv(f"{prefijo}_{k}")]
    if faltan:
        raise RuntimeError(f"Faltan variables {', '.join(prefijo + '_' + k for k in faltan)}")
    return {
        "host": os.environ[f"{prefijo}_HOST"],
        "port": int(os.getenv(f"{prefijo}_PORT", "3306")),
        "user": os.environ[f"{prefijo}_USER"],
        "password": os.environ[f"{prefijo}_PASSWORD"],
        "database": os.getenv(f"{prefijo}_DB", "licitaciones_diarias_total_farma"),
    }


def _conectar(p: dict, solo_lectura: bool):
    import pymysql  # import diferido: los tests del motor no necesitan pymysql
    conn = pymysql.connect(
        host=p["host"], port=p["port"], user=p["user"], password=p["password"],
        database=p["database"], charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=15,
        read_timeout=180, write_timeout=180,
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={_TIEMPO_MAX_MS}")
        if solo_lectura:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
    return conn


class BD:
    """Par de conexiones (lectura / escritura) con los candados de arriba."""

    def __init__(self, p: dict | None = None):
        self.p = p or parametros()
        self._lec = None
        self._esc = None

    # -- conexión -----------------------------------------------------------
    def _lectura(self):
        if self._lec is None:
            self._lec = _conectar(self.p, solo_lectura=True)
        else:
            self._lec.ping(reconnect=True)
        return self._lec

    def _escritura(self):
        if self._esc is None:
            self._esc = _conectar(self.p, solo_lectura=False)
        else:
            self._esc.ping(reconnect=True)
        return self._esc

    def cerrar(self) -> None:
        for c in (self._lec, self._esc):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
        self._lec = self._esc = None

    # -- lectura ------------------------------------------------------------
    def todos(self, sql: str, params=()) -> list[dict]:
        validar_lectura(sql)
        with self._lectura().cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def uno(self, sql: str, params=()) -> dict | None:
        filas = self.todos(sql, params)
        return filas[0] if filas else None

    # -- escritura (sólo tablas propias, en una transacción) -----------------
    def transaccion(self, sentencias: list[tuple[str, tuple]]) -> None:
        """Ejecuta [(sql, params), ...] en UNA transacción. Valida TODAS antes
        de empezar: si una apunta a una tabla ajena, no se ejecuta ninguna."""
        for sql, _ in sentencias:
            validar_escritura(sql)
        if not sentencias:
            return
        conn = self._escritura()
        conn.begin()
        try:
            with conn.cursor() as cur:
                for sql, params in sentencias:
                    cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def lock(self, nombre: str = "clasificador_f2_barrido") -> bool:
        """Lock con nombre de MySQL: evita dos barridos a la vez (servicio y
        una corrida manual). No toca ninguna tabla."""
        r = self.uno("SELECT GET_LOCK(%s, 0) AS ok", (nombre,))
        return bool(r and r["ok"])

    def unlock(self, nombre: str = "clasificador_f2_barrido") -> None:
        self.todos("SELECT RELEASE_LOCK(%s) AS ok", (nombre,))

    def usuario_actual(self) -> str:
        return self.uno("SELECT CURRENT_USER() AS u")["u"]

    def exigir_usuario_restringido(self) -> None:
        u = self.usuario_actual()
        if u.split("@")[0] == "root":
            raise RuntimeError("La fase 2 no corre con root: usar el usuario restringido ia_fase2.")
