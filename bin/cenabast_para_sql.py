#!/usr/bin/python3.6
"""Subida Cenabast: carga el Excel de Cenabast en `cenabast`.`base` (Clasico y
Prime) y registra el ultimo mes en `cenabast`.`Fecha`.

Flujo (mismo patron de conexion que base_para_sql.py):
    1. Lee el Excel (una hoja, 29 columnas).
    2. Mapea la columna FECHASQL del Excel a FECHA_ADJUDICACION de la tabla.
    3. Repara el trio de montos VALOR_UNITARIO / CANT_ADJUDICADA /
       TOTAL_NETO_ADJUDICADA cuando llegan con un punto espurio (ver
       reparar_montos): el invariante es TOTAL = VALOR_UNITARIO * CANT_ADJUDICADA.
    4. TRUNCATE + recarga por lotes en `base` (primero Clasico, luego Prime).
    5. Valida conteo y sumas del trio en cada servidor.
    6. Inserta en `Fecha` el ultimo mes (MAX(FECHASQL)) si aun no existe, en
       ambos servidores.

Uso: python3 cenabast_para_sql.py <ruta_excel>
"""
import os
import sys
import time

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('/usr/local/lib/python3.6/site-packages')

# HELPER ES USADO POR CLASICO; LA CONEXION A PRIME SE HACE POR URL.create,
# para evitar posible cruce de servidores (igual que base_para_sql.py).
from vault_linux_helper import get_engine

N_DB = "cenabast"
TABLA_BASE = "base"
TABLA_FECHA = "Fecha"
TAMANO_LOTE = 5000

# --- Conexion PRIME (.68) ---
url_prime = URL.create(
    drivername="mysql+pymysql",
    username="root",
    password="@_SecureRoot2025DB_M8qP3nX7",
    host="10.0.0.68",
    port=8806,
    database=N_DB,
    query={"charset": "utf8mb4"},
)
engine_prime = create_engine(url_prime)

# --- Conexion CLASICO (.69) via vault helper ---
engine_clasico = get_engine(N_DB)

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

# La columna FECHASQL del Excel corresponde a FECHA_ADJUDICACION en la tabla.
RENOMBRES = {"FECHASQL": "FECHA_ADJUDICACION"}

# Orden y tipos destino de `cenabast`.`base`.
COLS_DESTINO = [
    "Id", "NUM_LICITACION", "CLIENTE", "RUT", "DIRECCION", "COMUNA", "REGION",
    "FECHA_PUBLICACION", "FECHACIERRE", "PRODUCTO", "CODONU", "DESCONU",
    "CANTIDAD", "NUM_DE_PRODUCTO", "PROVEEDORES", "RUTPROVEEDORES",
    "RAZON_SOCIAL_PROVEEDOR", "ESPCOMPRADOR", "ESPECIFICACIONPROVEEDORES",
    "VALOR_UNITARIO", "CANT_ADJUDICADA", "TOTAL_NETO_ADJUDICADA", "ESTADO",
    "PRODUCTO_PHT", "PRESENTACION_PHT", "FORMA_F_PHT", "MEDICAMENTO",
    "FECHA_ADJUDICACION", "SUCURSALPROVEEDOR",
]
COLS_DOUBLE = [
    "Id", "PRODUCTO", "CODONU", "CANTIDAD", "NUM_DE_PRODUCTO",
    "VALOR_UNITARIO", "CANT_ADJUDICADA", "TOTAL_NETO_ADJUDICADA",
]
COLS_DATETIME = ["FECHA_PUBLICACION", "FECHACIERRE"]   # tipo datetime
COLS_DATE = ["FECHA_ADJUDICACION"]                      # tipo date
TRIO = ["VALOR_UNITARIO", "CANT_ADJUDICADA", "TOTAL_NETO_ADJUDICADA"]


