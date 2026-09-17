#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pasos que faltaban en los módulos legacy de carga (/legacy), medidos 2026-09-16.

IMPORTACIONES  (bin/importaciones_completo.py los encadena tras ImportOC.py)
    ImportOC.py solo deja el Excel en licitaciones_diarias_total_farma.importaciones_YYYY_MM,
    tabla de paso que nadie lee. Prime lee:
        importaciones_total_mes.YYYYMM            (todas las filas del mes)
        importaciones_total_farma.Importaciones   (Campo144 IN (29,30), por datFecha)
        importaciones_total_mes.fecha             (id, fecha DATE, fecha_natural 'Julio 2026';
                                                   prime toma la fila de mayor id como último mes)
    Julio 2026 quedó bien porque alguien corrió esos pasos a mano; enero 2026 quedó
    incompleto en prime (310.000 de 380.834 filas). Aquí: clásico primero, prime
    después, conteos iguales y la fila de `fecha` al final solo si todo validó.
    Verificado: el INSERT ... SELECT a farma reproduce exacto las 8.265 filas de julio.

ADJUDICACIONES (bin/estructura_adj.py lo llama al final)
    estructura_adj.py escribe en prime analisis_precios.Base; el clásico solo recibía
    filas en adquisiciones_validadas, así que su Base quedó en feb-2026 (le faltaban
    63.995 filas, Id > 920.462). Prime es la fuente (prime edita esa Base): se copian a
    la Base del clásico las filas con Id mayor al máximo del clásico.

CENABAST
    Solo faltaban 12 filas de cenabast.Fecha en el clásico (abr-2025 a mar-2026).

Uso manual:
    python cargas_legacy.py importaciones --periodo 2026-07            # publica clásico + prime
    python cargas_legacy.py importaciones --periodo 2026-01 --solo-prime
    python cargas_legacy.py adjudicaciones                              # Base prime -> clásico
    python cargas_legacy.py cenabast-fechas                             # Fecha prime -> clásico
Todos idempotentes. --dry-run informa sin escribir.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

import pymysql

from cierre_adj import bd

MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre")
LOTE_LECTURA = 20000


class ErrorCarga(Exception):
    pass


def periodo_desde_nombre(nombre: str) -> date | None:
    """'Importaciones Julio 2026 v1.xlsm' -> date(2026, 7, 1). Mes en español + año."""
    base = os.path.basename(nombre or "").lower()
    base = base.replace("setiembre", "septiembre")
    mes = next((i + 1 for i, m in enumerate(MESES) if m.lower() in base), None)
    anio = re.search(r"\b(20\d{2})\b", base)
    return date(int(anio.group(1)), mes, 1) if mes and anio else None


def _columnas(cn, db: str, tabla: str) -> list[str]:
    return [c for (c,) in bd.todos(cn, """SELECT COLUMN_NAME FROM information_schema.columns
                                          WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""", (db, tabla))]


