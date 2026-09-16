"""Pasos del cierre que ESCRIBEN. Cada uno es idempotente y verifica antes de
confirmar; si la verificación no calza hace rollback y lanza.

Orden (lo arma cierre_adjudicadas_completo.py):
    listado_desde_api   -> clásico listado_api
    descargar_actas     -> clásico Licitaciones (+ listado_api / errores_procesamiento)
    sincronizar_prime   -> prime Licitaciones
    escribir_resumen    -> clásico y prime resumen_licitaciones_adjudicadas
    recalcular_oc       -> OC Base / Licitaciones / Licitaciones_diarias, CALL consulta1/3/5
    publicar_consulta5  -> prime consulta5 + Fecha
"""

from __future__ import annotations

import csv
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

from cierre_adj import bd, mercadopublico as mp, reglas


# ---------------------------------------------------------------- listado ---

def listado_desde_api(cn_c, api_mes: dict, log=print) -> list[str]:
    """Agrega a listado_api lo que la API informa y falta. fecha_descarga = día
    en que la API lo listó (mismo criterio que el cierre viejo del clásico)."""
    existentes = set()
    codigos = {c: dia for dia, cods in sorted(api_mes.items()) for c in cods}
    for trozo in bd.trozos(sorted(codigos), 1000):
        marcas, vals = bd.en_lista(trozo)
        existentes |= {x for (x,) in bd.todos(
            cn_c, f"SELECT licitacion FROM {bd.DB_ADJ}.listado_api WHERE licitacion IN ({marcas})", vals)}
    nuevas = sorted(set(codigos) - existentes)
    with cn_c.cursor() as cur:
        for cod in nuevas:
            cur.execute(f"INSERT IGNORE INTO {bd.DB_ADJ}.listado_api (licitacion, fecha_descarga) VALUES (%s, %s)",
                        (cod, codigos[cod]))
    cn_c.commit()
    log(f"   listado: {len(nuevas)} licitaciones nuevas desde la API" + (f": {', '.join(nuevas[:15])}" if nuevas else ""))
    return nuevas


# ------------------------------------------------------------------ actas ---

