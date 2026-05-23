#!/usr/bin/python3.6
import numpy as np
import sys
import os
import re
import datetime
import requests
import time
import gc
import unicodedata
from bs4 import BeautifulSoup
from sqlalchemy import text
from urllib.parse import quote_plus
from datetime import timedelta
from datetime import datetime
from dateutil.relativedelta import relativedelta
from vault_linux_helper import VaultLinuxManager

sys.path.insert(0, '/usr/local/lib/python3.6/site-packages')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('/usr/local/lib/python3.6/site-packages')

try:
    import pandas as pd
    import sqlalchemy
    from sqlalchemy import create_engine
    import mysql.connector
except ImportError as e:
    print(f"ERROR CRÍTICO: No se pudo importar una librería: {e}")
    sys.exit(1)

PANDAS_VERSION = int(pd.__version__.split('.')[0])

#db_nombre = "prueba_practica" #<-- pruebas
db_nombre = "analisis_precios" #<-- Produccion

#engine_prime = get_engine("analisis_precios")

#TABLA_DESTINO = "adquisiciones" #<-- pruebas
TABLA_DESTINO = "Base" #<-- Produccion
TAMANO_LOTE = 5000

TABLA_VALIDADOS = "adquisiciones_validadas"
MODO_INICIO = "mantenimiento"
DIRECTORIO_ACTUAL = os.path.dirname(os.path.abspath(__file__))

def get_engine_by_server(nombre_bd, servidor_objetivo):
    v = VaultLinuxManager()
    creds = v.get_linux_mysql_connection(database=nombre_bd, server=servidor_objetivo, return_dict=True)
    usuario = creds['user']
    password = quote_plus(creds['password'])
    host = creds['host']
    conn_str = "mysql+mysqlconnector://{0}:{1}@{2}/{3}".format(usuario, password, host, nombre_bd)
    return create_engine(conn_str, pool_recycle=3600)

engine = get_engine_by_server("analisis_precios", "prime")
engine_clasico = get_engine_by_server("licitaciones_diarias_total", "clasico")

def unir_dataframes(df_principal, df_nuevo):
    if PANDAS_VERSION >= 2:
        return pd.concat([df_principal, df_nuevo], ignore_index=True)
    else:
        return df_principal.append(df_nuevo, ignore_index=True)

def seleccion_archivo_excel():
    for arg in sys.argv:
        if arg.endswith(('.xlsx', '.xls', '.csv')):
            return arg
            
    archivos = [f for f in os.listdir(DIRECTORIO_ACTUAL) if f.endswith(('.xlsx', '.csv')) and not f.startswith('~$')]
    if archivos:
        return os.path.join(DIRECTORIO_ACTUAL, archivos[0])
        
    print("[ERROR] No se encontro el archivo Excel.")
    sys.exit(1)

EXCEL_PATH = seleccion_archivo_excel()

def normalizar_texto(texto):
    if not texto or pd.isna(texto) or str(texto).lower() == 'nan':
        return None
    
    texto = str(texto)

    forma_nfd = unicodedata.normalize('NFD', texto)
    texto_limpio = "".join([c for c in forma_nfd if not unicodedata.combining(c)])

    return texto_limpio.strip()

def logica_optimizada(row_interna, dict_tiempos):
    t = dict_tiempos.get(row_interna['Adquisicion'], row_interna['Tiempo_contrato'])
    if not t or str(t).strip().lower() in ['', 'none', 'nan', '0'] or not row_interna['FechaSQL']:
        return t, None
    
    nums = re.sub(r'[^0-9]', '', str(t))
    if not nums: return t, None
    
    try:
        val = int(nums)
        f_base = pd.to_datetime(row_interna['FechaSQL'])
        t_low = str(t).lower()
        if 'mes' in t_low: f_fin = (f_base + pd.DateOffset(months=val)).date()
        elif 'dia' in t_low: f_fin = (f_base + pd.DateOffset(days=val)).date()
        elif 'semana' in t_low: f_fin = (f_base + pd.DateOffset(weeks=val)).date()
        elif 'año' in t_low or 'ano' in t_low: f_fin = (f_base + pd.DateOffset(years=val)).date()
        else: f_fin = None
        return t, f_fin
    except: 
        return t, None

