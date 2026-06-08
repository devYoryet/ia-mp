#!/usr/bin/python3.6
# -*- coding: utf-8 -*-
import sys
import os
import re
import io
import argparse
import csv as _csv
from datetime import datetime
from math import ceil

# Campos de texto largo (VARCHAR 500); el resto va a 255.
CAMPOS_LARGOS = {"Descripcion", "EspecificacionComprador",
                 "EspecificacionProveedor", "EspecificacionTotal"}

# Mapeo columna_destino -> variantes normalizadas del origen. UNICA fuente de
# verdad: la usan tanto el camino pandas (Excel) como el streaming (CSV grande).
COL_VARIANTS = {
    "Codigo": ["codigo", "id", "cod"],
    "CodigoLicitacion": ["codigolicitacion", "codigo_conveniomarco", "licitacion"],
    "Descripcion": ["descripcionobervaciones", "especificacioncomprador", "descripcion"],
    "TipoMoneda": ["tipomonedaoc", "monedaitem", "moneda"],
    "TotalNeto": ["totalnetooc", "totallineaneto", "neto"],
    "Total": ["montototaloc", "montototalocpesoschilenos", "total"],
    "CodigoOrganismo": ["codigoorganismopublico", "id_organismo"],
    "NombreOrganismo": ["organismopublico", "institucion"],
    "RutUnidad": ["rutunidadcompra", "rutunidad"],
    "CodigoUnidad": ["codigounidadcompra", "codigounidad"],
    "NombreUnidad": ["unidadcompra", "nombreunidad"],
    "PaisUnidad": ["paisunidadcompra", "paisunidad"],
    "ComunaUnidad": ["ciudadunidadcompra", "comuna"],
    "RegionUnidad": ["regionunidadcompra", "region"],
    "RutSucursalProveedor": ["rutsucursal", "rutsucursalproveedor"],
    "NombreSucursalProveedor": ["sucursal", "nombresucursalproveedor"],
    "CodigoSucursalProveedor": ["codigosucursal", "codigosucursalproveedor"],
    "Correlativo": ["iditem", "correlativo"],
    "CodigoProducto": ["codigoproductoonu", "sku"],
    "Producto": ["nombreroductogenerico", "producto"],
    "CantidadItem": ["cantidad", "cant"],
    "PrecioNetoItem": ["precioneto", "unitario"],
    "TotalItem": ["totallineaneto", "subtotal"],
    "EspecificacionTotal": ["nombreroductogenerico", "nombre", "especificacion"],
}


def _norm_col(c):
    return str(c).lower().replace(" ", "").replace("_", "").replace("/", "")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

sys.path.insert(0, '/usr/local/lib/python3.6/site-packages')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import mysql.connector
    import pandas as pd
except ImportError as e:
    print(f"LOG ERROR: No se encuentra Pandas.")
    print(f"LOG INFO: Usuario ejecutando: {os.getlogin() if hasattr(os, 'getlogin') else 'unknown'}")
    print(f"LOG INFO: Python Executable: {sys.executable}")
    print(f"LOG INFO: PYTHONPATH: {sys.path}")
    sys.exit(1)

PANDAS_VERSION = int(pd.__version__.split('.')[0])
BATCH_SIZE = 5000
TABLE_COLUMNS = [
    "Codigo","Nombre","Estado","CodigoLicitacion","Descripcion","Tipo","TipoMoneda",
    "FechaCreacion","FechaEnvio","FechaAceptacion","FechaCancelacion","FechaUltimaModificacion",
    "TotalNeto","PorcentajeIva","Impuestos","Total","Pais","CodigoOrganismo","NombreOrganismo",
    "RutUnidad","CodigoUnidad","NombreUnidad","DireccionUnidad","ComunaUnidad","RegionUnidad",
    "PaisUnidad","NombreContacto","CodigoProveedor","NombreProveedor","ActividadProveedor",
    "CodigoSucursalProveedor","NombreSucursalProveedor","RutSucursalProveedor","PaisProveedor",
    "NombreContactoProveedor","CargoContactoProveedor","Correlativo","CodigoCategoria","Categoria",
    "CodigoProducto","Producto","EspecificacionComprador","EspecificacionProveedor","CantidadItem",
    "MonedaItem","PrecioNetoItem","TotalItem","CodigoTipo","EspecificacionTotal"
]

