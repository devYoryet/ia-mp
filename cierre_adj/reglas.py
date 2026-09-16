"""Cálculo INDEPENDIENTE de lo que cada tabla del cierre debe contener.

Todo se calcula desde el clásico (fuente de verdad) y se compara fila a fila
contra lo escrito en clásico, OC y prime. Si coincide, hay certeza; si no,
el cierre repara ese mes.

Reglas (leídas de los procedimientos del OC y de cierre_batch.py del clásico,
2026-09-16):

  resumen_licitaciones_adjudicadas.<tabla>  por RUTPROVEEDORES:
      Items_Adjudicados    COUNT(ESTADO='Adjudicada')
      Valores_Adjudicados  ROUND(SUM(NETOADJUDICADO))
      Items_No_Adjudicados COUNT(RUTPROVEEDORES) - Items_Adjudicados
      Items_Postulados     COUNT(RUTPROVEEDORES)
      Resumen*            -> FECHASQL en el mes
      Resumen_publicados* -> FECHASQLPUBLICACION en el mes
      Resumen_cerrados*   -> FECHASQLCIERRE en el mes
      *_sin_cenabast      -> RUT <> '61.608.700-2'

  test_matias.consulta1 = rutproveedor de 0001_td_oc.Base
  test_matias.consulta3 = filas de Licitaciones del mes cuya ADQUISICION tiene
                          algún RUTPROVEEDORES en consulta1
  test_matias.consulta5 = DISTINCT consulta3 LEFT JOIN
                          licitaciones_diarias_total_farma.Licitaciones_diarias
                          ON adquisicion = Licitacion AND numprod = Item
                          (trae PACTIVO, COMPOSICION, PRESENTACION,
                          nombre_clasificador)
  Medido julio 2026: la regla da 55.756 filas / 947 licitaciones, igual que
  consulta5 en prime.
"""

from __future__ import annotations

import hashlib
from collections import Counter

from cierre_adj import bd

RUT_CENABAST = "61.608.700-2"
MESES_ES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
            "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")

# (tabla, columna de fecha, sin CENABAST, es "foto" del día del cierre)
# Publicados y cerrados crecen después del cierre (una licitación que cerró en
# mayo puede adjudicarse en julio): el batch original los calcula una vez, con
# los datos del día. Por eso una diferencia posterior en esas 4 tablas es un
# aviso, no una falla.
TABLAS_RESUMEN = (
    ("Resumen", "FECHASQL", False, False),
    ("Resumen_sin_cenabast", "FECHASQL", True, False),
    ("Resumen_publicados", "FECHASQLPUBLICACION", False, True),
    ("Resumen_publicados_sin_cenabast", "FECHASQLPUBLICACION", True, True),
    ("Resumen_cerrados", "FECHASQLCIERRE", False, True),
    ("Resumen_cerrados_sin_cenabast", "FECHASQLCIERRE", True, True),
)
COLS_RESUMEN = ("RUTPROVEEDORES", "Items_Adjudicados", "Valores_Adjudicados",
                "Items_No_Adjudicados", "Items_Postulados", "Mes", "Ano", "Fecha")

# Índice de CANTADJUDICADA (FLOAT) en cada tupla, para normalizar.
_FLOAT_LIC = bd.COLS_LICITACIONES.index("CANTADJUDICADA")
_FLOAT_C5 = bd.COLS_CONSULTA5.index("CANTADJUDICADA")


def nombre_mes(mes: str) -> str:
    anio, m = (int(x) for x in mes.split("-"))
    return f"{MESES_ES[m - 1]} {anio}"


def firma(filas, idx_float: int | None = None) -> Counter:
    """Multiconjunto de huellas de filas normalizadas (poca memoria)."""
    c: Counter = Counter()
    for f in filas:
        norm = tuple(bd.normalizar(v, i == idx_float) for i, v in enumerate(f))
        c[hashlib.blake2b(repr(norm).encode("utf-8"), digest_size=16).digest()] += 1
    return c


def comparar(esperado: Counter, real: Counter) -> dict:
    faltan = esperado - real
    sobran = real - esperado
    return {
        "esperadas": sum(esperado.values()),
        "reales": sum(real.values()),
        "faltan": sum(faltan.values()),
        "sobran": sum(sobran.values()),
        "iguales": not faltan and not sobran,
    }


# ------------------------------------------------------------------ resumen ---

def resumen_esperado(cn_clasico, mes: str, tabla: str) -> list[tuple]:
    col, sin_cenabast = next((c, s) for t, c, s, _ in TABLAS_RESUMEN if t == tabla)
    ini, fin = bd.rango_mes(mes)
    anio, m = (int(x) for x in mes.split("-"))
    extra = " AND RUT <> %s" if sin_cenabast else ""
    params = [MESES_ES[m - 1], anio, ini, ini, fin] + ([RUT_CENABAST] if sin_cenabast else [])
    return bd.todos(
        cn_clasico,
        f"""SELECT RUTPROVEEDORES,
                   COUNT(CASE WHEN ESTADO = 'Adjudicada' THEN 1 END),
                   ROUND(SUM(NETOADJUDICADO)),
                   COUNT(RUTPROVEEDORES) - COUNT(CASE WHEN ESTADO = 'Adjudicada' THEN 1 END),
                   COUNT(RUTPROVEEDORES), %s, %s, %s
            FROM {bd.DB_ADJ}.Licitaciones
            WHERE {col} BETWEEN %s AND %s{extra}
            GROUP BY RUTPROVEEDORES""",
        params,
    )