def _registrar_error(cn_c, licitacion: str, exc: Exception) -> None:
    with cn_c.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {bd.DB_ADJ}.errores_procesamiento
                    (licitacion, fecha_error, tipo_error, mensaje_error, stack_trace, intento_numero)
                SELECT %s, NOW(), %s, %s, %s, COALESCE(intentos, 0)
                FROM {bd.DB_ADJ}.listado_api WHERE licitacion = %s""",
            (licitacion, type(exc).__name__, str(exc)[:1000], traceback.format_exc()[:2000], licitacion))
    cn_c.commit()


def descargar_actas(cn_c, licitaciones: list[tuple[str, str]], limite_minutos: float | None = None,
                    log=print) -> dict:
    """Baja el acta de cada (licitacion, fecha_respaldo) e inserta en clásico.

    estado=1 solo si el acta quedó entera (adjudicada sin ítems con error),
    desierta o sin ítems. INSERT IGNORE sobre la clave UNICO: re-descargar un
    acta incompleta suma las filas que faltaban sin duplicar."""
    res = {"adjudicadas": 0, "desiertas": 0, "sin_items": 0, "sin_acta": 0, "errores": 0,
           "incompletas": [], "filas_insertadas": 0, "no_procesadas": 0}
    if not licitaciones:
        return res
    cols = ",".join(bd.COLS_LICITACIONES)
    sesion = requests.Session()
    t0 = time.monotonic()
    for i, (lic, respaldo) in enumerate(licitaciones, 1):
        if limite_minutos and (time.monotonic() - t0) / 60 > limite_minutos:
            res["no_procesadas"] = len(licitaciones) - i + 1
            log(f"   actas: tope de {limite_minutos:.0f} min alcanzado; quedan {res['no_procesadas']} para la próxima corrida")
            break
        with cn_c.cursor() as cur:
            cur.execute(f"""UPDATE {bd.DB_ADJ}.listado_api SET intentos = COALESCE(intentos, 0) + 1,
                            fecha_ultimo_intento = NOW() WHERE licitacion = %s""", (lic,))
        cn_c.commit()
        try:
            acta = mp.descargar_acta(lic, str(respaldo), sesion)
        except mp.SinActa as exc:
            res["sin_acta"] += 1
            _registrar_error(cn_c, lic, exc)
            continue
        except Exception as exc:  # noqa: BLE001  red, formato: reintenta la próxima corrida
            res["errores"] += 1
            _registrar_error(cn_c, lic, exc)
            log(f"   acta {lic}: {type(exc).__name__}: {exc}")
            continue
        insertadas = bd.insertar_lotes(cn_c, f"INSERT IGNORE INTO {bd.DB_ADJ}.Licitaciones ({cols}) VALUES",
                                       acta["filas"])
        completa = not acta["items_con_error"]
        if completa:
            with cn_c.cursor() as cur:
                cur.execute(f"UPDATE {bd.DB_ADJ}.listado_api SET estado = 1 WHERE licitacion = %s", (lic,))
        cn_c.commit()
        res["filas_insertadas"] += insertadas
        res[{"adjudicada": "adjudicadas", "desierta": "desiertas", "sin_items": "sin_items"}[acta["estado"]]] += 1
        if not completa:
            res["incompletas"].append(lic)
            _registrar_error(cn_c, lic, RuntimeError("acta parcial: " + "; ".join(acta["items_con_error"][:5])))
        if i % 25 == 0:
            log(f"   actas: {i}/{len(licitaciones)} procesadas, {res['filas_insertadas']} filas nuevas")
    log(f"   actas: {res}")
    return res


def pendientes_actas(cn_c, desde: str, hasta: str) -> list[tuple[str, str]]:
    return bd.todos(
        cn_c,
        f"""SELECT licitacion, fecha_descarga FROM {bd.DB_ADJ}.listado_api l
            WHERE estado = 0 AND fecha_descarga BETWEEN %s AND %s
              AND NOT EXISTS (SELECT 1 FROM {bd.DB_ADJ}.Licitaciones x WHERE x.ADQUISICION = l.licitacion)
            ORDER BY fecha_descarga""",
        (desde, hasta))


def reconciliar_marcas(cn_c, desde: str, hasta: str, log=print) -> int:
    """estado=0 que ya tienen filas -> estado=1 (medido 15-09: 10.935 así)."""
    with cn_c.cursor() as cur:
        n = cur.execute(
            f"""UPDATE {bd.DB_ADJ}.listado_api l SET l.estado = 1
                WHERE l.estado = 0 AND l.fecha_descarga BETWEEN %s AND %s
                  AND EXISTS (SELECT 1 FROM {bd.DB_ADJ}.Licitaciones x WHERE x.ADQUISICION = l.licitacion)""",
            (desde, hasta))
    cn_c.commit()
    if n:
        log(f"   listado: {n} marcas estado=0 con acta ya descargada -> estado=1")
    return n


# ------------------------------------------------------ clásico -> prime ---

def sincronizar_prime(cn_c, cn_p, mes: str, log=print) -> int:
    """Copia a prime las filas que le faltan (por licitación del mes)."""
    from cierre_adj.validaciones import conteo_por_licitacion
    c, p = conteo_por_licitacion(cn_c, mes), conteo_por_licitacion(cn_p, mes)
    faltan = sorted(k for k, v in c.items() if p.get(k, 0) < v)
    if not faltan:
        return 0
    ini, fin = bd.rango_mes(mes)
    cols = ",".join(bd.COLS_LICITACIONES)
    total = 0
    for trozo in bd.trozos(faltan, 200):
        marcas, vals = bd.en_lista(trozo)
        filas = bd.todos(cn_c, f"""SELECT {cols} FROM {bd.DB_ADJ}.Licitaciones
                                   WHERE FECHASQL BETWEEN %s AND %s AND ADQUISICION IN ({marcas})""", [ini, fin] + vals)
        total += bd.insertar_lotes(cn_p, f"INSERT IGNORE INTO {bd.DB_ADJ}.Licitaciones ({cols}) VALUES", filas)
        cn_p.commit()
    log(f"   prime {mes}: {total} filas copiadas ({len(faltan)} licitaciones)")
    return total


# ---------------------------------------------------------------- resumen ---

def _respaldar(filas, columnas, ruta: Path) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(columnas)
        w.writerows(filas)


def escribir_resumen(cn_c, cn_p, mes: str, tablas: list[str], dir_respaldo: Path, log=print) -> None:
    """DELETE del mes + INSERT, en una transacción por servidor, con respaldo
    CSV previo y verificación fila a fila antes del commit."""
    ini, _ = bd.rango_mes(mes)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    cols = ",".join(reglas.COLS_RESUMEN)
    esperado = {t: reglas.resumen_esperado(cn_c, mes, t) for t in tablas}
    for nombre, cn in (("clasico", cn_c), ("prime", cn_p)):
        try:
            for t in tablas:
                previas = reglas.resumen_actual(cn, mes, t)
                if previas:
                    _respaldar(previas, reglas.COLS_RESUMEN, dir_respaldo / f"resumen_{nombre}_{t}_{mes}_{sello}.csv")
                with cn.cursor() as cur:
                    cur.execute(f"DELETE FROM {bd.DB_RESUMEN}.{t} WHERE Fecha = %s", (ini,))
                bd.insertar_lotes(cn, f"INSERT INTO {bd.DB_RESUMEN}.{t} ({cols}) VALUES", esperado[t])
                comp = reglas.comparar(reglas.firma(esperado[t]), reglas.firma(reglas.resumen_actual(cn, mes, t)))
                if not comp["iguales"]:
                    raise RuntimeError(f"{nombre}.{t} {mes}: escrito != calculado {comp}")
                log(f"   resumen {nombre}.{t} {mes}: {len(previas)} -> {len(esperado[t])} proveedores")
            cn.commit()
        except Exception:
            cn.rollback()
            raise


def copiar_resumen_a_prime(cn_c, cn_p, mes: str, tablas: list[str], dir_respaldo: Path, log=print) -> None:
    """Para meses ya cerrados: publicados/cerrados son la foto del día del
    cierre; si prime difiere se copia la foto del clásico, sin recalcular."""
    ini, _ = bd.rango_mes(mes)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    cols = ",".join(reglas.COLS_RESUMEN)
    try:
        for t in tablas:
            filas = reglas.resumen_actual(cn_c, mes, t)
            previas = reglas.resumen_actual(cn_p, mes, t)
            if previas:
                _respaldar(previas, reglas.COLS_RESUMEN, dir_respaldo / f"resumen_prime_{t}_{mes}_{sello}.csv")
            with cn_p.cursor() as cur:
                cur.execute(f"DELETE FROM {bd.DB_RESUMEN}.{t} WHERE Fecha = %s", (ini,))
            bd.insertar_lotes(cn_p, f"INSERT INTO {bd.DB_RESUMEN}.{t} ({cols}) VALUES", filas)
            if not reglas.comparar(reglas.firma(filas), reglas.firma(reglas.resumen_actual(cn_p, mes, t)))["iguales"]:
                raise RuntimeError(f"prime.{t} {mes}: copia distinta del clásico")
            log(f"   resumen prime.{t} {mes}: copiada la foto del clásico ({len(filas)} proveedores)")
        cn_p.commit()
    except Exception:
        cn_p.rollback()
        raise


def asegurar_fecha(cn_p, mes: str, log=print) -> None:
    nombre = reglas.nombre_mes(mes)
    with cn_p.cursor() as cur:
        n = cur.execute(f"""INSERT INTO {bd.DB_TM}.Fecha (strNombreFecha, datFecha)
                            SELECT %s, %s FROM DUAL
                            WHERE NOT EXISTS (SELECT 1 FROM {bd.DB_TM}.Fecha WHERE datFecha = %s)""",
                        (nombre, mes, mes))
    cn_p.commit()
    if n:
        log(f"   prime Fecha: agregada '{nombre}'")


# -------------------------------------------------------------------- OC ---

def _sincronizar_base(cn_c, cn_o, log=print) -> None:
    """consulta1 solo usa rutproveedor de Base. Si el conjunto de RUT difiere,
    se reemplaza Base del OC completa (tabla nueva + RENAME atómico)."""
    ruts_c, ruts_o = reglas.ruts_base(cn_c), reglas.ruts_base(cn_o)
    if ruts_c == ruts_o:
        log(f"   OC Base: mismos {len(ruts_c)} proveedores que el clásico, no se toca")
        return
    log(f"   OC Base: clásico {len(ruts_c)} proveedores, OC {len(ruts_o)} -> se reemplaza")
    cols = [c for (c,) in bd.todos(cn_c, """SELECT COLUMN_NAME FROM information_schema.columns
                                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='Base' ORDER BY ORDINAL_POSITION""",
                                   (bd.DB_BASE,))]
    lista = ",".join(f"`{c}`" for c in cols)
    with cn_o.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {bd.DB_BASE}.Base_cierre_nueva")
        cur.execute(f"CREATE TABLE {bd.DB_BASE}.Base_cierre_nueva LIKE {bd.DB_BASE}.Base")
    ultimo_id, total = 0, 0
    while True:
        filas = bd.todos(cn_c, f"SELECT {lista} FROM {bd.DB_BASE}.Base WHERE id > %s ORDER BY id LIMIT 20000", (ultimo_id,))
        if not filas:
            break
        total += bd.insertar_lotes(cn_o, f"INSERT INTO {bd.DB_BASE}.Base_cierre_nueva ({lista}) VALUES", filas)
        cn_o.commit()
        ultimo_id = filas[-1][cols.index("id")]
    n_c = bd.uno(cn_c, f"SELECT COUNT(*) FROM {bd.DB_BASE}.Base")[0]
    if total != n_c:
        raise RuntimeError(f"Base copiada incompleta: {total} de {n_c}")
    with cn_o.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {bd.DB_BASE}.Base_cierre_respaldo")
        cur.execute(f"RENAME TABLE {bd.DB_BASE}.Base TO {bd.DB_BASE}.Base_cierre_respaldo, "
                    f"{bd.DB_BASE}.Base_cierre_nueva TO {bd.DB_BASE}.Base")
    log(f"   OC Base: {total} filas cargadas (la anterior quedó en Base_cierre_respaldo)")


def _sincronizar_licitaciones_oc(cn_c, cn_o, mes: str, log=print) -> None:
    """Deja las filas del mes del OC iguales a las del clásico, licitación por licitación."""
    from cierre_adj.validaciones import conteo_por_licitacion
    ini, fin = bd.rango_mes(mes)
    c, o = conteo_por_licitacion(cn_c, mes), conteo_por_licitacion(cn_o, mes)
    distintas = sorted(k for k in set(c) | set(o) if c.get(k, 0) != o.get(k, 0))
    if not distintas:
        log(f"   OC Licitaciones {mes}: igual al clásico ({sum(c.values())} filas)")
        return
    cols = ",".join(bd.COLS_LICITACIONES)
    for trozo in bd.trozos(distintas, 200):
        marcas, vals = bd.en_lista(trozo)
        with cn_o.cursor() as cur:
            cur.execute(f"""DELETE FROM {bd.DB_ADJ}.Licitaciones
                            WHERE FECHASQL BETWEEN %s AND %s AND ADQUISICION IN ({marcas})""", [ini, fin] + vals)
        filas = bd.todos(cn_c, f"""SELECT {cols} FROM {bd.DB_ADJ}.Licitaciones
                                   WHERE FECHASQL BETWEEN %s AND %s AND ADQUISICION IN ({marcas})""", [ini, fin] + vals)
        bd.insertar_lotes(cn_o, f"INSERT IGNORE INTO {bd.DB_ADJ}.Licitaciones ({cols}) VALUES", filas)
        cn_o.commit()
    o2 = conteo_por_licitacion(cn_o, mes)
    if o2 != c:
        malas = [k for k in set(c) | set(o2) if c.get(k) != o2.get(k)]
        raise RuntimeError(f"OC Licitaciones {mes} sigue distinto del clásico en {len(malas)} licitaciones: {malas[:5]}")
    log(f"   OC Licitaciones {mes}: {len(distintas)} licitaciones igualadas al clásico ({sum(c.values())} filas)")


def _sincronizar_diarias_oc(cn_c, cn_o, adquisiciones, log=print) -> None:
    """REPLACE de las filas de Licitaciones_diarias que usa el JOIN de consulta5.

    REPLACE y no INSERT IGNORE: una fila clasificada después de copiarla debe
    llegar con su pactivo."""
    cols = [c for (c,) in bd.todos(cn_c, """SELECT COLUMN_NAME FROM information_schema.columns
                                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='Licitaciones_diarias'
                                           ORDER BY ORDINAL_POSITION""", (bd.DB_DIARIAS,))]
    lista = ",".join(f"`{c}`" for c in cols)
    total = 0
    for trozo in bd.trozos(sorted(adquisiciones), 300):
        marcas, vals = bd.en_lista(trozo)
        filas = bd.todos(cn_c, f"SELECT {lista} FROM {bd.DB_DIARIAS}.Licitaciones_diarias WHERE Licitacion IN ({marcas})", vals)
        bd.insertar_lotes(cn_o, f"REPLACE INTO {bd.DB_DIARIAS}.Licitaciones_diarias ({lista}) VALUES", filas)
        cn_o.commit()
        total += len(filas)
    log(f"   OC Licitaciones_diarias: {total} filas actualizadas ({len(adquisiciones)} licitaciones)")


def recalcular_oc(cn_c, cn_o, mes: str, esp: dict, log=print) -> None:
    ini, fin = bd.rango_mes(mes)
    _sincronizar_base(cn_c, cn_o, log)
    _sincronizar_licitaciones_oc(cn_c, cn_o, mes, log)
    i_adq = bd.COLS_LICITACIONES.index("ADQUISICION")
    _sincronizar_diarias_oc(cn_c, cn_o, {f[i_adq] for f in esp["c3"]}, log)
    with cn_o.cursor() as cur:
        cur.execute(f"DELETE FROM {bd.DB_TM}.consulta3 WHERE FECHASQL BETWEEN %s AND %s", (ini, fin))
        cur.execute(f"DELETE FROM {bd.DB_TM}.consulta5 WHERE FECHASQL BETWEEN %s AND %s", (ini, fin))
        cn_o.commit()
        cur.execute(f"CALL {bd.DB_TM}.insertarTablaConsulta1()")
        cur.execute(f"CALL {bd.DB_TM}.insertarTablaConsulta3(%s, %s)", (ini, fin))
        cur.execute(f"CALL {bd.DB_TM}.insertarTablaConsulta5(%s, %s)", (ini, fin))
    cn_o.commit()
    c3 = reglas.comparar(esp["firma_c3"], reglas.firma_c3(reglas.consulta_actual(cn_o, "consulta3", mes)))
    c5 = reglas.comparar(esp["firma_c5"], reglas.firma_c5(reglas.consulta_actual(cn_o, "consulta5", mes)))
    log(f"   OC {mes}: consulta3 {c3} | consulta5 {c5}")
    if not (c3["iguales"] and c5["iguales"]):
        raise RuntimeError(f"OC {mes}: el resultado de los procedimientos no calza con la regla; no se publica en prime")


def publicar_consulta5(cn_o, cn_p, mes: str, esp: dict, log=print) -> None:
    """consulta5 del mes OC -> prime (DELETE + INSERT en una transacción) y Fecha."""
    ini, fin = bd.rango_mes(mes)
    filas = reglas.consulta_actual(cn_o, "consulta5", mes)
    cols = ",".join(bd.COLS_CONSULTA5)
    try:
        with cn_p.cursor() as cur:
            antes = cur.execute(f"DELETE FROM {bd.DB_TM}.consulta5 WHERE FECHASQL BETWEEN %s AND %s", (ini, fin))
        bd.insertar_lotes(cn_p, f"INSERT INTO {bd.DB_TM}.consulta5 ({cols}) VALUES", filas)
        comp = reglas.comparar(esp["firma_c5"], reglas.firma_c5(reglas.consulta_actual(cn_p, "consulta5", mes)))
        if not comp["iguales"]:
            raise RuntimeError(f"prime consulta5 {mes}: escrito != regla {comp}")
        cn_p.commit()
    except Exception:
        cn_p.rollback()
        raise
    log(f"   prime consulta5 {mes}: {antes} -> {len(filas)} filas")
    asegurar_fecha(cn_p, mes, log)