#================================================================================================

sys.path.append('/usr/local/lib/python3.6/site-packages')

from vault_linux_helper import VaultLinuxManager
vault = VaultLinuxManager()

try:
    import mysql.connector
    from mysql.connector import errorcode

    if hasattr(mysql.connector, '__version__') and mysql.connector.__version__.startswith('2.'):
        print("LOG: Detectada libreria antigua, intentando re-vincular al conector moderno...")
        if 'mysql.connector' in sys.modules:
            del sys.modules['mysql.connector']
        import mysql.connector
except ImportError:
    print("LOG ERROR: No se encontro el conector de MySQL adecuado. Favor contactarse con administracion")
    sys.exit(1)

def actualizar_tabla_fecha(conn, table_name):
    if not re.match(r'^\d{6}$', table_name):
        print(f" [AVISO] El nombre de tabla '{table_name}' no tiene formato YYYYMM. Saltando tabla 'fecha'.")
        return

    year = table_name[:4]
    month_num = int(table_name[4:])
    
    meses_es = {
        1:"Enero", 2:"Febrero", 3:"Marzo", 4: "Abril", 
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }

    fecha_sql = f"{year}-{str(month_num).zfill(2)}-01"
    fecha_natural = f"{meses_es[month_num]} {year}"

    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM `fecha` WHERE fecha = %s", (fecha_sql,))
        
        sql = "INSERT INTO `fecha` (fecha, fecha_natural) VALUES (%s, %s)"
        cursor.execute(sql, (fecha_sql, fecha_natural))

        conn.commit()
        print(f" >>> [OK] Tabla 'fecha' sincronizada: {fecha_natural} ({fecha_sql})")
    except Exception as e:
        print(f" [ERROR] No se pudo actualizar tabla 'fecha': {e}")
    finally:
        cursor.close()

#===============================================================================================

# --- FUNCIONES DE APOYO (Se mantienen igual) ---
def norm(s):
    s = re.sub(r"\s+", "", str(s)).lower()
    s = s.replace("ó", "o").replace("á", "a").replace("é", "e").replace("í", "i").replace("ú", "u").replace("ñ", "n")
    s = re.sub(r"[^a-z0-9]", "", s)
    return s

def extract_table_name_from_filename(path):
    basename = os.path.basename(path)
    match = re.search(r'(\d{4})-(\d{1,2})', basename)
    if match:
        year, month = match.groups()
        # zfill(2) asegura que el mes 5 se convierta en "05"
        return f"{year}{month.zfill(2)}"
    return None

def prepare_dataframe(path):
    df = pd.DataFrame()

    # 1. Configuración de argumentos
    csv_args = {
        'dtype': str,
        'sep': ';',
        'low_memory': False,
        'skip_blank_lines': True
    }

    if PANDAS_VERSION >= 2:
        csv_args['on_bad_lines'] = 'skip'
    else:
        csv_args['error_bad_lines'] = False
        csv_args['warn_bad_lines'] = True

    # 2. Leer archivo
    if path.endswith('.csv'):
        try:
            df = pd.read_csv(path, encoding='utf-8', **csv_args)
        except UnicodeDecodeError:
            print("Reintentando con Latin-1...")
            df = pd.read_csv(path, encoding='latin1', **csv_args)
    else:
        df = pd.read_excel(path, dtype=str)

    print(f"Filas leidas: {len(df)}")
    return map_dataframe(df)