def ejecutar_migracion():
    try:
        metodo_sql = 'multi' if PANDAS_VERSION >= 2 else None
        #engine = get_engine(db_nombre)
        print("\n--- CONEXION EXITOSA ---")

        print(f"[INFO] LEYENDO ARCHIVO: {os.path.basename(EXCEL_PATH)}...")
        df_completo = pd.read_excel(EXCEL_PATH, engine='openpyxl')
        
        df_completo.columns = [str(c).strip() for c in df_completo.columns]
        df_completo['Adquisicion'] = df_completo['Adquisicion'].astype(str).str.strip()
        df_completo['RazonSocialProveedores'] = df_completo['SucursalProveedor']
        df_completo = df_completo[df_completo['Adquisicion'] != 'nan']
        #df_completo['Producto'] = pd.to_numeric(df_completo['Producto'], errors='coerce')

        columnas_sql = [
            "Adquisicion", "Rut_cliente", "Fecha_Publicacion", "Fecha_Cierre",
            "Cantidad", "Producto", "Rut_Proveedor", "Estado", "FechaAdjudicacion",
            "FechaSQL", "Precio_unit", "Cantadjudicada", "Valor_Adjudicado", 
            "SucursalProveedor", "Pactivo", "Composicion", "Presentacion", 
            "RazonSocialCliente", "CorpProveedor", "EspComprador", 
            "Esp_Proveedores", "finDeContrato", "Tiempo_contrato", 
            "RazonSocialProveedores"
        ]
        
        df_subida = df_completo[[col for col in df_completo.columns if col in columnas_sql]].copy()
        
        columnas_fecha = ["Fecha_Publicacion", "Fecha_Cierre", "FechaAdjudicacion", "FechaSQL"]
        for col in columnas_fecha:
            if col in df_subida.columns:
                df_subida[col] = pd.to_datetime(df_subida[col], dayfirst=True, errors='coerce').dt.strftime('%Y-%m-%d')
        
        columnas_texto = [
            "Adquisicion", "SucursalProveedor", "Pactivo", "Composicion", 
            "Presentacion", "RazonSocialCliente", "CorpProveedor", 
            "EspComprador", "Esp_Proveedores", "RazonSocialProveedores"
        ]

        for col in columnas_texto:
            if col in df_subida.columns:
                df_subida[col] = df_subida[col].apply(normalizar_texto)

        df_subida = df_subida.replace({np.nan: None, "nan": None})

        print(f"[INFO] BUSCANDO DATOS EXISTENTES...")
        try:
            existentes_query = f"SELECT DISTINCT Adquisicion FROM {TABLA_DESTINO}"
            ids_en_db = pd.read_sql(existentes_query, engine)['Adquisicion'].astype(str).tolist()
            
            df_subida = df_subida[~df_subida['Adquisicion'].astype(str).isin(ids_en_db)]
        except Exception as e:
            print(f"[ALERTA] NO SE PUDO FILTRAR DUPLICADOS: {e}")

        if not df_subida.empty:
            print(f"[INFO] INSERTANDO {len(df_subida)} FILAS NUEVAS...")
            df_subida.to_sql(
                TABLA_DESTINO, 
                engine, 
                if_exists='append', 
                index=False, 
                chunksize=5000, 
                method=metodo_sql
            )
            print("[OK] CARGA FINALIZADA CON EXITO")
        else:
            print("[OK] TODAS LAS FILAS YA EXISTEN EN LA BASE DE DATOS. SALTANDO SUBIDA.")

        print("[INFO] SINCRONIZANDO RAZON SOCIAL Y PREPARANDO TABLAS...")
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {TABLA_DESTINO} SET RazonSocialProveedores = SucursalProveedor WHERE RazonSocialProveedores IS NULL OR RazonSocialProveedores = ''"))

            if MODO_INICIO == "truncate":
                print(f"[ALERTA] MODO TRUNCATE ACTIVADO. LIMPIANDO TABLAS EN 10 SEGUNDOS...")
                time.sleep(10)
                conn.execute(text(f"TRUNCATE TABLE {TABLA_DESTINO}"))
                #conn.execute(text(f"TRUNCATE TABLE {TABLA_VALIDADOS}"))

            with engine_clasico.begin() as conn_clasico: 
                conn_clasico.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLA_VALIDADOS} LIKE {TABLA_DESTINO}"))

                if MODO_INICIO == "truncate":
                    print(f"[ALERTA] LIMPIANDO TABLA VALIDADOS DE CLASICO EN 10 SEGUNDOS...")
                    time.sleep(10)
                    conn_clasico.execute(text(f"TRUNCATE TABLE {TABLA_VALIDADOS}"))
            
        print("[INFO] CREANDO TABLA TEMPORAL DE VINCULO...")
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS tmp_vinculo_licitaciones"))
            conn.execute(text("""
                CREATE TABLE tmp_vinculo_licitaciones (
                    Licitacion VARCHAR(100) PRIMARY KEY,
                    Tiempo_contrato VARCHAR(255)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """))
            
            print("[INFO] RESCATANDO DATOS DEL HISTORICO DESDE EL CLÁSICO...")
            query_historico = """
                SELECT Licitacion, Tiempo_contrato 
                FROM Licitaciones_diarias_practica 
                WHERE Tiempo_contrato IS NOT NULL 
                AND Tiempo_contrato NOT IN ('0', '', 'nan')
            """
            df_historico = pd.read_sql(query_historico, engine_clasico)

            print("[INFO] SUBIENDO PUENTE DE DATOS AL PRIME...")
            df_historico.to_sql('tmp_vinculo_licitaciones', engine, if_exists='replace', index=False)

            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX idx_licitaciones_tmp ON tmp_vinculo_licitaciones(Licitacion)"))
            
            print("[OK] VINCULO CON HISTORICO SINCRONIZADO EXITOSAMENTE.")

            conn.execute(text("CREATE INDEX idx_licitaciones_tmp ON tmp_vinculo_licitaciones(Licitacion)"))
            print("[OK] VINCULO CON HISTORICO CREADO Y GUARDADO EXITOSAMENTE.")

            # ==============================================================================
            # ⚠️ ADVERTENCIA DE RENDIMIENTO PARA QUIEN EJECUTE EL SCRIPT:
            # ==============================================================================
            # Si la tabla de destino ya contiene registros con la columna 'Tiempo_contrato' procesada,
            # el motor SQL realizará un Full Table Scan sobre la tabla maestra 
            # (actualmente, fecha 25/02/26 tiene 55 millones de filas)
            # para buscar nulos remanentes. Esto puede tardar muchas horas.
            #
            # RECOMENDACIÓN: Si solo se están realizando pruebas o scraping, aplicar un
            # TRUNCATE a la tabla donde se quiere pedir los datos antes de ejecutar. El tiempo de 
            # respuesta caerá de horas a practicamente segundos de procesamiento...
            #
            # ⚠️ TRUNCATE elimina toda la informacion de la tabla, el desarrollador del script no se
            # responsabiliza si se ha perdido informacion critica.
            # ==============================================================================

        print("[INFO] DESCARGANDO MAPEO A LA MEMORIA...")
        df_mapeo = pd.read_sql("SELECT Licitacion, Tiempo_contrato FROM tmp_vinculo_licitaciones", engine)
        dict_tiempos = dict(zip(df_mapeo['Licitacion'], df_mapeo['Tiempo_contrato']))
        del df_mapeo
        gc.collect()

        print("[INFO] PROCESANDO REGISTROS PENDIENTES EN MAESTRA...")
        df_destino = pd.read_sql(f"SELECT Adquisicion, FechaSQL, Tiempo_contrato FROM {TABLA_DESTINO} WHERE Tiempo_contrato IS NULL", engine)

        if not df_destino.empty:
            total_filas = len(df_destino)
            print(f"[INFO] CALCULANDO LOGICA PARA {total_filas} FILAS...")
            
            lista_res = []
            
            for i, (idx, fila) in enumerate(df_destino.iterrows(), 1):
                resultado = logica_optimizada(fila, dict_tiempos)
                lista_res.append(resultado)
                
                if i % 10000 == 0:
                    print(f"   > [PROGRESO] {i}/{total_filas} ({(i/total_filas)*100:.1f}%)")

            df_destino['Tiempo_contrato'] = [r[0] for r in lista_res]
            df_destino['finDeContrato'] = [r[1] for r in lista_res]
            
            df_upd = df_destino.dropna(subset=['finDeContrato']).copy()

            if not df_upd.empty:
                df_upd = df_upd.drop_duplicates(subset=['Adquisicion'])
                print(f"[INFO] SUBIENDO {len(df_upd)} REGISTROS CALCULADOS...")
                
                df_upd[['Adquisicion', 'Tiempo_contrato', 'finDeContrato']].to_sql(
                    'tmp_bulk_final', 
                    engine, 
                    if_exists='replace', 
                    index=False,
                    method=metodo_sql
                )
                
                with engine.begin() as conn:
                    print("[INFO] NORMALIZANDO IDIOMA DE TABLA TEMPORAL...")
                    conn.execute(text("ALTER TABLE tmp_bulk_final CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
                    conn.execute(text("ALTER TABLE tmp_bulk_final MODIFY COLUMN Adquisicion VARCHAR(100), ADD INDEX (Adquisicion)"))

                print("[INFO] ACTUALIZANDO TABLA MAESTRA...")
                with engine.begin() as conn:
                    conn.execute(text(f"""
                        UPDATE {TABLA_DESTINO} AS d
                        INNER JOIN tmp_bulk_final AS t 
                            ON d.Adquisicion = t.Adquisicion COLLATE utf8mb4_unicode_ci
                        SET d.Tiempo_contrato = t.Tiempo_contrato, 
                            d.finDeContrato = IF(t.Tiempo_contrato = '0', d.FechaSQL, t.finDeContrato)
                        WHERE d.Tiempo_contrato IS NULL
                    """))

                print("[INFO] ENVIANDO HALLAZGOS A TABLA DE VALIDADOS EN EL CLÁSICO...")
                query_validados = f"""
                    SELECT * FROM {TABLA_DESTINO} 
                    WHERE Adquisicion IN (SELECT Adquisicion FROM tmp_bulk_final)
                """
                df_validados = pd.read_sql(query_validados, engine)

                if not df_validados.empty:
                    df_validados.to_sql(TABLA_VALIDADOS, engine_clasico, if_exists='append', index=False)
                print("[OK] SINCRONIZACION DE HISTORICO COMPLETADA.")

        fallos_1 = scraping_tiempo_contrato(engine, engine_clasico)
        print("\n[INFO] INICIANDO SEGUNDO SCRAPING (REINTENTO DE DATOS NO PROCESADOS)")
        time.sleep(2)
        fallos_2 = scraping_tiempo_contrato(engine, engine_clasico)

        print("[INFO] NORMALIZANDO REGISTROS RESTANTES A '0' EN TABLA MAESTRA...")
        
        df_normalizados = pd.read_sql(f"SELECT * FROM {TABLA_DESTINO} WHERE Tiempo_contrato IS NULL", engine)
        
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {TABLA_DESTINO} SET Tiempo_contrato = '0', finDeContrato = FechaSQL WHERE Tiempo_contrato IS NULL"))
            
        if not df_normalizados.empty:
            df_normalizados['Tiempo_contrato'] = '0'
            df_normalizados['finDeContrato'] = df_normalizados['FechaSQL']
            print(f"[INFO] ENVIANDO {len(df_normalizados)} REGISTROS NORMALIZADOS A CLÁSICO...")
            df_normalizados.to_sql(TABLA_VALIDADOS, engine_clasico, if_exists='append', index=False)

        fallos_totales = (fallos_1 or []) + (fallos_2 or [])
        verificar_integridad_total(df_completo, columnas_sql, engine, fallos_totales)

    except Exception as e:
        print(f"\n[ERROR CRITICO] {str(e)}")

def scraping_tiempo_contrato(engine_prime, engine_clasico):
    print("\n[INFO] INICIANDO SCRAPING PARA DATOS FALTANTES...")
    ids_fallidos = []
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    with engine_prime.connect() as conn:
        query = text(f"""
            SELECT Adquisicion
            FROM {TABLA_DESTINO}
            WHERE Tiempo_contrato IS NULL 
            GROUP BY Adquisicion
        """)
        pendientes = conn.execute(query).fetchall()

    if not pendientes:
        print("[OK] NO HAY REGISTROS NULOS PENDIENTES.")
        return

    total_reg = len(pendientes)
    print(f"[INFO] SE ENCONTRARON {total_reg} REGISTROS NULOS.")

    for i, row in enumerate(pendientes, 1):
        id_lic = row[0]
        prefix = f"[{i}/{total_reg}] ID {id_lic}: "
        url = f"https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={id_lic}"
        
        exito = False
        intentos = 0

        while not exito and intentos < 3:
            try:
                response = session.get(url, timeout=15)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, 'html.parser')
                    elemento = soup.find('span', {'id': 'lblFicha7TiempoContrato'})
                    
                    if elemento and elemento.text.strip():
                        valor_web = elemento.text.strip()
                        mensaje_log = f"ENCONTRADO: {valor_web}"
                    else:
                        valor_web = '0'
                        mensaje_log = "NO DISPONIBLE (Marcado como 0 confirmado)"
                        
                    with engine.begin() as conn_upd:
                        sql_web = text(f"""
                            UPDATE {TABLA_DESTINO} 
                            SET Tiempo_contrato = :t,
                                finDeContrato = CASE 
                                    WHEN :t = '0' THEN FechaSQL

                                    WHEN :t LIKE '%hora%' 
                                        OR :t LIKE '%Hora%' 
                                        OR :t LIKE '%horas%' 
                                        OR :t LIKE '%Horas%'
                                        OR :t LIKE '%HORA%'
                                        OR :t LIKE '%HORAS%'
                                        THEN DATE_ADD(FechaSQL, INTERVAL CAST(REGEXP_REPLACE(:t, '[^0-9]', '') AS UNSIGNED) HOUR)

                                    WHEN :t LIKE '%dia%' 
                                        OR :t LIKE '%Dia%' 
                                        OR :t LIKE '%día%' 
                                        OR :t LIKE '%Día%'
                                        OR :t LIKE '%dias%' 
                                        OR :t LIKE '%Dias%' 
                                        OR :t LIKE '%días%' 
                                        OR :t LIKE '%Días%'
                                        OR :t LIKE '%DIA%'
                                        OR :t LIKE '%DÍA%'
                                        OR :t LIKE '%DIAS%'
                                        OR :t LIKE '%DÍAS%'
                                        THEN DATE_ADD(FechaSQL, INTERVAL CAST(REGEXP_REPLACE(:t, '[^0-9]', '') AS UNSIGNED) DAY)

                                    WHEN :t LIKE '%semana%' 
                                        OR :t LIKE '%Semana%' 
                                        OR :t LIKE '%semanas%' 
                                        OR :t LIKE '%Semanas%'
                                        OR :t LIKE '%SEMANA%'
                                        OR :t LIKE '%SEMANAS%'
                                        THEN DATE_ADD(FechaSQL, INTERVAL (CAST(REGEXP_REPLACE(:t, '[^0-9]', '') AS UNSIGNED) * 7) DAY)
                                    
                                    WHEN :t LIKE '%mes%' 
                                        OR :t LIKE '%Mes%' 
                                        OR :t LIKE '%meses%' 
                                        OR :t LIKE '%Meses%'
                                        OR :t LIKE '%MES%'
                                        OR :t LIKE '%MESES%'
                                        THEN DATE_ADD(FechaSQL, INTERVAL CAST(REGEXP_REPLACE(:t, '[^0-9]', '') AS UNSIGNED) MONTH)
                                                                    
                                    WHEN :t LIKE '%año%' 
                                        OR :t LIKE '%ano%' 
                                        OR :t LIKE '%Año%' 
                                        OR :t LIKE '%Ano%'
                                        OR :t LIKE '%años%' 
                                        OR :t LIKE '%Años%' 
                                        OR :t LIKE '%anos%' 
                                        OR :t LIKE '%Anos%'
                                        OR :t LIKE '%AÑO%'
                                        OR :t LIKE '%ANO%'
                                        OR :t LIKE '%AÑOS%'
                                        OR :t LIKE '%ANOS%'

                                        THEN DATE_ADD(FechaSQL, INTERVAL CAST(REGEXP_REPLACE(:t, '[^0-9]', '') AS UNSIGNED) YEAR)
                                        
                                    ELSE finDeContrato
                                END
                            WHERE Adquisicion = :id 
                            AND Tiempo_contrato IS NULL
                        """)

                        conn_upd.execute(sql_web, {"t": valor_web, "id": id_lic})

                    with engine_prime.begin() as conn_upd:
                        conn_upd.execute(sql_web, {"t": valor_web, "id": id_lic})

                    try:
                        df_fila = pd.read_sql(f"SELECT * FROM {TABLA_DESTINO} WHERE Adquisicion = '{id_lic}'", engine_prime)
                        if not df_fila.empty:
                            df_fila.to_sql(TABLA_VALIDADOS, engine_clasico, if_exists='append', index=False)
                        print(f"{prefix}{mensaje_log}, SINCRONIZANDO CON AMBAS TABLAS")
                    except Exception as e:
                        print(f"{prefix}[ERROR] ERROR SINCRONIZANDO A CLASICO: {e}")
                    
                    exito = True

                elif response.status_code == 403:
                    print(f"\n[ALERTA] ERROR 403 EN {id_lic}. PAUSA DE 1 MINUTO Y REINTENTANDO...")
                    time.sleep(60)

                elif response.status_code == 429:
                    print(f"\n[ALERTA] ERROR 429. PAUSA DE 40 SEGUNDOS...")
                    time.sleep(40)
                    intentos += 1

                else:
                    print(f"{prefix}Error HTTP {response.status_code}. Saltando...")
                    ids_fallidos.append({
                        "ID_Licitacion": id_lic,
                        "Motivo_Fallo": f"Error HTTP {response.status_code}",
                        "Fecha_Intento": datetime.now().strftime("%Y-%m-%d %H:%M")
                    })
                    exito = True

            except Exception as e:
                print(f"\n[ERROR DE RED] ID {id_lic}: {e}. Reintentando en 10 seg...")
                time.sleep(10)
                intentos += 1
        
        time.sleep(1.2)

    print("\n[OK] PROCESO DE SCRAPING FINALIZADO.")

def verificar_integridad_total(df_excel_original, columnas_sql, engine, ids_fallidos=None):
    print("\n[INFO] GENERANDO REPORTE DETALLADO...")
    if ids_fallidos is None: ids_fallidos = []
    
    try:
        query = f"SELECT Adquisicion, Tiempo_contrato FROM {TABLA_DESTINO}"
        df_db = pd.read_sql(query, engine)
        
        df_resumen = pd.DataFrame({
            "Descripcion": ["IDs unicos TOTALES", "IDs unicos con NULL", "IDs unicos con CERO"],
            "Cantidad": [
                len(df_db), 
                df_db['Tiempo_contrato'].isna().sum(), 
                (df_db['Tiempo_contrato'] == '0').sum()
            ]
        })

        base_path = os.path.abspath(os.path.join(DIRECTORIO_ACTUAL, "..", "storage", "app", "temp"))
        fecha_actual = datetime.now().strftime("%Y-%m-%d_%H-%M")
        nombre_archivo = f"reporte_{fecha_actual}.xlsx"
        ruta_reporte = os.path.join(base_path, nombre_archivo)

        with pd.ExcelWriter(ruta_reporte, engine='openpyxl') as writer:
            df_resumen.to_excel(writer, sheet_name='Resumen_General', index=False)
            
            if ids_fallidos:
                df_err = pd.DataFrame(ids_fallidos)
                df_err.to_excel(writer, sheet_name='Detalle_Fallos_Scraping', index=False)

        print("-" * 35)
        print(f"[OK] REPORTE CREADO: {nombre_archivo}")
        if ids_fallidos:
            print(f"[AVISO] SE REGISTRARON {len(ids_fallidos)} ERRORES EN EL EXCEL.")

    except Exception as e:
        print(f"[ERROR] NO SE PUDO GENERAR EL REPORTE: {str(e)}")

if __name__ == "__main__":
    ejecutar_migracion()