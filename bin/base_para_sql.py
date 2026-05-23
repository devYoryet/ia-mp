#!/usr/bin/python3.6
import pandas as pd
import numpy as np
import sys
import os
import time
import threading
import unicodedata
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from urllib.parse import quote_plus

#HELPER ES USADO POR CLASICO, LA CONEXION A PRIME SE HACE POR URL.create, para evitar posible cruce de servidores
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('/usr/local/lib/python3.6/site-packages')

from vault_linux_helper import VaultLinuxManager
from vault_linux_helper import get_engine

u_db = "root" 
p_db = "@_SecureRoot2025DB_M8qP3nX7"
h_db = "10.0.0.68"
n_db = "0001_td_oc"
puerto = 8806

url_object = URL.create(
    drivername="mysql+pymysql",
    username=u_db,
    password=p_db,
    host=h_db.strip(),
    port=puerto,
    database=n_db.strip(),
    query={"charset": "utf8mb4"}
)

engine_prime = create_engine(url_object)
engine_clasico = get_engine(n_db)

try:    
    with engine_prime.connect() as conn:
        print("--- CONEXIÓN A PRIME EXITOSA ---")
except Exception as e:
    print(f"ERROR DE CONEXION PRIME: {e}")

# --- DEFINICIÓN DE VARIABLES GLOBALES ---
TAMANO_LOTE = 5000
TABLA_DESTINO = "Base"

def seleccion_archivo_excel():
    for arg in sys.argv:
        if arg.endswith(('.xlsx', '.xlsb', '.csv')):
            return arg
    print("[ERROR] No se recibió la ruta del archivo Excel.")
    sys.exit(1)

EXCEL_PATH = seleccion_archivo_excel()
REPORTES_DIR = os.environ.get("BASE_SQL_REPORT_DIR", "/tmp/base_para_sql_reportes")

def asegurar_directorio_reportes():
    try:
        os.makedirs(REPORTES_DIR, exist_ok=True)
        return REPORTES_DIR
    except Exception as e:
        print(f"[WARN] No se pudo crear directorio de reportes '{REPORTES_DIR}': {e}")
        return "/tmp"

# ---------------------------------------------------------
# FUNCIONES COMPARTIDAS (Se usan tanto en Clásico como en Prime)
# ---------------------------------------------------------

def limpiar_columna(series):
    res = series.astype(str).str.replace('_x000D_', ' ')
    res = res.str.replace(r'_x[0-9A-Fa-f]{4}_', ' ', regex=True)
    res = res.apply(lambda x: "".join(c for c in unicodedata.normalize('NFKD', x) 
                                     if unicodedata.category(c) != 'Mn'))

    res = res.str.replace(r'\s+', ' ', regex=True).str[:255].str.strip()
    
    return res.replace(['nan', 'NAN', 'None', 'NONE', 'NULO', 'N/A', '', 'NAT'], 'NULL_VAL')

def convertir_fecha_excel(series):
    s = series.copy()
    s = s.replace(['', 'NULL_VAL', 'nan', 'NaN', 'None', 'NONE', 'NULO', 'N/A', 'NAT'], np.nan)

    # openpyxl entrega las fechas como datetime64 directamente — formatear sin conversión extra.
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime('%Y-%m-%d').where(s.notna(), None)

    valores_numericos = pd.to_numeric(s, errors='coerce')
    fechas_excel = pd.to_datetime(valores_numericos, unit='D', origin='1899-12-30', errors='coerce')

    # Evita interpretar seriales numericos como epoch 1970 en parseo directo.
    s_texto = s.where(valores_numericos.isna(), np.nan)
    # Intentar formato exacto primero para evitar ambigüedad day/month.
    fechas_texto = pd.to_datetime(s_texto, format='%Y-%m-%d %H:%M:%S', errors='coerce')
    fechas_texto = fechas_texto.where(fechas_texto.notna(),
                                      pd.to_datetime(s_texto, format='%Y-%m-%d', errors='coerce'))
    fechas_texto = fechas_texto.where(fechas_texto.notna(),
                                      pd.to_datetime(s_texto, dayfirst=False, errors='coerce'))
    fechas_finales = fechas_excel.where(fechas_excel.notna(), fechas_texto)

    return fechas_finales.dt.strftime('%Y-%m-%d').where(fechas_finales.notna(), None)

