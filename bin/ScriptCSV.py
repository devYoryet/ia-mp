#!/usr/bin/python3.6
# -*- coding: utf-8 -*-
import sys
import os
import re
import io
import argparse
from math import ceil

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

    if len(df) == 0:
        return pd.DataFrame()

    # 3. Mapeo y Limpieza
    def internal_norm(c): return str(c).lower().replace(" ", "").replace("_", "").replace("/", "")
    df_cols_norm = {internal_norm(c): c for c in df.columns}
    
    col_variants = {
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
        "EspecificacionTotal": ["nombreroductogenerico", "nombre", "especificacion"] 
    }

    out = pd.DataFrame()
    for col in TABLE_COLUMNS:
        found = None
        for variant in col_variants.get(col, [internal_norm(col)]):
            if variant in df_cols_norm:
                found = df_cols_norm[variant]
                break
        
        if found:
            val = df[found].fillna("")
            text_val = val.astype(str).str.replace(r'[\r\n]+', ' ', regex=True).str.strip()
            
            if col == "FechaEnvio":
                text_val = pd.to_datetime(text_val, dayfirst=True, errors='coerce').dt.strftime('%Y-%m-%d')

            campos_largos = ["Descripcion", "EspecificacionComprador", "EspecificacionProveedor", "EspecificacionTotal"]
            
            if col in campos_largos:
                text_val = text_val.str.slice(0, 500)
            else:
                text_val = text_val.str.slice(0, 255)
            
            # --- LA CLAVE ESTÁ AQUÍ ---
            if col == "FechaEnvio":
                # La fecha SI necesita None para ser NULL, de lo contrario MySQL da error
                out[col] = text_val.replace(["nan", "NaN", "NA", "None", "nan ", "NaT", ""], None)
            else:
                # El resto lo reemplazamos por "" para que no sea NULL
                out[col] = text_val.replace(["nan", "NaN", "NA", "None", "nan ", "NaT"], "")
        else:
            # Si la columna no existe: NULL para fecha, vacío para texto
            out[col] = None if col == "FechaEnvio" else ""

    return out

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

        records = df.to_records(index=False).tolist()
        total = len(records)
        print("    Insercion masiva en {0}: {1} filas en '{2}'".format(
            server_name, total, table_name))

        for i in range(0, total, BATCH_SIZE):
            lote = records[i:i + BATCH_SIZE]
            insert_batch(conn, table_name, lote)
            progreso = min(i + BATCH_SIZE, total)
            print("    [{0}] {1} / {2} filas".format(server_name, progreso, total),
                  flush=True)

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

    # 3. Procesar el archivo (lo costoso: solo se hace una vez aunque escribamos
    # en dos servidores).
    print("Procesando archivo: {0}".format(args.excel))
    df = prepare_dataframe(args.excel)
    if len(df) == 0:
        print("Error: El DataFrame esta vacio, el archivo no se leyo bien o no tiene datos.")
        return
    print("DEBUG: Filas a insertar: {0}".format(len(df)))

    db_name = "prueba_practica" if args.local else "oc_items_segmentado"

    # 4. Primario.
    ok = importar_a_servidor(df, table_name, args.server, db_name, force_local=args.local)
    if not ok:
        print("\n[ERROR CRITICO] Fallo en servidor primario {0}.".format(args.server))
        return

    # 5. Espejo (si corresponde).
    espejo = ESPEJO_DE.get(args.server)
    if args.local or args.no_espejo or not espejo:
        print("\nImportacion terminada en {0} (sin espejo).".format(args.server))
        return

    print("\n>>> Replicando a servidor espejo: {0}".format(espejo.upper()))
    ok_espejo = importar_a_servidor(df, table_name, espejo, db_name, force_local=False)
    if ok_espejo:
        print("\n Importacion finalizada con exito en AMBOS servidores ({0} + {1}).".format(
            args.server, espejo))
        print("Importacion terminada con exito en tabla: {0}".format(table_name))
    else:
        # Clasico/primario ya quedo escrito; el espejo no. No es CRITICO porque
        # los datos NO se perdieron, solo no se replicaron. Aviso para que se
        # rehaga el espejo manualmente sin reprocesar el primario.
        print("\n[WARNING] Primario {0} quedo OK pero ESPEJO {1} fallo.".format(
            args.server, espejo))
        print("Para reintentar SOLO el espejo: subir el archivo con --server {0} --no-espejo".format(
            espejo))

if __name__ == "__main__":
    main()