def map_dataframe(df):
    """Mapea/limpia un DataFrame a las columnas destino. Sirve tanto para el
    archivo completo (Excel) como para cada CHUNK del CSV grande."""
    if len(df) == 0:
        return pd.DataFrame()

    # 3. Mapeo y Limpieza
    df_cols_norm = {_norm_col(c): c for c in df.columns}

    out = pd.DataFrame()
    for col in TABLE_COLUMNS:
        found = None
        for variant in COL_VARIANTS.get(col, [_norm_col(col)]):
            if variant in df_cols_norm:
                found = df_cols_norm[variant]
                break

        if found:
            val = df[found].fillna("")
            text_val = val.astype(str).str.replace(r'[\r\n]+', ' ', regex=True).str.strip()

            if col == "FechaEnvio":
                # Fecha ISO 'YYYY-MM-DD'. NO usar dayfirst=True: en pandas>=2
                # to_datetime infiere UN formato para toda la columna y con
                # dayfirst infiere '%Y-%d-%m', con lo que '2026-05-26' (dia 26) se
                # lee como "mes 26" -> NaT y se PIERDE la fecha (13k+/mes).
                text_val = pd.to_datetime(text_val, format='%Y-%m-%d', errors='coerce').dt.strftime('%Y-%m-%d')

            text_val = text_val.str.slice(0, 500 if col in CAMPOS_LARGOS else 255)

            if col == "FechaEnvio":
                out[col] = text_val.replace(["nan", "NaN", "NA", "None", "nan ", "NaT", ""], None)
            else:
                out[col] = text_val.replace(["nan", "NaN", "NA", "None", "nan ", "NaT"], "")
        else:
            out[col] = None if col == "FechaEnvio" else ""

    return out


def _sanear_records(records):
    """np.nan / float('nan') -> None (NULL). strftime() sobre fechas vacias deja
    un np.nan (float) que MySQL no sabe vincular (error "Unknown column 'nan'").
    Se hace a nivel de tupla porque en pandas 3.x el .where(notnull, None) NO
    convierte el nan a None. Independiente de la version de pandas."""
    return [
        tuple(None if (isinstance(v, float) and v != v) else v for v in row)
        for row in records
    ]

def create_table_if_not_exists(conn, table_name):
    cursor = conn.cursor()
    columns_sql = []
    
    campos_largos = ["Descripcion", "EspecificacionComprador", "EspecificacionProveedor", "EspecificacionTotal"]
    campos_medios = ["Nombre", "NombreOrganismo", "NombreUnidad", "NombreProveedor", "Producto", "Categoria"]

    for col in TABLE_COLUMNS:
        if col == "FechaEnvio":
            columns_sql.append(f"`{col}` DATE NULL")
        elif col in campos_largos:
            columns_sql.append(f"`{col}` VARCHAR(500) NULL")
        elif col in campos_medios:
            columns_sql.append(f"`{col}` VARCHAR(255) NULL")
        else:
            columns_sql.append(f"`{col}` VARCHAR(255) NULL")

    create_sql = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
        id INT AUTO_INCREMENT PRIMARY KEY,
        {', '.join(columns_sql)},
        fecha_insercion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    cursor.execute(create_sql)
    conn.commit()
    cursor.close()

def insert_batch(conn, table, batch):
    placeholders = ",".join(["%s"] * len(TABLE_COLUMNS))
    cols = "`" + "`, `".join(TABLE_COLUMNS) + "`"
    sql = f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders})"
    cursor = conn.cursor()
    try:
        cursor.executemany(sql, batch)
        conn.commit()
        rows = cursor.rowcount
        if rows > 0:
            print(f"     [DEBUG] Se guardaron {rows} filas en este lote.")
        else:
            print("     [ADVERTENCIA] El lote se envio pero MySQL reporto 0 filas afectadas.")
            
    except Exception as e:
        conn.rollback()
        print(f"     [ERROR] Fallo la insercion: {e}")
        raise e
    finally:
        cursor.close()