def normalizar_nombres_columnas(df):
    mapa_columnas = {
        "id": "NumeroOc",
        "esp comprador": "EspComprador",
        "esp proveedor": "EspProveedor",
        "razon social cliente": "RazonSocialCliente",
        "sucursal proveedor": "SucursalProveedor",
        "presentacion": "Comp",
        "un medida pht": "MedidaPHT",
        "cant pht": "Cant",
        "precio pht": "PrecioPht",
        "valor total": "ValorTotal",
        "clase terap 4nivel": "CodCt4",
        "desc ct 4nivel": "DescCt4",
        "rut cliente": "RutCliente",
        "comuna cliente": "ComunaCliente",
        "rut proveedor": "RutProveedor",
        "tipo oc": "TipoOc",
        "id licitacion": "IdLicitacion"
    }

    nuevos_nombres = {}
    for col in df.columns:
        col_limpia = str(col).strip().replace("_", " ")
        col_limpia = "".join(
            c for c in unicodedata.normalize('NFKD', col_limpia)
            if unicodedata.category(c) != 'Mn'
        ).lower()
        col_limpia = " ".join(col_limpia.split())
        nuevos_nombres[col] = mapa_columnas.get(col_limpia, str(col).strip())

    return df.rename(columns=nuevos_nombres)

def construir_paccomppres_df(df):
    for col in ["Pactivo", "Comp", "MedidaPHT"]:
        if col not in df.columns:
            df[col] = None

    def unir_campos(row):
        partes = []
        for col in ["Pactivo", "Comp", "MedidaPHT"]:
            valor = row.get(col)
            if valor is None or str(valor).strip().lower() in ["", "nan", "none", "nat"]:
                continue
            partes.append(str(valor).strip())
        return "-".join(partes) if partes else None

    df["paccomppres"] = df.apply(unir_campos, axis=1)
    return df

def diagnostico_columna_fecha(df, etiqueta):
    try:
        if "Fecha" not in df.columns:
            print(f"[DIAG {etiqueta}] Columna 'Fecha' NO existe en el archivo.")
            return

        muestra_cruda = df["Fecha"].head(8).tolist()
        muestra_convertida = convertir_fecha_excel(df["Fecha"].head(8)).tolist()
        total_no_nulo = int(df["Fecha"].notna().sum())
        total_convertible = int(pd.Series(convertir_fecha_excel(df["Fecha"])).notna().sum())

        print(f"[DIAG {etiqueta}] Columnas detectadas: {list(df.columns)}")
        print(f"[DIAG {etiqueta}] Muestra cruda 'Fecha': {muestra_cruda}")
        print(f"[DIAG {etiqueta}] Muestra convertida 'Fecha': {muestra_convertida}")
        print(f"[DIAG {etiqueta}] Fecha no nula: {total_no_nulo} | Fecha convertible: {total_convertible}")
    except Exception as e:
        print(f"[WARN] Fallo diagnóstico de columna Fecha ({etiqueta}): {e}")

def verificar_integridad_total(df_excel_original, columnas_sql, engine, servidor_label):
    print(f"\n--- Validando datos en {servidor_label} ---")

    try:
        df_sql = pd.read_sql("SELECT * FROM `{0}`".format(TABLA_DESTINO), con=engine)

        count_excel = len(df_excel_original)
        count_sql   = len(df_sql)
        print(f"Registros Excel: {count_excel:,} | Registros SQL: {count_sql:,}")

        # 1. Conteo exacto — error crítico si no coincide.
        if count_excel != count_sql:
            print(f"\nERROR CRÍTICO: El conteo de filas no coincide.")
            print(f"  Excel={count_excel:,}  vs  SQL={count_sql:,}  (delta={count_excel - count_sql:+,})")
            sys.exit(1)

        # 2. Totales de columnas numéricas críticas — error crítico si no coinciden.
        CRITICAS = {
            "Cant":       {"decimales": 0, "tolerancia": 1},
            "ValorTotal": {"decimales": 0, "tolerancia": 1},
            "PrecioPht":  {"decimales": 1, "tolerancia": 1.0},
        }
        errores = []
        for col, cfg in CRITICAS.items():
            if col not in df_excel_original.columns or col not in df_sql.columns:
                continue
            dec = cfg["decimales"]
            tol = cfg["tolerancia"]
            suma_excel = pd.to_numeric(df_excel_original[col], errors='coerce').fillna(0).round(dec).sum()
            suma_sql   = pd.to_numeric(df_sql[col],            errors='coerce').fillna(0).round(dec).sum()
            if abs(suma_excel - suma_sql) > tol:
                errores.append(f"  {col}: Excel={suma_excel:,.{dec}f}  vs  SQL={suma_sql:,.{dec}f}")

        if errores:
            print(f"\nERROR CRÍTICO: Los totales numéricos no coinciden:")
            for e in errores:
                print(e)
            sys.exit(1)

        print(f"\nEXCELENTE: Validación exitosa en {servidor_label}.")
        print(f"  Filas: {count_sql:,} OK  |  Cant OK  |  ValorTotal OK  |  PrecioPht OK")
        return True

    except SystemExit:
        raise
    except Exception as e:
        print(f"Error en validación {servidor_label}: {e}")
        return False