def seleccion_archivo():
    for arg in sys.argv[1:]:
        if arg.lower().endswith((".xlsx", ".xls", ".xlsb")):
            return arg
    print("[ERROR] No se recibio la ruta del archivo Excel.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# REPARACION DEL TRIO DE MONTOS
# ---------------------------------------------------------------------------
# Tolerancia relativa para considerar que el trio "cuadra" (TOTAL = V*C).
# Los precios unitarios pueden ser decimales reales (p.ej. 5.5), asi que se usa
# margen relativo en vez de exigir igualdad exacta.
TOL_REL = 0.005


def _num(raw):
    """float del valor crudo, o None si esta vacio / no parsea."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _dedot(raw):
    """Quita puntos/comas y devuelve el entero resultante (recupera el numero
    real cuando hubo un punto espurio, p.ej. '1.614907296' -> 1614907296)."""
    s = str(raw).replace(".", "").replace(",", "").strip()
    if s.lstrip("-").isdigit():
        return float(s)
    return None


def _sospechoso(raw):
    """True solo en el caso 'varchar' real: el valor NO parsea como numero o
    trae 2+ puntos (separadores espurios). Un decimal simple y valido como
    '5.5' NO es sospechoso."""
    if raw is None:
        return False
    s = str(raw).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return False
    if _num(s) is None:
        return True
    return s.count(".") >= 2 or s.count(",") >= 2


def _cuadra(t, v, c):
    if t is None or v is None or c is None:
        return False
    esperado = v * c
    if esperado == 0:
        return abs(t) <= 1
    return abs(t - esperado) <= abs(esperado) * TOL_REL


# Limite de un entero de 32 bits (Long). Un TOTAL con decimales que supera este
# valor es justo lo que rompia en el proceso viejo de Access (casteo a Long); en
# `double` no rompe, pero se redondea a entero para dejarlo INT-safe.
INT32_MAX = 2147483647


def reparar_montos(df):
    """Normaliza el trio VALOR_UNITARIO / CANT_ADJUDICADA / TOTAL_NETO_ADJUDICADA.

    Dos correcciones, ambas acotadas (la gran mayoria de filas no se toca):

    1. Punto espurio / 'varchar': un valor que NO parsea como numero o trae 2+
       puntos. Se reconstruye con el invariante TOTAL = VALOR * CANT:
         - TOTAL roto -> TOTAL = VALOR * CANT.
         - VALOR/CANT roto -> se despeja del TOTAL y el otro campo.

    2. TOTAL decimal por encima del INT32 (lo que reventaba en Access): se
       redondea a entero. Si ese TOTAL ademas no concuerda con VALOR*CANT
       (decimal espurio, p.ej. Id 22466) se usa VALOR*CANT; si concuerda
       (decimal real, p.ej. Id 21643) se redondea el propio TOTAL.

    NO toca decimales validos por debajo del INT32, ni inconsistencias sin punto
    (p.ej. diferencias de unidad /1000): esas se dejan tal cual.
    """
    cont = {"varchar_total": 0, "varchar_valor": 0, "varchar_cant": 0,
            "overflow_vxc": 0, "overflow_round": 0}

    def fix(row):
        rv, rc, rt = row["VALOR_UNITARIO"], row["CANT_ADJUDICADA"], row["TOTAL_NETO_ADJUDICADA"]
        v, c, t = _num(rv), _num(rc), _num(rt)
        sv, sc, st = _sospechoso(rv), _sospechoso(rc), _sospechoso(rt)
        esperado = (v * c) if (v is not None and c is not None) else None

        # --- 1) Caso 'varchar' (valor no-numerico / 2+ puntos) ---
        if st and not sv and not sc and esperado is not None:
            t = round(esperado)
            cont["varchar_total"] += 1
        elif sv and not sc and not st and t is not None and c not in (None, 0):
            cand = t / c
            dedot = _dedot(rv)
            v = dedot if (dedot is not None and _cuadra(t, dedot, c)) else cand
            cont["varchar_valor"] += 1
        elif sc and not sv and not st and t is not None and v not in (None, 0):
            cand = t / v
            dedot = _dedot(rc)
            c = dedot if (dedot is not None and _cuadra(t, v, dedot)) else cand
            cont["varchar_cant"] += 1
        # --- 2) TOTAL decimal que supera el INT32 (caso Access) -> entero ---
        elif t is not None and float(t) != int(t) and abs(t) > INT32_MAX:
            if esperado is not None and abs(t - esperado) > 1:
                t = round(esperado)      # decimal espurio -> VALOR * CANT (Id 22466)
                cont["overflow_vxc"] += 1
            else:
                t = round(t)             # decimal real -> redondeo (Id 21643)
                cont["overflow_round"] += 1
        # else: fila sin punto y/o decimal bajo INT32 -> se deja tal cual.

        return pd.Series({"VALOR_UNITARIO": v, "CANT_ADJUDICADA": c,
                          "TOTAL_NETO_ADJUDICADA": t})

    reparado = df[TRIO].apply(fix, axis=1)
    for col in TRIO:
        df[col] = reparado[col]

    print(f"[MONTOS] varchar -> TOTAL: {cont['varchar_total']} | "
          f"VALOR: {cont['varchar_valor']} | CANT: {cont['varchar_cant']}  ||  "
          f"TOTAL decimal>INT32 -> a V*C: {cont['overflow_vxc']} | "
          f"redondeado: {cont['overflow_round']}")
    return df


# ---------------------------------------------------------------------------
# PREPARACION DEL DATAFRAME
# ---------------------------------------------------------------------------
def preparar_dataframe(path):
    print(f"Leyendo Excel: {path}")
    # El trio de montos se lee como texto para poder detectar el punto espurio
    # (un numero ya parseado pierde la diferencia entre '5.5' y '1.6.7').
    df = pd.read_excel(path, engine="openpyxl", dtype={c: str for c in TRIO})
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns=RENOMBRES)
    print(f"Filas leidas: {len(df)} | columnas: {len(df.columns)}")

    # Reparar montos antes de castear a numero.
    df = reparar_montos(df)

    # Asegura todas las columnas destino (las que falten -> NULL).
    for col in COLS_DESTINO:
        if col not in df.columns:
            print(f"[AVISO] Columna ausente en el Excel, se llena NULL: {col}")
            df[col] = None

    # Numericos (double).
    for col in COLS_DOUBLE:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Fechas: FECHA_ADJUDICACION es date; FECHA_PUBLICACION/FECHACIERRE datetime.
    for col in COLS_DATE:
        d = pd.to_datetime(df[col], errors="coerce")
        df[col] = d.dt.strftime("%Y-%m-%d").where(d.notna(), None)
    for col in COLS_DATETIME:
        d = pd.to_datetime(df[col], errors="coerce")
        df[col] = d.dt.strftime("%Y-%m-%d %H:%M:%S").where(d.notna(), None)

    # Varchar: strip + recorte a 255.
    cols_texto = [c for c in COLS_DESTINO
                  if c not in COLS_DOUBLE + COLS_DATETIME + COLS_DATE]
    for col in cols_texto:
        df[col] = (df[col].astype(str)
                   .replace({"nan": None, "NaT": None, "None": None, "": None})
                   .map(lambda x: x.strip()[:255] if isinstance(x, str) else x))

    return df[COLS_DESTINO]


def periodo_ultimo_mes(df):
    """(datFecha 'YYYY-MM', strNombreFecha 'Mes Anio') del MAX(FECHA_ADJUDICACION)."""
    d = pd.to_datetime(df["FECHA_ADJUDICACION"], errors="coerce")
    fmax = d.max()
    if pd.isna(fmax):
        return None, None
    return fmax.strftime("%Y-%m"), f"{MESES_ES[fmax.month]} {fmax.year}"


# ---------------------------------------------------------------------------
# MIGRACION POR SERVIDOR
# ---------------------------------------------------------------------------
def migrar_base(engine, df, servidor):
    print(f"\n=== Cargando `base` en {servidor} ===")
    try:
        with engine.connect() as conn:
            conn.execute(text(f"TRUNCATE TABLE `{TABLA_BASE}`"))
            try:
                conn.execute(text("COMMIT"))
            except Exception:
                pass

        total = len(df)
        for i in range(0, total, TAMANO_LOTE):
            lote = df.iloc[i:i + TAMANO_LOTE].copy()
            lote = lote.where(pd.notnull(lote), None)
            lote.to_sql(name=TABLA_BASE, con=engine, if_exists="append", index=False)
            fin = min(i + TAMANO_LOTE, total)
            print(f"Progreso {servidor}: {fin}/{total} ({fin / total * 100:.1f}%)")

        return validar_base(engine, df, servidor)
    except Exception as e:
        print(f"ERROR CRITICO cargando base en {servidor}: {e}")
        return False


def validar_base(engine, df, servidor):
    print(f"--- Validando `base` en {servidor} ---")
    try:
        df_sql = pd.read_sql(f"SELECT * FROM `{TABLA_BASE}`", con=engine)
        n_excel, n_sql = len(df), len(df_sql)
        print(f"Filas Excel: {n_excel:,} | Filas SQL: {n_sql:,}")
        if n_excel != n_sql:
            print(f"ERROR CRITICO: conteo no coincide en {servidor} "
                  f"(delta={n_excel - n_sql:+,}).")
            return False

        for col in TRIO:
            s_excel = pd.to_numeric(df[col], errors="coerce").fillna(0).round(0).sum()
            s_sql = pd.to_numeric(df_sql[col], errors="coerce").fillna(0).round(0).sum()
            if abs(s_excel - s_sql) > 1:
                print(f"ERROR CRITICO: suma de {col} no coincide en {servidor} "
                      f"(Excel={s_excel:,.0f} vs SQL={s_sql:,.0f}).")
                return False

        print(f"EXCELENTE: validacion OK en {servidor} ({n_sql:,} filas, trio cuadra).")
        return True
    except Exception as e:
        print(f"ERROR CRITICO validando base en {servidor}: {e}")
        return False


def actualizar_fecha(engine, dat_fecha, str_nombre, servidor):
    """Inserta el periodo en `Fecha` solo si aun no existe (idempotente)."""
    print(f"\n--- Alineando `Fecha` en {servidor}: {str_nombre} ({dat_fecha}) ---")
    try:
        with engine.begin() as conn:
            res = conn.execute(text(f"""
                INSERT INTO `{TABLA_FECHA}` (strNombreFecha, datFecha)
                SELECT :nom, :fec FROM DUAL
                WHERE NOT EXISTS (
                    SELECT 1 FROM `{TABLA_FECHA}` WHERE datFecha = :fec
                )
            """), {"nom": str_nombre, "fec": dat_fecha})
            if res.rowcount:
                print(f">>> [Fecha] Periodo agregado en {servidor}: {str_nombre} | {dat_fecha}")
            else:
                print(f"--- [OK] {servidor}: el periodo {dat_fecha} ya existia, no se duplica ---")
    except Exception as e:
        print(f"ERROR actualizando Fecha en {servidor}: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    path = seleccion_archivo()
    df = preparar_dataframe(path)

    dat_fecha, str_nombre = periodo_ultimo_mes(df)
    if dat_fecha:
        print(f"\nUltimo mes del archivo (MAX FECHASQL): {str_nombre} ({dat_fecha})")
    else:
        print("\n[ALERTA] No se pudo determinar el ultimo mes (FECHASQL vacio).")

    # Clasico primero; Prime solo si Clasico valido (mismo criterio que la TD).
    ok_clasico = migrar_base(engine_clasico, df, "CLASICO")
    ok_prime = False
    if ok_clasico:
        ok_prime = migrar_base(engine_prime, df, "PRIME")
    else:
        print("\nABORTANDO PRIME: fallo la carga/validacion en Clasico.")

    # Fecha: solo en los servidores donde la base quedo bien cargada.
    if dat_fecha:
        if ok_clasico:
            actualizar_fecha(engine_clasico, dat_fecha, str_nombre, "CLASICO")
        if ok_prime:
            actualizar_fecha(engine_prime, dat_fecha, str_nombre, "PRIME")

    if ok_clasico and ok_prime:
        print("\nFINALIZADO EXITOSAMENTE")
    else:
        print("\nERROR CRITICO: el proceso no termino completo (ver log).")


if __name__ == "__main__":
    inicio = time.time()
    main()
    print(f"Tiempo total: {time.time() - inicio:.1f}s")