def crear_indice_fecha_envio(conn, table_name):
    """Crea un indice en FechaEnvio si no existe.

    Se llama DESPUES de la insercion masiva: indexar sobre la tabla ya cargada
    es mas rapido que mantener el indice durante 440k inserts. Idempotente:
    MySQL no soporta CREATE INDEX IF NOT EXISTS, asi que verificamos antes con
    INFORMATION_SCHEMA para que re-subir el mismo CSV no falle por duplicado.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND INDEX_NAME = 'idx_FechaEnvio'",
            (table_name,),
        )
        if cursor.fetchone()[0] > 0:
            print("    [OK] Indice idx_FechaEnvio ya existe en '{0}'.".format(table_name))
            return
        print("    Creando indice idx_FechaEnvio en '{0}'...".format(table_name))
        cursor.execute(
            "CREATE INDEX idx_FechaEnvio ON `{0}` (FechaEnvio)".format(table_name)
        )
        conn.commit()
        print("    [OK] Indice idx_FechaEnvio creado.")
    except Exception as e:
        # No es critico: si falla la indexacion, los datos ya estan insertados.
        print("    [WARNING] No se pudo crear indice idx_FechaEnvio: {0}".format(e))
    finally:
        cursor.close()


def check_row_count(conn, table_name):
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT (*) FROM `{table_name}`")
        count = cursor.fetchone()[0]
        cursor.close()
        return count
    except:
        return 0

# Servidores espejo: tras exito en uno, replicamos en el otro. Tanto Clasico
# como Prime tienen 'oc_items_segmentado' con la misma estructura, asi que el
# mismo dataframe sirve para los dos sin re-leer el CSV.
ESPEJO_DE = {"clasico": "prime", "prime": "clasico"}


def importar_a_servidor(df, table_name, server_name, db_name, force_local=False):
    """Conecta a `server_name`, crea la tabla si falta, actualiza la fila de
    `fecha` para el periodo, e inserta el dataframe en lotes. Devuelve True si
    todo salio bien, False si fallo en algun paso."""

    print("\n=== Servidor: {0} (BD: {1}) ===".format(server_name.upper(), db_name))

    # 1) Conexion
    try:
        conn = vault.get_linux_mysql_connection(
            database=db_name,
            force_local=force_local,
            server=server_name,
        )
        # MySQL 8 + mysql-connector-python 9.x: 'utf8_general_ci' fue renombrado
        # a 'utf8mb3_general_ci' y el driver moderno ya no lo acepta. La tabla
        # que crea este script usa utf8mb4/utf8mb4_unicode_ci.
        conn.set_charset_collation("utf8mb4", "utf8mb4_unicode_ci")
        cursor = conn.cursor()
        cursor.execute("SET NAMES utf8mb4")
        cursor.close()
        print("    Conexion OK")
    except Exception as e:
        print("    [ERROR] Conexion {0}: {1}".format(server_name, e))
        return False

    # 2) Infraestructura + insercion
    try:
        create_table_if_not_exists(conn, table_name)
        actualizar_tabla_fecha(conn, table_name)

        records = _sanear_records(df.to_records(index=False).tolist())
        total = len(records)
        print("    Insercion masiva en {0}: {1} filas en '{2}'".format(
            server_name, total, table_name))

        for i in range(0, total, BATCH_SIZE):
            lote = records[i:i + BATCH_SIZE]
            insert_batch(conn, table_name, lote)
            progreso = min(i + BATCH_SIZE, total)
            print("    [{0}] {1} / {2} filas".format(server_name, progreso, total),
                  flush=True)

        # Indice sobre FechaEnvio: se hace al final, sobre la tabla ya cargada
        # (mas rapido que mantener el indice durante la insercion masiva).
        crear_indice_fecha_envio(conn, table_name)

        print("=== {0}: OK ===".format(server_name.upper()))
        return True
    except Exception as e:
        print("    [ERROR] {0}: insercion fallida: {1}".format(server_name, e))
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _detectar_encoding(path):
    """utf-8 si el inicio del archivo decodifica; si no, latin-1 (la fuente DA
    suele venir en latin-1). Se decide UNA sola vez: reintentar a mitad de la
    lectura por chunks duplicaria filas."""
    try:
        with open(path, encoding="utf-8") as f:
            f.read(2_000_000)
        return "utf-8"
    except UnicodeDecodeError:
        return "latin1"


def _build_col_index(header):
    """De la cabecera del CSV arma {columna_destino: indice_en_la_fila o None}."""
    norm = {_norm_col(c): i for i, c in enumerate(header)}
    idx = {}
    for col in TABLE_COLUMNS:
        idx[col] = None
        for variant in COL_VARIANTS.get(col, [_norm_col(col)]):
            if variant in norm:
                idx[col] = norm[variant]
                break
    return idx


_RE_WS = __import__("re").compile(r"[\r\n]+")
_NULOS = {"nan", "na", "none", "nat", "null"}


def _limpiar_valor(col, raw):
    """Mismo mapeo/limpieza que map_dataframe, pero por valor (sin pandas).
    Devuelve str (texto) o None (NULL para FechaEnvio)."""
    if raw is None:
        return None if col == "FechaEnvio" else ""
    s = _RE_WS.sub(" ", raw).strip()
    if col == "FechaEnvio":
        # ISO 'YYYY-MM-DD' -> normalizado, o NULL si vacio/invalido.
        if not s or s.lower() in _NULOS:
            return None
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    if s.lower() in _NULOS:
        s = ""
    return s[:500] if col in CAMPOS_LARGOS else s[:255]


def importar_csv_streaming(path, table_name, servers, db_name,
                           batch_size=None, throttle=None):
    """Importa el CSV grande LEYENDO FILA POR FILA (modulo csv, sin pandas) e
    insertando en lotes. Memoria CONSTANTE: solo un lote en RAM (unos pocos MB),
    sin los picos de ~1GB de pandas que ahogaban el server de 8GB. Escribe al
    primario y al espejo en la misma pasada. Devuelve {server: bool}.

    Pensado para NO sobrecargar nada:
      - RAM: 1 lote a la vez (ITEM_DETALLE_BATCH filas).
      - BD: executemany por lote + commit; ~N/lote viajes, trivial para MySQL.
      - Opcional ITEM_DETALLE_THROTTLE (seg) de pausa entre lotes para dar aire."""
    if batch_size is None:
        batch_size = int(os.environ.get("ITEM_DETALLE_BATCH", str(BATCH_SIZE)))
    if throttle is None:
        throttle = float(os.environ.get("ITEM_DETALLE_THROTTLE", "0"))

    # 1) Conectar + crear tabla/fecha en cada servidor.
    conns = {}
    for srv in servers:
        try:
            conn = vault.get_linux_mysql_connection(
                database=db_name, force_local=False, server=srv)
            conn.set_charset_collation("utf8mb4", "utf8mb4_unicode_ci")
            cur = conn.cursor(); cur.execute("SET NAMES utf8mb4"); cur.close()
            create_table_if_not_exists(conn, table_name)
            actualizar_tabla_fecha(conn, table_name)
            conns[srv] = conn
            print("    Conexion OK: {0}".format(srv))
        except Exception as e:
            print("    [ERROR] Conexion {0}: {1}".format(srv, e))

    primario = servers[0]
    if primario not in conns:
        return {s: False for s in servers}

    encoding = _detectar_encoding(path)
    print("    Encoding: {0} · lectura streaming fila-a-fila · lote={1} · throttle={2}s".format(
        encoding, batch_size, throttle))

    totales = {s: 0 for s in conns}

    def _flush(lote):
        for srv in list(conns.keys()):
            conn = conns[srv]
            try:
                insert_batch(conn, table_name, lote)
                totales[srv] += len(lote)
            except Exception as e:
                print("    [ERROR] {0}: insercion fallida: {1}".format(srv, e))
                if srv == primario:
                    raise
                print("    [WARNING] Se deja de escribir en espejo {0}.".format(srv))
                try:
                    conn.close()
                except Exception:
                    pass
                del conns[srv]

    try:
        with open(path, encoding=encoding, newline="") as f:
            reader = _csv.reader(f, delimiter=";")
            header = next(reader)
            idx = _build_col_index(header)
            lote = []
            ncols = len(header)
            for fila in reader:
                if not fila:
                    continue
                rec = tuple(
                    _limpiar_valor(col, fila[idx[col]] if (idx[col] is not None and idx[col] < len(fila)) else None)
                    for col in TABLE_COLUMNS
                )
                lote.append(rec)
                if len(lote) >= batch_size:
                    _flush(lote)
                    print("    Progreso: {0} filas en primario {1}".format(
                        totales.get(primario, 0), primario), flush=True)
                    lote = []
                    if throttle:
                        time.sleep(throttle)
            if lote:
                _flush(lote)
                print("    Progreso: {0} filas en primario {1}".format(
                    totales.get(primario, 0), primario), flush=True)
    except Exception as e:
        print("    [ERROR] Importacion abortada: {0}".format(e))
        for c in conns.values():
            try:
                c.close()
            except Exception:
                pass
        return {s: False for s in servers}

    # Indice + cierre por servidor.
    ok = {s: False for s in servers}
    for srv, conn in conns.items():
        try:
            crear_indice_fecha_envio(conn, table_name)
        except Exception:
            pass
        print("    {0}: {1} filas insertadas en '{2}'".format(srv, totales[srv], table_name))
        print("=== {0}: OK ===".format(srv.upper()))
        ok[srv] = True
        try:
            conn.close()
        except Exception:
            pass
    return ok


def main():
    # 1. Configuración de Argumentos
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True)
    ap.add_argument("--tabla", required=False)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--server", default="prime")  # primario: clasico o prime
    ap.add_argument("--no-espejo", action="store_true",
                    help="No replicar al servidor espejo tras exito en el primario.")
    args = ap.parse_args()

    # 2. Determinar nombre de tabla
    table_name = args.tabla if args.tabla else extract_table_name_from_filename(args.excel)
    if not table_name:
        print("ERROR: Especifica --tabla manualmente.")
        return

    # 3. Procesar el archivo.
    print("Procesando archivo: {0}".format(args.excel))
    db_name = "prueba_practica" if args.local else "oc_items_segmentado"
    espejo = ESPEJO_DE.get(args.server)
    replicar = bool(espejo) and not args.no_espejo and not args.local

    # CSV grande (caso real de produccion): procesar por CHUNKS para no cargar
    # 2M filas en RAM. gestor_oc tiene 8 GB y el camino en-memoria revienta por
    # OOM. La lectura es streaming; los INSERT siguen por lotes de BATCH_SIZE.
    # Una sola pasada escribe al primario y, si corresponde, al espejo.
    if args.excel.lower().endswith(".csv") and not args.local:
        servers = [args.server] + ([espejo] if replicar else [])
        res = importar_csv_streaming(args.excel, table_name, servers, db_name)
        if not res.get(args.server):
            print("\n[ERROR CRITICO] Fallo en servidor primario {0}.".format(args.server))
            return
        if replicar and not res.get(espejo):
            print("\n[WARNING] Primario {0} quedo OK pero ESPEJO {1} fallo.".format(
                args.server, espejo))
            print("Para reintentar SOLO el espejo: --server {0} --no-espejo".format(espejo))
        print("Importacion terminada con exito en tabla: {0}".format(table_name))
        return

    # Camino clasico en memoria (Excel o --local): archivos chicos.
    df = prepare_dataframe(args.excel)
    if len(df) == 0:
        print("Error: El DataFrame esta vacio, el archivo no se leyo bien o no tiene datos.")
        return
    print("DEBUG: Filas a insertar: {0}".format(len(df)))

    ok = importar_a_servidor(df, table_name, args.server, db_name, force_local=args.local)
    if not ok:
        print("\n[ERROR CRITICO] Fallo en servidor primario {0}.".format(args.server))
        return

    if not replicar:
        print("\nImportacion terminada en {0} (sin espejo).".format(args.server))
        return

    print("\n>>> Replicando a servidor espejo: {0}".format(espejo.upper()))
    ok_espejo = importar_a_servidor(df, table_name, espejo, db_name, force_local=False)
    if ok_espejo:
        print("\n Importacion finalizada con exito en AMBOS servidores ({0} + {1}).".format(
            args.server, espejo))
        print("Importacion terminada con exito en tabla: {0}".format(table_name))
    else:
        # Primario ya quedo escrito; el espejo no. No es CRITICO: los datos NO
        # se perdieron, solo no se replicaron.
        print("\n[WARNING] Primario {0} quedo OK pero ESPEJO {1} fallo.".format(
            args.server, espejo))
        print("Para reintentar SOLO el espejo: subir el archivo con --server {0} --no-espejo".format(
            espejo))

if __name__ == "__main__":
    main()