# ---------------------------------------------------------
# FASE 2: FUNCIONES EXCLUSIVAS DEL PRIME
# ---------------------------------------------------------

def sincronizacion_tabla_pk(engine):
    print("\n--- Iniciando copia de tablas... ---")
    TABLA_PK = "base_para_rk"
    try:
        with engine.connect() as conn:
            print(f"Eliminando tabla temporal: {TABLA_PK}...")
            time.sleep(5)
            conn.execute(text(f"DROP TABLE IF EXISTS `{TABLA_PK}`"))

            print(f"Clonando estructura desde {TABLA_DESTINO}...")
            conn.execute(text(f"CREATE TABLE `{TABLA_PK}` LIKE `{TABLA_DESTINO}`"))

            columnas_a_borrar = [
                "Id", "EspComprador", "EspProveedor", "ComunaCliente", "TipoOc",
                "IdLicitacion", "RazonSocialProveedores", "RazonSocialProveedor"
            ]

            print("Filtrando estructura de la tabla...")
            drops = [f"DROP COLUMN `{c}`" for c in columnas_a_borrar]
            sql_drop = f"ALTER TABLE `{TABLA_PK}` {', '.join(drops)}"

            try:
                conn.execute(text(sql_drop))
            except Exception as e:
                print(f"Nota: Algunas columnas ya no estaban presentes: {e}")

            res = conn.execute(text(f"""
                SELECT COLUMN_NAME 
                FROM information_schema.COLUMNS 
                WHERE TABLE_NAME = '{TABLA_PK}' 
                AND TABLE_SCHEMA = DATABASE()
            """))

            cols_finales = [row[0] for row in res]
            cols_str = ", ".join([f"`{c}`" for c in cols_finales])

            print(f"Transfiriendo registros a {TABLA_PK}...")
            conn.execute(text(f"""
                INSERT INTO `{TABLA_PK}` ({cols_str})
                SELECT {cols_str} FROM `{TABLA_DESTINO}`                
            """))

            conn.execute(text("COMMIT"))
            print(f"Tabla {TABLA_PK} reconstruida")

            print("Sincronizando con fecha_ranking...")

            res_periodos = conn.execute(text(f"""
                SELECT DISTINCT
                    DATE_FORMAT (Fecha, '%Y-%m') as f_id,
                    MONTH(Fecha) as n_num,
                    YEAR(Fecha) as a_val
                FROM `{TABLA_PK}`
                WHERE Fecha IS NOT NULL                                  
            """)).fetchall()

            meses_es = {
                1:"Enero", 2:"Febrero", 3:"Marzo", 4: "Abril", 
                5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 
                9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
            }

            with engine.begin() as connection:
                for row in res_periodos:
                    val_f_id = str(row[0])
                    val_month_num = row[1]
                    val_year = row[2]
                    
                    nombre_mes = meses_es[val_month_num]
                    val_strNombre = f"{nombre_mes} {val_year}" 

                    sql_fechas = text("""
                        INSERT IGNORE INTO `fecha_ranking` (strNombreFecha, datFecha)
                        VALUES (:nom, :fec)
                    """)
                        
                    connection.execute(sql_fechas, {"nom": val_strNombre, "fec": val_f_id})
                    print(f">>> [FECHA] Registrando periodo en tabla: {val_strNombre} | ID: {val_f_id}")

                print(f"--- [OK] Tabla 'fecha_ranking' sincronizada con el Excel ---")

    except Exception as e:
        print(f"Error en la sincronizacion: {e}")

