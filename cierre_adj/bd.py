"""Conexiones a clásico, prime y OC, y utilidades de copia entre servidores.

OC es el MySQL local de gestor_oc: solo acepta root@localhost / root@127.0.0.1
(medido 2026-09-16: desde la red de Docker responde 1130 "Host not allowed").
Por eso en producción el proceso corre con la red del host y MYSQL_OC_HOST
es 127.0.0.1. Desde un equipo de desarrollo se llega con un túnel SSH a
127.0.0.1:3306 del servidor (entra como root@localhost).
"""

from __future__ import annotations

import calendar
import math
import os
from datetime import date, datetime
from decimal import Decimal

import pymysql

from config import config

DB_ADJ = "licitaciones_adjudicadas_diarias"
DB_DIARIAS = "licitaciones_diarias_total_farma"
DB_RESUMEN = "resumen_licitaciones_adjudicadas"
DB_TM = "test_matias"
DB_BASE = "0001_td_oc"

# Columnas de licitaciones_adjudicadas_diarias.Licitaciones (iguales en los 3
# servidores, verificado 2026-09-16) en el orden de la tabla.
COLS_LICITACIONES = (
    "ADQUISICION", "CLIENTE", "RUT", "DIRECCION", "COMUNA", "REGION",
    "FECHAPUBLICACION", "FECHACIERRE", "PRODUCTO", "CODONU", "DESCONU",
    "ESPCOMPRADOR", "CANTIDAD", "NUMPROD", "PROVEEDORES", "RUTPROVEEDORES",
    "RAZONSOCIALPROVEEDORES", "ESPECIFICACIONPROVEEDORES",
    "MONTOUNITARIOPROVEEDOR", "CANTADJUDICADA", "NETOADJUDICADO", "ESTADO",
    "FECHAADJUDICACION", "FECHASQL", "TIPOMONEDA", "SUCURSALPROVEEDOR",
    "LINK_ACTA", "FECHASQLCIERRE", "FECHASQLPUBLICACION", "DESCRIPCION",
)

# Columnas de test_matias.consulta5 (iguales en OC y prime).
COLS_CONSULTA5 = (
    "ADQUISICION", "CLIENTE", "RUT", "DIRECCION", "FECHAPUBLICACION",
    "FECHACIERRE", "PRODUCTO", "CODONU", "DESCONU", "ESPCOMPRADOR", "CANTIDAD",
    "NUMPROD", "RUTPROVEEDORES", "RAZONSOCIALPROVEEDORES",
    "ESPECIFICACIONPROVEEDORES", "MONTOUNITARIOPROVEEDOR", "CANTADJUDICADA",
    "NETOADJUDICADO", "ESTADO", "FECHAADJUDICACION", "FECHASQL", "TIPOMONEDA",
    "SUCURSALPROVEEDOR", "DESCRIPCION", "PACTIVO", "COMPOSICION",
    "PRESENTACION", "nombre_clasificador",
)

# El max_allowed_packet del OC es 16 MB: los lotes se cortan por bytes, no
# solo por filas (DESCRIPCION llega a 9.999 caracteres).
LOTE_FILAS = 500
LOTE_BYTES = 2 * 1024 * 1024  # caracteres; en utf8 hasta 3 bytes c/u -> < 8 MB por INSERT


def _conectar(host: str, port: int, user: str, password: str, database: str | None = None):
    return pymysql.connect(
        host=host, port=int(port), user=user, password=password, database=database,
        charset="utf8mb4", connect_timeout=20, read_timeout=900, write_timeout=900,
        autocommit=False,
    )


def clasico(database: str | None = None):
    return _conectar(config.db_host, config.db_port, config.db_user, config.db_password, database)


def prime(database: str | None = None):
    pw = os.getenv("MYSQL_PRIME_PASSWORD", "")
    if not pw:
        raise RuntimeError("Falta MYSQL_PRIME_PASSWORD en el .env")
    return _conectar(os.getenv("MYSQL_PRIME_HOST", "10.0.0.68"),
                     int(os.getenv("MYSQL_PRIME_PORT", "8806")),
                     os.getenv("MYSQL_PRIME_USER", "root"), pw, database)


def oc(database: str | None = None):
    pw = os.getenv("MYSQL_OC_PASSWORD", "")
    if not pw:
        raise RuntimeError("Falta MYSQL_OC_PASSWORD en el .env")
    return _conectar(os.getenv("MYSQL_OC_HOST", "127.0.0.1"),
                     int(os.getenv("MYSQL_OC_PORT", "3306")),
                     os.getenv("MYSQL_OC_USER", "root"), pw, database)


def rango_mes(mes: str) -> tuple[str, str]:
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-<último día>')."""
    anio, m = (int(x) for x in mes.split("-"))
    return f"{anio:04d}-{m:02d}-01", f"{anio:04d}-{m:02d}-{calendar.monthrange(anio, m)[1]:02d}"


def mes_anterior(hoy: date | None = None, n: int = 1) -> str:
    hoy = hoy or date.today()
    total = hoy.year * 12 + (hoy.month - 1) - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def meses_hacia_atras(mes: str, n: int) -> list[str]:
    """[mes, mes-1, ..., mes-(n-1)]."""
    anio, m = (int(x) for x in mes.split("-"))
    total = anio * 12 + (m - 1)
    return [f"{(total - i) // 12:04d}-{(total - i) % 12 + 1:02d}" for i in range(n)]


def uno(cn, sql: str, params=None):
    with cn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def todos(cn, sql: str, params=None) -> list[tuple]:
    with cn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def insertar_lotes(cn, sql_prefijo: str, filas: list[tuple]) -> int:
    """INSERT multi-fila en lotes acotados por filas y bytes. NO hace commit.

    `sql_prefijo` es "INSERT [IGNORE] INTO t (c1,..,cn) VALUES" sin placeholders.
    Devuelve las filas afectadas que reporta MySQL."""
    if not filas:
        return 0
    ncols = len(filas[0])
    marca = "(" + ",".join(["%s"] * ncols) + ")"
    afectadas = 0
    lote: list[tuple] = []
    tam = 0
    with cn.cursor() as cur:
        for fila in filas:
            lote.append(fila)
            tam += sum(len(v) if isinstance(v, (str, bytes)) else 16 for v in fila)
            if len(lote) >= LOTE_FILAS or tam >= LOTE_BYTES:
                afectadas += cur.execute(f"{sql_prefijo} " + ",".join([marca] * len(lote)),
                                         [v for f in lote for v in f])
                lote, tam = [], 0
        if lote:
            afectadas += cur.execute(f"{sql_prefijo} " + ",".join([marca] * len(lote)),
                                     [v for f in lote for v in f])
    return afectadas


def en_lista(valores) -> tuple[str, list]:
    """Para `IN (...)`: devuelve ('%s,%s,...', lista)."""
    valores = list(valores)
    return ",".join(["%s"] * len(valores)), valores


def trozos(seq, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def normalizar(v, flotante_simple: bool = False):
    """Valor comparable entre MySQL 5.7 (OC, prime) y 8.0 (clásico).

    Los FLOAT se devuelven con distinta cantidad de decimales según versión:
    se comparan con 6 dígitos significativos; los DOUBLE con 12."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6g}" if flotante_simple else f"{v:.12g}"
    if isinstance(v, Decimal):
        return normalizar(float(v), flotante_simple)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)