def resumen_actual(cn, mes: str, tabla: str) -> list[tuple]:
    ini, _ = bd.rango_mes(mes)
    return bd.todos(cn, f"SELECT {','.join(COLS_RESUMEN)} FROM {bd.DB_RESUMEN}.{tabla} WHERE Fecha = %s", (ini,))


# ------------------------------------------------------- consulta3 / consulta5 ---

def ruts_base(cn) -> set[str]:
    return {r[0] for r in bd.todos(cn, f"SELECT DISTINCT rutproveedor FROM {bd.DB_BASE}.Base") if r[0] is not None}


def consulta3_esperado(cn_clasico, mes: str) -> list[tuple]:
    """Filas que consulta3 debe tener para el mes.

    En dos pasos: el IN con subconsulta en una sola query tardaba 97 s en el
    clásico (julio); primero las licitaciones del mes con proveedor de Base y
    después sus filas por lotes."""
    ini, fin = bd.rango_mes(mes)
    ruts = ruts_base(cn_clasico)
    adqs = sorted({a for a, r in bd.todos(
        cn_clasico,
        f"SELECT ADQUISICION, RUTPROVEEDORES FROM {bd.DB_ADJ}.Licitaciones WHERE FECHASQL BETWEEN %s AND %s",
        (ini, fin)) if r in ruts})
    cols = ",".join(bd.COLS_LICITACIONES)
    filas: list[tuple] = []
    for trozo in bd.trozos(adqs, 300):
        marcas, vals = bd.en_lista(trozo)
        filas += bd.todos(
            cn_clasico,
            f"""SELECT {cols} FROM {bd.DB_ADJ}.Licitaciones
                WHERE FECHASQL BETWEEN %s AND %s AND ADQUISICION IN ({marcas})""",
            [ini, fin] + vals,
        )
    return filas


def _clave_join(licitacion, item) -> tuple:
    """Emula la comparación de MySQL: texto sin mayúsculas ni espacios finales,
    y Item (varchar) comparado como número contra NUMPROD (double)."""
    lic = (licitacion or "").rstrip().upper()
    try:
        num = float(str(item).strip())
    except (TypeError, ValueError):
        num = 0.0
    return lic, num


def clasificacion_diarias(cn_clasico, adquisiciones) -> dict:
    """(licitacion, item) -> (pactivo, composicion, presentacion, nombre_clasificador)."""
    out: dict = {}
    for trozo in bd.trozos(sorted(adquisiciones), 500):
        marcas, vals = bd.en_lista(trozo)
        for lic, item, p, c, pr, n in bd.todos(
            cn_clasico,
            f"""SELECT Licitacion, Item, pactivo, composicion, presentacion, nombre_clasificador
                FROM {bd.DB_DIARIAS}.Licitaciones_diarias WHERE Licitacion IN ({marcas})""",
            vals,
        ):
            out[_clave_join(lic, item)] = (p, c, pr, n)
    return out


_IDX_C5_DESDE_C3 = tuple(bd.COLS_LICITACIONES.index(c) for c in bd.COLS_CONSULTA5[:24])


def consulta5_esperado(cn_clasico, filas_c3: list[tuple]) -> list[tuple]:
    i_adq = bd.COLS_LICITACIONES.index("ADQUISICION")
    i_num = bd.COLS_LICITACIONES.index("NUMPROD")
    clasif = clasificacion_diarias(cn_clasico, {f[i_adq] for f in filas_c3})
    vistas: set = set()
    out: list[tuple] = []
    for f in filas_c3:
        fila = tuple(f[i] for i in _IDX_C5_DESDE_C3) + clasif.get(_clave_join(f[i_adq], f[i_num]), (None,) * 4)
        clave = tuple(bd.normalizar(v, i == _FLOAT_C5) for i, v in enumerate(fila))
        if clave not in vistas:  # DISTINCT del procedimiento
            vistas.add(clave)
            out.append(fila)
    return out


def consulta_actual(cn, tabla: str, mes: str) -> list[tuple]:
    ini, fin = bd.rango_mes(mes)
    cols = bd.COLS_LICITACIONES if tabla == "consulta3" else bd.COLS_CONSULTA5
    return bd.todos(cn, f"SELECT {','.join(cols)} FROM {bd.DB_TM}.{tabla} WHERE FECHASQL BETWEEN %s AND %s", (ini, fin))


def firma_c3(filas) -> Counter:
    return firma(filas, _FLOAT_LIC)


def firma_c5(filas) -> Counter:
    return firma(filas, _FLOAT_C5)