def validacion_de_tablas(engine):
    print("\n--- Verificando copia de datos, por favor espere... ---")
    TABLA_ORIGEN = "Base"
    TABLA_DESTINO_RK = "base_para_rk"

    try:
        with engine.connect() as conn:
            query = text(f"""
                SELECT
                    (SELECT COUNT(*) FROM `{TABLA_ORIGEN}`) as total_base,
                    (SELECT COUNT(*) FROM `{TABLA_DESTINO_RK}`) as total_rk,
                    (SELECT SUM(ValorTotal) FROM `{TABLA_ORIGEN}`) as suma_base,
                    (SELECT SUM(ValorTotal) FROM `{TABLA_DESTINO_RK}`) as suma_rk
            """)

            res = conn.execute(query).fetchone()
            count_base, count_rk, sum_base, sum_rk = res

            if count_base == count_rk and abs((sum_base or 0) - (sum_rk or 0)) < 0.01:
                print("=====================================================")
                print("\n Verificacion correcta, ambas tablas son identicas.")
                print("=====================================================")
            else:
                print(f"\n ALERTA: Se han detectado diferencias entre las tablas: {abs(count_base - count_rk)}.")

    except Exception as e:
        print(f"no se pudo completar la validacion final: {e}")

def sincronizacion_ranking_incremental(engine):
    print("\n--- Iniciando actualizacion de historico (ranking) ---")
    TABLA_ESPEJO = "base_para_rk"
    TABLA_HISTORICA = "ranking"

    try:
        with engine.connect() as conn:
            print("Calculando rango de fechas en la nueva data...")
            res_rango = conn.execute(text(f"SELECT MIN(Fecha), MAX(Fecha) FROM `{TABLA_ESPEJO}`")).fetchone()
            f_min, f_max = res_rango[0], res_rango[1]

            if f_min is None:
                print("La tabla espejo está vacía. Abortando.")

            res_previo = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLA_HISTORICA}`")).fetchone()
            conteo_antes = res_previo[0]

            print(f"Fechas detectadas que se van a truncar: {f_min} hasta {f_max}")
            time.sleep(2)

            print(f"Rango a refrescar en Ranking: {f_min} hasta {f_max}")
            print(f"Registros actuales en Ranking: {conteo_antes}")

            print(f"Eliminando datos antiguos en '{TABLA_HISTORICA}'...")
            sql_del = text(f"DELETE FROM `{TABLA_HISTORICA}` WHERE Fecha BETWEEN :inicio AND :fin")
            conn.execute(sql_del, {"inicio": f_min, "fin": f_max})

            print(f"Insertando nueva data corregida desde {TABLA_ESPEJO}...")
            sql_ins = text(f"""
                INSERT INTO `{TABLA_HISTORICA}` (
                    NumeroOc, RazonSocialCliente, SucursalProveedor, Pactivo, Comp, MedidaPHT,
                    Cant, PrecioPht, ValorTotal, Fecha, CodCt4, DescCt4, RutCliente,
                    RutProveedor, Instituciones, Region, CorporacionesPHT, paccomppres
                )
                SELECT 
                    LEFT(NumeroOc, 255), LEFT(RazonSocialCliente, 255), LEFT(SucursalProveedor, 255), 
                    LEFT(Pactivo, 255), LEFT(Comp, 255), LEFT(MedidaPHT, 255),
                    Cant, PrecioPht, ValorTotal, Fecha, LEFT(CodCt4, 255), LEFT(DescCt4, 255), 
                    LEFT(RutCliente, 255), LEFT(RutProveedor, 255), LEFT(Instituciones, 255), 
                    LEFT(Region, 255), LEFT(CorporacionesPHT, 255), LEFT(paccomppres, 255)
                FROM `{TABLA_ESPEJO}`
            """)

            conn.execute(sql_ins)
            conn.execute(text("COMMIT"))
            
            res_final = conn.execute(text(f"SELECT COUNT(*) FROM `{TABLA_HISTORICA}`")).fetchone()
            
            print("============================================")
            print(f"El histórico 'ranking' ha sido actualizado.")
            print(f"Filas finales en Ranking: {res_final[0]}")
            print("============================================")

    except Exception as e:
        print(f"Error en la actualización de Ranking: {e}")

# ---------------------------------------------------------
# FASE 2: EJECUCIÓN PRIME
# ---------------------------------------------------------

def ejecutar_migracion_prime(engine_prime):
    print("\n==============================================================================")
    print(" >>> Iniciando Fase 2: Ejecucion en Servidor Prime <<<")
    try:
        print("--- Ajustando estructura y limpiando tabla en Prime ---")
        with engine_prime.connect() as conn:
            conn.execute(text("TRUNCATE TABLE `{0}`".format(TABLA_DESTINO)))
            try: conn.execute(text("COMMIT")) 
            except: pass

        print("Leyendo archivo {0}...".format(EXCEL_PATH))
        try:
            if EXCEL_PATH.lower().endswith('.xlsb'):
                df_completo = pd.read_excel(EXCEL_PATH, engine='pyxlsb')
            else:
                df_completo = pd.read_excel(EXCEL_PATH, engine='openpyxl')
        
        except Exception as e:
            print("Error al leer con openpyxl: {0}".format(e))
            df_completo = pd.read_excel(EXCEL_PATH)

        df_completo.columns = [str(c).strip() for c in df_completo.columns]
        df_completo = normalizar_nombres_columnas(df_completo)
        df_completo = construir_paccomppres_df(df_completo)
        diagnostico_columna_fecha(df_completo, "PRIME-LECTURA")

        if 'SucursalProveedor' in df_completo.columns:
            df_completo['RazonSocialProveedores'] = df_completo['SucursalProveedor']
            df_completo['RazonSocialProveedor'] = df_completo['SucursalProveedor']

        columnas_sql = [
            "NumeroOc", "EspComprador", "EspProveedor", "RazonSocialCliente", "SucursalProveedor", 
            "Pactivo", "Comp", "MedidaPHT", "Cant", "PrecioPht", "ValorTotal", "Fecha", 
            "CodCt4", "DescCt4", "RutCliente", "ComunaCliente", "RutProveedor", 
            "Instituciones", "Region", "TipoOc", "IdLicitacion", "CorporacionesPHT", 
            "paccomppres", "RazonSocialProveedores", "RazonSocialProveedor"
        ]

        for col in columnas_sql:
            if col not in df_completo.columns:
                df_completo[col] = None

        total_filas = len(df_completo)
        print("Iniciando subida de {0} filas por lotes en Prime...".format(total_filas))

        for i in range(0, total_filas, TAMANO_LOTE):
            lote = df_completo.iloc[i : i+ TAMANO_LOTE].copy()

            if 'SucursalProveedor' in lote.columns:
                lote['RazonSocialProveedores'] = lote['SucursalProveedor']
                lote['RazonSocialProveedor'] = lote['SucursalProveedor']

            for col in columnas_sql:
                if col in ["Cant", "PrecioPht", "ValorTotal", "Fecha"]:
                    continue
                
                if col not in lote.columns:
                    lote[col] = None
                else:
                    lote[col] = limpiar_columna(lote[col])
                    lote[col] = lote[col].replace('NULL_VAL', None)
            
            for col in ["Cant", "ValorTotal"]:
                if col in lote.columns:
                    lote[col] = pd.to_numeric(lote[col], errors='coerce').fillna(0).round(0).astype('int64')

            if "PrecioPht" in lote.columns:
                lote["PrecioPht"] = pd.to_numeric(lote["PrecioPht"], errors='coerce').fillna(0).round(1).astype(float)

            if "Fecha" in lote.columns:
                lote["Fecha"] = convertir_fecha_excel(lote["Fecha"])

            # Recalcula sobre campos ya normalizados para evitar desfaces en validación.
            lote = construir_paccomppres_df(lote)
            lote_final = lote[columnas_sql].where(pd.notnull(lote[columnas_sql]), None)
            lote_final.to_sql(name=TABLA_DESTINO, con=engine_prime, if_exists='append', index=False)

            fila_final = min(i + TAMANO_LOTE, total_filas)
            print("Progreso Prime: {0}/{1} ({2:.1f}%)".format(fila_final, total_filas, (fila_final/total_filas)*100))

        print("============================================")
        print("\nSubida finalizada. Iniciando validación en Prime...")
        print("============================================")

        validacion_exitosa = verificar_integridad_total(df_completo, columnas_sql, engine_prime, "PRIME")

        if validacion_exitosa:
            sincronizacion_tabla_pk(engine_prime)
            sincronizacion_ranking_incremental(engine_prime)
            validacion_de_tablas(engine_prime)
        else:
            print("\nABORTANDO: Falló la validación en Prime. No se actualizará el Ranking.")

    except Exception as e:
        print("Error en migración Prime: {0}".format(str(e)))

# ---------------------------------------------------------
# FASE 1: EJECUCIÓN CLÁSICO
# ---------------------------------------------------------

def ejecutar_migracion_clasico(engine_clasico, engine_prime):
    try:
        print(f"--- Intentando conexion a Clasico ---")
        print("======================================================================================================")
        print("Para mantener la fiabilidad de los datos por favor no cierre esta pagina hasta que el proceso termine.")
        print("Adicionalmente, los datos que se validen puede que se tarden un tiempo en procesar.")
        print("======================================================================================================")
        print("INICIANDO EN 15 SEGUNDOS...")
        time.sleep(15)
        with engine_clasico.connect() as conn:
            print("Ajustando estructura en Clasico...")
            conn.execute(text("TRUNCATE TABLE `{0}` ".format(TABLA_DESTINO)))
            try: conn.commit()
            except: pass

        print("Leyendo documento Excel, espere un poco...")
        try:
            df_completo = pd.read_excel(EXCEL_PATH, engine='openpyxl')
        except Exception:
            df_completo = pd.read_excel(EXCEL_PATH)

        df_completo.columns = [str(c).strip() for c in df_completo.columns]
        df_completo = normalizar_nombres_columnas(df_completo)
        df_completo = construir_paccomppres_df(df_completo)
        diagnostico_columna_fecha(df_completo, "CLASICO-LECTURA")

        # Alinea el dataframe base con lo que realmente se inserta en lotes.
        if 'SucursalProveedor' in df_completo.columns:
            df_completo['RazonSocialProveedores'] = df_completo['SucursalProveedor']
            df_completo['RazonSocialProveedor'] = df_completo['SucursalProveedor']

        columnas_sql = [
            "NumeroOc", "EspComprador", "EspProveedor", "RazonSocialCliente", "SucursalProveedor", 
            "Pactivo", "Comp", "MedidaPHT", "Cant", "PrecioPht", "ValorTotal", "Fecha", 
            "CodCt4", "DescCt4", "RutCliente", "ComunaCliente", "RutProveedor", 
            "Instituciones", "Region", "TipoOc", "IdLicitacion", "CorporacionesPHT", 
            "paccomppres", "RazonSocialProveedores", "RazonSocialProveedor"
        ]

        total_filas = len(df_completo)
        print("Éxito al leer {0} filas. Iniciando subida en Clasico...".format(total_filas))

        for i in range(0, total_filas, TAMANO_LOTE):
            lote = df_completo.iloc[i : i + TAMANO_LOTE].copy()
            lote.columns = [str(c).strip() for c in lote.columns]
            lote = normalizar_nombres_columnas(lote)

            if 'SucursalProveedor' in lote.columns:
                lote['RazonSocialProveedores'] = lote['SucursalProveedor']
                lote['RazonSocialProveedor'] = lote['SucursalProveedor']
            
            for col in columnas_sql:
                if col in ["Cant", "PrecioPht", "ValorTotal", "Fecha"]:
                    continue
                if col not in lote.columns:
                    lote[col] = None
                else:
                    lote[col] = lote[col].apply(lambda x:
                        "".join(c for c in unicodedata.normalize('NFKD', str(x))
                            if unicodedata.category(c) != 'Mn')
                        if pd.notna(x) and x not in ['nan', 'NaN', 'None', ''] else None
                    ).str.replace(r'\s+', ' ', regex=True).str.strip().str[:255]

            for col in ["Cant", "ValorTotal"]:
                if col in lote.columns:
                    lote[col] = pd.to_numeric(lote[col], errors='coerce').fillna(0).round(0).astype('int64')

            if "PrecioPht" in lote.columns:
                lote["PrecioPht"] = pd.to_numeric(lote["PrecioPht"], errors='coerce').fillna(0).round(1).astype(float)

            if "Fecha" in lote.columns:
                lote["Fecha"] = convertir_fecha_excel(lote["Fecha"])

            # Recalcula sobre campos ya normalizados para evitar desfaces en validación.
            lote = construir_paccomppres_df(lote)
            lote = lote[columnas_sql]
            lote = lote.where(pd.notnull(lote), None)
            lote.to_sql(name=TABLA_DESTINO, con=engine_clasico, if_exists='append', index=False)

            fila_final_lote = min(i + TAMANO_LOTE, total_filas)
            print(f"Lote cargado: [{fila_final_lote}/{total_filas}] ({(fila_final_lote/total_filas)*100:.1f}%)")

        print("\nCarga completada. Iniciando validación en Clasico...")
        validacion_exitosa = verificar_integridad_total(df_completo, columnas_sql, engine_clasico, "CLASICO")

        if validacion_exitosa:
            ejecutar_migracion_prime(engine_prime)

    except Exception as e:
        print("Error en migración Clásico: {0}".format(str(e)))

if __name__ == "__main__":
    # .69
    engine_servidor_clasico = engine_clasico
    
    #.68
    engine_servidor_prime = engine_prime
    
    ejecutar_migracion_clasico(engine_servidor_clasico, engine_servidor_prime)