def _existe(cn, db: str, tabla: str) -> bool:
    return bool(bd.uno(cn, "SELECT COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                       (db, tabla))[0])


def _contar(cn, sql: str, params=None) -> int:
    return int(bd.uno(cn, sql, params)[0])


def _copiar_entre_servidores(origen, destino, sql_select: str, params, db_dest: str, tabla_dest: str,
                             columnas: list[str], log) -> int:
    """Lee por streaming desde `origen` e inserta en `destino` por lotes (commit por lote)."""
    lista = ",".join(f"`{c}`" for c in columnas)
    total = 0
    with origen.cursor(pymysql.cursors.SSCursor) as cur:
        cur.execute(sql_select, params)
        while True:
            filas = cur.fetchmany(LOTE_LECTURA)
            if not filas:
                break
            bd.insertar_lotes(destino, f"INSERT INTO `{db_dest}`.`{tabla_dest}` ({lista}) VALUES", filas)
            destino.commit()
            total += len(filas)
            log(f"   {db_dest}.{tabla_dest}: {total:,} filas copiadas")
    return total


# ============================================================ IMPORTACIONES ===

DB_PASO = "licitaciones_diarias_total_farma"
DB_MES = "importaciones_total_mes"
DB_FARMA = "importaciones_total_farma"


def _tabla_mes_anterior(cn, yyyymm: str) -> str | None:
    tablas = [t for (t,) in bd.todos(cn, """SELECT TABLE_NAME FROM information_schema.tables
                                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME REGEXP '^[0-9]{6}$'""", (DB_MES,))]
    previas = sorted(t for t in tablas if t < yyyymm)
    return previas[-1] if previas else None


def _swap(cn, db: str, nueva: str, final: str, log) -> None:
    with cn.cursor() as cur:
        if _existe(cn, db, final):
            cur.execute(f"DROP TABLE IF EXISTS `{db}`.`{final}_reemplazada`")
            cur.execute(f"RENAME TABLE `{db}`.`{final}` TO `{db}`.`{final}_reemplazada`, `{db}`.`{nueva}` TO `{db}`.`{final}`")
            cur.execute(f"DROP TABLE `{db}`.`{final}_reemplazada`")
            log(f"   {db}.{final}: reemplazada por la versión nueva")
        else:
            cur.execute(f"RENAME TABLE `{db}`.`{nueva}` TO `{db}`.`{final}`")


def _validar_mes(cn, yyyymm: str, periodo: date, esperadas: int, tabla: str | None = None) -> None:
    tabla = tabla or yyyymm
    n, fechas, fmin = bd.uno(cn, f"SELECT COUNT(*), COUNT(DISTINCT datFecha), MIN(datFecha) FROM `{DB_MES}`.`{tabla}`")
    if int(n) != esperadas:
        raise ErrorCarga(f"{DB_MES}.{tabla}: {n} filas, se esperaban {esperadas}")
    if int(fechas) != 1 or str(fmin)[:10] != periodo.isoformat():
        raise ErrorCarga(f"{DB_MES}.{tabla}: datFecha no es única {periodo} ({fechas} valores, mínimo {fmin})")


def _mes_clasico(C, periodo: date, log, dry: bool) -> int:
    yyyymm, paso = periodo.strftime("%Y%m"), f"importaciones_{periodo.strftime('%Y_%m')}"
    if not _existe(C, DB_PASO, paso):
        raise ErrorCarga(f"no existe la tabla de paso {DB_PASO}.{paso} (¿corrió ImportOC.py?)")
    n_paso = _contar(C, f"SELECT COUNT(*) FROM `{DB_PASO}`.`{paso}`")
    if not n_paso:
        raise ErrorCarga(f"la tabla de paso {DB_PASO}.{paso} está vacía")
    cols_paso = _columnas(C, DB_PASO, paso)
    ref = _tabla_mes_anterior(C, yyyymm)
    if ref and _columnas(C, DB_MES, ref) != cols_paso:
        raise ErrorCarga(f"la estructura de {paso} no es igual a la del mes anterior {DB_MES}.{ref}; no se publica")
    log(f"   estructura OK: {paso} = {DB_MES}.{ref} ({len(cols_paso)} columnas); {n_paso:,} filas")
    if _existe(C, DB_MES, yyyymm) and _contar(C, f"SELECT COUNT(*) FROM `{DB_MES}`.`{yyyymm}`") == n_paso:
        _validar_mes(C, yyyymm, periodo, n_paso)
        log(f"   clásico {DB_MES}.{yyyymm}: ya publicado ({n_paso:,} filas)")
        return n_paso
    if dry:
        log(f"   [dry-run] se publicaría clásico {DB_MES}.{yyyymm} con {n_paso:,} filas")
        return n_paso
    nueva = f"{yyyymm}_nuevo"
    lista = ",".join(f"`{c}`" for c in cols_paso)
    with C.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{DB_MES}`.`{nueva}`")
        cur.execute(f"CREATE TABLE `{DB_MES}`.`{nueva}` LIKE `{DB_PASO}`.`{paso}`")
        cur.execute(f"INSERT INTO `{DB_MES}`.`{nueva}` ({lista}) SELECT {lista} FROM `{DB_PASO}`.`{paso}`")
    C.commit()
    _validar_mes(C, yyyymm, periodo, n_paso, nueva)
    _swap(C, DB_MES, nueva, yyyymm, log)
    log(f"   clásico {DB_MES}.{yyyymm}: {n_paso:,} filas publicadas")
    return n_paso


def _farma_clasico(C, periodo: date, log, dry: bool) -> int:
    yyyymm = periodo.strftime("%Y%m")
    cols = _columnas(C, DB_FARMA, "Importaciones")
    if sorted(cols) != sorted(_columnas(C, DB_MES, yyyymm)):
        raise ErrorCarga(f"{DB_FARMA}.Importaciones y {DB_MES}.{yyyymm} no tienen las mismas columnas")
    lista = ",".join(f"`{c}`" for c in cols)
    esperadas = _contar(C, f"SELECT COUNT(*) FROM `{DB_MES}`.`{yyyymm}` WHERE Campo144 IN (29, 30)")
    actuales = _contar(C, f"SELECT COUNT(*) FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (periodo.isoformat(),))
    if actuales == esperadas:
        log(f"   clásico {DB_FARMA}.Importaciones {periodo}: ya tiene {esperadas:,} filas")
        return esperadas
    if dry:
        log(f"   [dry-run] clásico farma {periodo}: {actuales:,} -> {esperadas:,} filas")
        return esperadas
    try:
        with C.cursor() as cur:
            cur.execute(f"DELETE FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (periodo.isoformat(),))
            cur.execute(f"INSERT INTO `{DB_FARMA}`.Importaciones ({lista}) SELECT {lista} FROM `{DB_MES}`.`{yyyymm}` WHERE Campo144 IN (29, 30)")
        n = _contar(C, f"SELECT COUNT(*) FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (periodo.isoformat(),))
        if n != esperadas:
            raise ErrorCarga(f"clásico farma {periodo}: {n} filas, se esperaban {esperadas}")
        C.commit()
    except Exception:
        C.rollback()
        raise
    log(f"   clásico {DB_FARMA}.Importaciones {periodo}: {actuales:,} -> {esperadas:,} filas")
    return esperadas


def _fecha(cn, servidor: str, periodo: date, log, dry: bool) -> None:
    nombre = f"{MESES[periodo.month - 1]} {periodo.year}"
    if _contar(cn, f"SELECT COUNT(*) FROM `{DB_MES}`.fecha WHERE fecha = %s", (periodo.isoformat(),)):
        log(f"   {servidor} {DB_MES}.fecha: '{nombre}' ya existe")
        return
    ultimo = bd.uno(cn, f"SELECT MAX(fecha) FROM `{DB_MES}`.fecha")[0]
    if ultimo and str(ultimo)[:10] > periodo.isoformat():
        # prime toma la fila de mayor id como "último mes": agregar un mes antiguo lo rompería.
        log(f"   AVISO {servidor}: {periodo} es anterior al último mes registrado ({ultimo}); "
            f"no se agrega a {DB_MES}.fecha (hacerlo a mano si corresponde)")
        return
    if dry:
        log(f"   [dry-run] {servidor} {DB_MES}.fecha: se agregaría '{nombre}'")
        return
    with cn.cursor() as cur:
        cur.execute(f"INSERT INTO `{DB_MES}`.fecha (fecha, fecha_natural) VALUES (%s, %s)", (periodo.isoformat(), nombre))
    cn.commit()
    log(f"   {servidor} {DB_MES}.fecha: agregado '{nombre}'")


def _mes_prime(C, P, periodo: date, log, dry: bool) -> int:
    yyyymm = periodo.strftime("%Y%m")
    n_c = _contar(C, f"SELECT COUNT(*) FROM `{DB_MES}`.`{yyyymm}`")
    cols = _columnas(C, DB_MES, yyyymm)
    existe = _existe(P, DB_MES, yyyymm)
    if existe and _contar(P, f"SELECT COUNT(*) FROM `{DB_MES}`.`{yyyymm}`") == n_c:
        _validar_mes(P, yyyymm, periodo, n_c)
        log(f"   prime {DB_MES}.{yyyymm}: ya igual al clásico ({n_c:,} filas)")
        return n_c
    # Plantilla: la propia tabla de prime si existe (conserva su estructura), si no la del mes anterior.
    plantilla = yyyymm if existe else _tabla_mes_anterior(P, yyyymm)
    if not plantilla or sorted(_columnas(P, DB_MES, plantilla)) != sorted(cols):
        raise ErrorCarga(f"prime {DB_MES}.{plantilla}: columnas distintas a las del clásico {yyyymm}; no se publica")
    if dry:
        n_p = _contar(P, f"SELECT COUNT(*) FROM `{DB_MES}`.`{yyyymm}`") if existe else 0
        log(f"   [dry-run] prime {DB_MES}.{yyyymm}: {n_p:,} -> {n_c:,} filas (plantilla {plantilla})")
        return n_c
    nueva = f"{yyyymm}_nuevo"
    with P.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{DB_MES}`.`{nueva}`")
        cur.execute(f"CREATE TABLE `{DB_MES}`.`{nueva}` LIKE `{DB_MES}`.`{plantilla}`")
    P.commit()
    lista = ",".join(f"`{c}`" for c in cols)
    _copiar_entre_servidores(C, P, f"SELECT {lista} FROM `{DB_MES}`.`{yyyymm}`", None, DB_MES, nueva, cols, log)
    _validar_mes(P, yyyymm, periodo, n_c, nueva)
    _swap(P, DB_MES, nueva, yyyymm, log)
    log(f"   prime {DB_MES}.{yyyymm}: {n_c:,} filas publicadas")
    return n_c


def _farma_prime(C, P, periodo: date, log, dry: bool) -> int:
    cols = _columnas(C, DB_FARMA, "Importaciones")
    if sorted(cols) != sorted(c for c in _columnas(P, DB_FARMA, "Importaciones")):
        raise ErrorCarga("prime importaciones_total_farma.Importaciones: columnas distintas al clásico")
    f = periodo.isoformat()
    n_c = _contar(C, f"SELECT COUNT(*) FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (f,))
    n_p = _contar(P, f"SELECT COUNT(*) FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (f,))
    if n_p == n_c:
        log(f"   prime {DB_FARMA}.Importaciones {periodo}: ya igual al clásico ({n_c:,} filas)")
        return n_c
    if dry:
        log(f"   [dry-run] prime farma {periodo}: {n_p:,} -> {n_c:,} filas")
        return n_c
    lista = ",".join(f"`{c}`" for c in cols)
    filas = bd.todos(C, f"SELECT {lista} FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (f,))
    # DESCRIPCION es NOT NULL en ambos servidores.
    i_desc = cols.index("DESCRIPCION")
    filas = [tuple("" if (i == i_desc and v is None) else v for i, v in enumerate(fila)) for fila in filas]
    try:
        with P.cursor() as cur:
            cur.execute(f"DELETE FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (f,))
        bd.insertar_lotes(P, f"INSERT INTO `{DB_FARMA}`.Importaciones ({lista}) VALUES", filas)
        n = _contar(P, f"SELECT COUNT(*) FROM `{DB_FARMA}`.Importaciones WHERE datFecha = %s", (f,))
        if n != n_c:
            raise ErrorCarga(f"prime farma {periodo}: {n} filas, se esperaban {n_c}")
        P.commit()
    except Exception:
        P.rollback()
        raise
    log(f"   prime {DB_FARMA}.Importaciones {periodo}: {n_p:,} -> {n_c:,} filas")
    return n_c


def publicar_importaciones(periodo: date, solo_prime: bool = False, dry: bool = False, log=print) -> dict:
    C, P = bd.clasico(), bd.prime()
    try:
        log(f"== Importaciones {periodo:%Y-%m}: publicación {'solo prime' if solo_prime else 'clásico + prime'}")
        if not solo_prime:
            _mes_clasico(C, periodo, log, dry)
            _farma_clasico(C, periodo, log, dry)
        n_mes = _mes_prime(C, P, periodo, log, dry)
        n_farma = _farma_prime(C, P, periodo, log, dry)
        # Fecha al final y solo con los datos ya validados en ambos servidores.
        if not solo_prime:
            _fecha(C, "clásico", periodo, log, dry)
        _fecha(P, "prime", periodo, log, dry)
        log(f"   Importaciones {periodo:%Y-%m} OK: {n_mes:,} filas del mes y {n_farma:,} farma, iguales en clásico y prime")
        return {"mes": n_mes, "farma": n_farma}
    finally:
        C.close()
        P.close()


# =========================================================== ADJUDICACIONES ===

def sincronizar_base_adjudicaciones(dry: bool = False, log=print) -> int:
    """analisis_precios.Base prime -> clásico: filas con Id mayor al máximo del clásico."""
    C, P = bd.clasico(), bd.prime()
    try:
        cols = _columnas(C, "analisis_precios", "Base")
        if cols != _columnas(P, "analisis_precios", "Base"):
            raise ErrorCarga("analisis_precios.Base: columnas distintas entre clásico y prime")
        max_c = bd.uno(C, "SELECT COALESCE(MAX(Id), 0) FROM analisis_precios.Base")[0]
        max_p = bd.uno(P, "SELECT COALESCE(MAX(Id), 0) FROM analisis_precios.Base")[0]
        faltan = _contar(P, "SELECT COUNT(*) FROM analisis_precios.Base WHERE Id > %s", (max_c,))
        log(f"== Adjudicaciones: Base clásico max Id {max_c:,} · prime max Id {max_p:,} · faltan {faltan:,} filas")
        if faltan and not dry:
            lista = ",".join(f"`{c}`" for c in cols)
            _copiar_entre_servidores(P, C, f"SELECT {lista} FROM analisis_precios.Base WHERE Id > %s ORDER BY Id",
                                     (max_c,), "analisis_precios", "Base", cols, log)
        elif faltan:
            log(f"   [dry-run] se copiarían {faltan:,} filas a la Base del clásico")
        # Validación: filas por mes iguales en los últimos 24 meses.
        q = """SELECT LEFT(FechaSQL, 7), COUNT(*) FROM analisis_precios.Base
               WHERE FechaSQL >= DATE_SUB(CURDATE(), INTERVAL 24 MONTH) GROUP BY 1"""
        por_mes_c, por_mes_p = dict(bd.todos(C, q)), dict(bd.todos(P, q))
        distintos = {m: (por_mes_c.get(m, 0), n) for m, n in por_mes_p.items() if por_mes_c.get(m, 0) != n}
        if distintos and not dry:
            raise ErrorCarga(f"Base clásico != prime en {len(distintos)} meses: {dict(list(distintos.items())[:6])}")
        log(f"   Base clásico == prime en los últimos 24 meses ({sum(por_mes_p.values()):,} filas)" if not distintos
            else f"   [dry-run] meses distintos hoy: {dict(list(distintos.items())[:8])}")
        return faltan
    finally:
        C.close()
        P.close()


# ================================================================= CENABAST ===

def completar_fechas_cenabast(dry: bool = False, log=print) -> int:
    """cenabast.Fecha: agrega al clásico los periodos que prime tiene y el clásico no."""
    C, P = bd.clasico(), bd.prime()
    try:
        en_c = {f for (f,) in bd.todos(C, "SELECT datFecha FROM cenabast.Fecha")}
        faltan = [(n, f) for n, f in bd.todos(P, "SELECT strNombreFecha, datFecha FROM cenabast.Fecha ORDER BY datFecha")
                  if f not in en_c]
        log(f"== Cenabast: {len(faltan)} periodos de Fecha faltan en el clásico: {[f for _, f in faltan]}")
        if faltan and not dry:
            with C.cursor() as cur:
                cur.executemany("INSERT INTO cenabast.Fecha (strNombreFecha, datFecha) VALUES (%s, %s)", faltan)
            C.commit()
        return len(faltan)
    finally:
        C.close()
        P.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pasos finales de los módulos legacy de carga")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("importaciones")
    imp.add_argument("--periodo", required=True, metavar="YYYY-MM")
    imp.add_argument("--solo-prime", action="store_true")
    imp.add_argument("--dry-run", action="store_true")
    adj = sub.add_parser("adjudicaciones")
    adj.add_argument("--dry-run", action="store_true")
    cen = sub.add_parser("cenabast-fechas")
    cen.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "importaciones":
            anio, mes = (int(x) for x in a.periodo.split("-"))
            publicar_importaciones(date(anio, mes, 1), a.solo_prime, a.dry_run)
        elif a.cmd == "adjudicaciones":
            sincronizar_base_adjudicaciones(a.dry_run)
        else:
            completar_fechas_cenabast(a.dry_run)
    except ErrorCarga as exc:
        print(f"ERROR CRÍTICO: {exc}")
        return 1
    print("FINALIZADO EXITOSAMENTE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
