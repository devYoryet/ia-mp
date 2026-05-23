#!/usr/bin/env python3.6
"""
=============================================================
SCRIPT 1 de 2: Importar Excel de Importaciones → MySQL
=============================================================

PROPÓSITO:
    Lee un archivo Excel (.xlsm/.xlsx) con datos de importaciones
    y los inserta en la base de datos MySQL en una tabla cuyo
    nombre incluye el año y mes del parámetro --fecha.

    Ej: --fecha 2026-01-01  →  tabla: importaciones_2026_01
                               BD:    licitaciones_diarias_total_farma

FLUJO COMPLETO (2 scripts):
    1. Este script:  Excel → importaciones_YYYY_MM  (BD: licitaciones_diarias_total_farma, ver MYSQL_CONFIG)
    2. migrar_total_mes.py: importaciones_2026_01 → 202601 (BD: importaciones_total_mes)
                            → filtrar Campo144=29/30 → Importaciones (BD: importaciones_total_farma)

USO:
    python3 ImportOC.py --archivo "Importaciones Marzo 2026 v2.xlsm"
    python3 ImportOC.py --fecha 2026-03-01 --archivo "Importaciones Marzo 2026 v2.xlsm"

PARÁMETROS:
    --fecha   Opcional. Por defecto el mes se deduce del nombre del archivo si contiene mes (español) + año.
              Si el backend envía --fecha distinta, se ignora salvo --forzar-fecha.
    --forzar-fecha  Usar --fecha tal cual aunque el nombre del archivo indique otro mes (casos raros).
    --archivo Ruta al archivo Excel (.xlsm/.xlsx)
    IMPORTOC_LOG  (opcional) Ruta absoluta del archivo de log; si no, junto al script o en /tmp.
    --batch   Registros por inserción (default: 3000). Más alto = más rápido pero más RAM.
    --dry-run Solo valida tipos sin tocar la BD (ideal para probar antes de subir)

DEPENDENCIAS:
    pip install openpyxl mysql-connector-python
"""

import argparse
import os
import sys
import logging
import re
import tempfile
from datetime import datetime, date
from collections import defaultdict

# Meses en español para deducir --fecha desde el nombre del archivo (ej. "... Marzo 2026 ...")
_MESES_ES = (
    ('enero', 1), ('febrero', 2), ('marzo', 3), ('abril', 4), ('mayo', 5), ('junio', 6),
    ('julio', 7), ('agosto', 8), ('septiembre', 9), ('setiembre', 9),
    ('octubre', 10), ('noviembre', 11), ('diciembre', 12),
)
import openpyxl
import mysql.connector

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN DE CONEXIÓN MYSQL
# Se puede sobreescribir con variables de entorno para mayor seguridad.
# Ej: export MYSQL_PASSWORD='mi_clave'
# ─────────────────────────────────────────────────────────────

MYSQL_CONFIG = {
    'host':            os.getenv('MYSQL_HOST',     '10.0.0.69'),       # IP del servidor MySQL
    'port':            int(os.getenv('MYSQL_PORT', '3306')),            # Puerto (default 3306)
    'user':            os.getenv('MYSQL_USER',     'root'),             # Usuario
    'password':        os.getenv('MYSQL_PASSWORD', '@_Clasic0Root2025DB_M8qP3nP12'),
    'database':        os.getenv('MYSQL_DATABASE', 'licitaciones_diarias_total_farma'),
    'charset':         'utf8mb4',    # Soporte completo de Unicode (emojis, tildes, etc.)
    'use_unicode':     True,
    'connect_timeout': 30,           # Segundos antes de timeout de conexión
}

#MYSQL_CONFIG = {
#    'host':            os.getenv('MYSQL_HOST',     '10.0.0.69'),       # IP del servidor MySQL
#    'port':            int(os.getenv('MYSQL_PORT', '3306')),            # Puerto (default 3306)
#    'user':            os.getenv('MYSQL_USER',     'root'),             # Usuario
#    'password':        os.getenv('MYSQL_PASSWORD', '@_Clasic0Root2025DB_M8qP3nP12'),
#    'database':        os.getenv('MYSQL_DATABASE', 'licitaciones_diarias_total_farma'),
#    'charset':         'utf8mb4',    # Soporte completo de Unicode (emojis, tildes, etc.)
#    'use_unicode':     True,
#    'connect_timeout': 30,           # Segundos antes de timeout de conexión
#}


# ─────────────────────────────────────────────────────────────
# DEFINICIÓN DE TIPOS POR COLUMNA
# Estos sets indican qué tipo de conversión aplica a cada campo.
# Si agregas una columna nueva al Excel, debes clasificarla aquí.
# ─────────────────────────────────────────────────────────────

# Columnas que se guardan como texto libre (TEXT en MySQL)
TEXT_COLS = {
    'Campo4', 'Campo5', 'Campo6', 'Campo26', 'Campo64',
    'Campo111', 'Campo112', 'Campo113', 'Campo114', 'Campo115',
    'Campo116', 'Campo117', 'Campo133', 'Campo137', 'Campo139',
    'Campo141', 'Campo143', 'Campo182', 'Campo183', 'DESCRIPCION',
}

# Columnas numéricas enteras (BIGINT en MySQL)
# Nota: Campo149 tiene decimales en el Excel (1.2, 2.4) → se truncan a entero
BIGINT_COLS = {
    'Campo186', 'Campo187', 'Campo188',
    'Campo10', 'Campo11', 'Campo12', 'Campo13', 'Campo14', 'Campo15',
    'Campo24', 'Campo39', 'Campo45', 'Campo46', 'Campo65',
    'Campo50', 'Campo51', 'Campo52',
    'Campo152', 'Campo153',
    'Campo122', 'Campo123', 'Campo127', 'Campo128',
    'Campo144', 'Campo145', 'Campo146', 'Campo147', 'Campo148', 'Campo149',
    'Campo154', 'Campo178', 'Campo179', 'Campo184', 'Campo185',
}

# Columnas numéricas con decimales (DOUBLE en MySQL)
DOUBLE_COLS = {'Campo158'}

# Columna de fecha (DATE en MySQL) — siempre recibe el valor del parámetro --fecha
DATE_COLS = {'datFecha'}

# ─────────────────────────────────────────────────────────────
# ORDEN DE COLUMNAS EN LA TABLA MySQL
# Debe coincidir exactamente con la estructura de la tabla destino.
# 56 columnas = 55 del Excel + datFecha (agregada por el script)
# ─────────────────────────────────────────────────────────────

DB_COLUMNS = [
    'Campo4', 'Campo5', 'Campo6',
    'Campo186', 'Campo187', 'Campo188',
    'Campo10', 'Campo11', 'Campo12', 'Campo13', 'Campo14', 'Campo15',
    'Campo24', 'Campo26', 'Campo39', 'Campo45', 'Campo46', 'Campo65',
    'Campo50', 'Campo51', 'Campo52', 'Campo64',
    'Campo152', 'Campo153',
    'Campo111', 'Campo112', 'Campo113', 'Campo114', 'Campo115',
    'Campo116', 'Campo117',
    'Campo122', 'Campo123', 'Campo127', 'Campo128',
    'Campo133', 'Campo137', 'Campo139', 'Campo141', 'Campo143',
    'Campo144', 'Campo145', 'Campo146', 'Campo147', 'Campo148', 'Campo149',
    'Campo154', 'Campo158', 'Campo178', 'Campo179',
    'Campo182', 'Campo183', 'Campo184', 'Campo185',
    'DESCRIPCION', 'datFecha',
]

# Renombres de columnas del Excel → nombre en BD
# Ej: el Excel dice "descripcion_unificada" pero en la BD se llama "DESCRIPCION"
EXCEL_TO_DB = {'descripcion_unificada': 'DESCRIPCION'}


# ─────────────────────────────────────────────────────────────
# LOGGING (se configura en setup_logging() al iniciar main)
# No usar ruta relativa al cwd: en producción suele ejecutarse desde public/
# y el usuario del cron no tiene permiso de escritura ahí.
# Prioridad: IMPORTOC_LOG → mismo directorio que este script → /tmp
# ─────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)


def setup_logging():
    """Consola + archivo en una ruta escribible (evita PermissionError en producción)."""
    fmt = '%(asctime)s [%(levelname)s] %(message)s'
    handlers = [logging.StreamHandler(sys.stdout)]
    candidates = []
    env_path = os.environ.get('IMPORTOC_LOG')
    if env_path:
        candidates.append(env_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, 'importacion_log.txt'))
    candidates.append(os.path.join(tempfile.gettempdir(), 'importacion_log.txt'))

    log_path_used = None
    for path in candidates:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, mode=0o755, exist_ok=True)
            handlers.append(logging.FileHandler(path, encoding='utf-8'))
            log_path_used = path
            break
        except (PermissionError, OSError):
            continue

    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    if log_path_used:
        sys.stderr.write(f'[ImportOC] Log en archivo: {log_path_used}\n')
    else:
        sys.stderr.write('[ImportOC] Sin archivo de log (solo consola); defina IMPORTOC_LOG.\n')


def infer_fecha_from_filename(filepath):
    """
    Intenta obtener el primer día del mes desde el nombre del archivo
    (p. ej. 'Importaciones Marzo 2026 v2.xlsm' → 2026-03-01).
    """
    base = os.path.basename(filepath or '')
    low = base.lower()
    month_num = None
    for nombre, num in _MESES_ES:
        if nombre in low:
            month_num = num
            break
    if month_num is None:
        return None
    m = re.search(r'\b(20\d{2})\b', base)
    if not m:
        return None
    return date(int(m.group(1)), month_num, 1)


# ─────────────────────────────────────────────────────────────
# CLASES Y FUNCIONES DE VALIDACIÓN DE TIPOS
# ─────────────────────────────────────────────────────────────

class DataIssue:
    """
    Representa un problema de tipo encontrado en una celda del Excel.
    Se registra en el log al final para auditoría.
    """
    def __init__(self, row_num, col, raw_value, resolved_value, reason):
        self.row_num = row_num         # Número de fila en el Excel (con encabezado)
        self.col = col                 # Nombre de columna
        self.raw_value = raw_value     # Valor original del Excel
        self.resolved_value = resolved_value  # Valor que se insertará en BD
        self.reason = reason           # Explicación del ajuste

    def __str__(self):
        return (f"  Fila {self.row_num} | {self.col}: "
                f"'{self.raw_value}' → '{self.resolved_value}' ({self.reason})")


def coerce_bigint(value, col, row_num, issues):
    """
    Convierte un valor a entero para columnas BIGINT.

    Casos que maneja:
    - int          → retorna tal cual
    - float        → trunca a entero (ej: 1.2 → 1), registra en issues si pierde decimales
    - float NaN    → retorna None (NULL en BD)
    - str numérica → convierte
    - str con texto → extrae dígitos si puede, sino NULL
    - None / ''    → retorna None (NULL en BD)
    """
    if value is None or value == '':
        return None  # Celda vacía → NULL en BD

    if isinstance(value, int):
        return value  # Ya es entero, no hay ajuste

    if isinstance(value, float):
        if value != value:  # Truco para detectar NaN (NaN != NaN en Python)
            issues.append(DataIssue(row_num, col, value, None, "float NaN → NULL"))
            return None
        int_val = int(value)
        # Si el float no es exactamente un entero (ej: 1.2), reportar la pérdida
        if float(int_val) != value:
            issues.append(DataIssue(row_num, col, value, int_val,
                                    f"float {value} truncado a int {int_val}"))
        return int_val

    if isinstance(value, str):
        # Limpiar separadores de miles (ej: "1,000" → "1000")
        clean = value.strip().replace(',', '').replace('.', '')
        if clean == '':
            issues.append(DataIssue(row_num, col, value, None, "string vacío → NULL"))
            return None
        try:
            return int(clean)
        except ValueError:
            # Último recurso: extraer solo caracteres numéricos
            digits = re.sub(r'[^\d\-]', '', value.strip())
            if digits and digits != '-':
                try:
                    int_val = int(digits)
                    issues.append(DataIssue(row_num, col, value, int_val,
                                            f"no-numérico, se extrajeron dígitos: {int_val}"))
                    return int_val
                except ValueError:
                    pass
            issues.append(DataIssue(row_num, col, value, None,
                                    f"no convertible a bigint → NULL"))
            return None

    # Para cualquier otro tipo (bool, datetime, etc.)
    try:
        return int(value)
    except (TypeError, ValueError):
        issues.append(DataIssue(row_num, col, value, None,
                                f"tipo {type(value).__name__} no convertible → NULL"))
        return None


def coerce_double(value, col, row_num, issues):
    """
    Convierte un valor a float para columnas DOUBLE (Campo158).
    Acepta coma decimal (ej: "1,5" → 1.5).
    """
    if value is None or value == '':
        return None

    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN check
            issues.append(DataIssue(row_num, col, value, None, "NaN → NULL"))
            return None
        return float(value)

    if isinstance(value, str):
        clean = value.strip().replace(',', '.')  # Normalizar separador decimal
        if clean == '':
            return None
        try:
            return float(clean)
        except ValueError:
            issues.append(DataIssue(row_num, col, value, None,
                                    f"no convertible a double → NULL"))
            return None

    try:
        return float(value)
    except (TypeError, ValueError):
        issues.append(DataIssue(row_num, col, value, None,
                                f"tipo {type(value).__name__} no convertible → NULL"))
        return None


def coerce_text(value, col, row_num, issues):
    """
    Convierte un valor a string para columnas TEXT.
    - Elimina caracteres nulos (\x00) que MySQL no acepta
    - Trunca a 65535 chars si excede el límite de tipo TEXT
    - Retorna None para valores vacíos (NULL en BD)
    """
    if value is None:
        return None  # NULL en BD

    if isinstance(value, str):
        cleaned = value.replace('\x00', '')  # MySQL no acepta caracteres nulos
        if len(cleaned) > 65535:
            # El tipo TEXT de MySQL tiene límite de 65535 bytes
            issues.append(DataIssue(row_num, col, f"[{len(cleaned)} chars]",
                                    f"[truncado a 65535 chars]",
                                    "valor excede límite TEXT de MySQL"))
            cleaned = cleaned[:65535]
        return cleaned if cleaned != '' else None  # Vacíos van como NULL

    # Convertir otros tipos a string
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return str(value)
    return str(value)


def coerce_date(value, fecha_param, col, row_num, issues):
    """
    Para la columna datFecha siempre retorna el parámetro --fecha.
    Todos los registros del mismo archivo comparten la misma fecha de mes.
    """
    return fecha_param  # Ignora el valor del Excel, usa el parámetro


def coerce_row(excel_row_dict, db_col_order, fecha_param, row_num, issues):
    """
    Convierte una fila completa del Excel al formato esperado por la BD.

    Para cada columna en db_col_order:
    - Determina el tipo que corresponde (BIGINT, DOUBLE, TEXT, DATE)
    - Llama a la función de conversión correspondiente
    - Agrega a 'issues' cualquier ajuste necesario

    Retorna una lista de valores en el mismo orden que db_col_order.
    """
    result = []
    for col in db_col_order:
        # Obtener valor del Excel (None si la columna no existe en el archivo)
        raw = excel_row_dict.get(col)

        if col in BIGINT_COLS:
            result.append(coerce_bigint(raw, col, row_num, issues))
        elif col in DOUBLE_COLS:
            result.append(coerce_double(raw, col, row_num, issues))
        elif col in DATE_COLS:
            result.append(coerce_date(raw, fecha_param, col, row_num, issues))
        elif col in TEXT_COLS:
            result.append(coerce_text(raw, col, row_num, issues))
        else:
            # Columna sin tipo definido (no debería ocurrir si DB_COLUMNS está bien)
            issues.append(DataIssue(row_num, col, raw, None,
                                    "columna sin tipo definido → NULL"))
            result.append(None)
    return result


# ─────────────────────────────────────────────────────────────
# LECTURA DEL EXCEL
# Usa openpyxl en modo "read_only" para no cargar todo en RAM.
# Esto permite procesar archivos de cientos de miles de filas.
# ─────────────────────────────────────────────────────────────

def read_excel(filepath):
    """
    Generador que lee el Excel fila por fila (sin cargar todo en RAM).

    - La primera fila se trata como encabezado
    - Renombra columnas según EXCEL_TO_DB (ej: descripcion_unificada → DESCRIPCION)
    - Genera un dict {nombre_columna_bd: valor} por cada fila de datos
    """
    log.info(f"Abriendo archivo: {filepath}")
    # read_only=True: no carga todo en RAM
    # keep_vba=False: ignora macros VBA (el .xlsm las tiene)
    # data_only=True: lee valores calculados, no fórmulas
    wb = openpyxl.load_workbook(filepath, read_only=True, keep_vba=False, data_only=True)
    ws = wb.active  # Usa la primera hoja activa
    log.info(f"Hoja activa: '{ws.title}'")

    headers = None
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            # Primera fila → construir lista de nombres de columna BD
            headers = []
            for h in row:
                if h is None:
                    headers.append(None)  # Columna sin nombre → se ignora
                    continue
                h_str = str(h).strip()
                # Aplicar renombres (descripcion_unificada → DESCRIPCION)
                headers.append(EXCEL_TO_DB.get(h_str, h_str))
            log.info(f"Encabezados detectados ({len(headers)}): {headers}")
            continue  # Pasar a la siguiente fila (los datos)

        # Filas de datos: armar diccionario columna → valor
        row_dict = {}
        for i, val in enumerate(row):
            if i < len(headers) and headers[i] is not None:
                row_dict[headers[i]] = val
        yield row_dict  # Entregar la fila al loop principal

    wb.close()


# ─────────────────────────────────────────────────────────────
# CONEXIÓN Y OPERACIONES MySQL
# ─────────────────────────────────────────────────────────────

def get_connection():
    """Abre y retorna una conexión MySQL con autocommit desactivado."""
    log.info(f"Conectando a MySQL {MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']} "
             f"→ BD '{MYSQL_CONFIG['database']}'")
    # autocommit=False: controlamos cuándo hacer commit (mejor rendimiento en bulk insert)
    conn = mysql.connector.connect(**MYSQL_CONFIG, autocommit=False)
    log.info("Conexión establecida.")
    return conn


def create_table_if_not_exists(cursor, table, columns_def):
    """
    Crea la tabla si no existe en la BD.
    Si ya existe, no hace nada (no borra datos existentes).
    """
    cursor.execute(f"SHOW TABLES LIKE '{table}'")
    if cursor.fetchone():
        log.info(f"Tabla '{table}' ya existe.")
        return
    log.info(f"Creando tabla '{table}'...")
    cols_sql = ',\n  '.join(columns_def)
    ddl = (
        f"CREATE TABLE `{table}` (\n  {cols_sql}\n) "
        f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    cursor.execute(ddl)
    log.info(f"Tabla '{table}' creada.")


# ─────────────────────────────────────────────────────────────
# DDL (Definición de Estructura) de la tabla importaciones_YYYY_MM
# Este bloque define exactamente cómo se crea la tabla en MySQL.
# Si la estructura cambia, hay que actualizar aquí también.
# ─────────────────────────────────────────────────────────────

COLUMNS_DDL = [
    "`Campo4`    TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo5`    TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo6`    TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo186`  BIGINT NULL",
    "`Campo187`  BIGINT NULL",
    "`Campo188`  BIGINT NULL",
    "`Campo10`   BIGINT NULL",
    "`Campo11`   BIGINT NULL",
    "`Campo12`   BIGINT NULL",
    "`Campo13`   BIGINT NULL",
    "`Campo14`   BIGINT NULL",
    "`Campo15`   BIGINT NULL",
    "`Campo24`   BIGINT NULL",
    "`Campo26`   TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo39`   BIGINT NULL",
    "`Campo45`   BIGINT NULL",
    "`Campo46`   BIGINT NULL",
    "`Campo65`   BIGINT NULL",
    "`Campo50`   BIGINT NULL",
    "`Campo51`   BIGINT NULL",
    "`Campo52`   BIGINT NULL",
    "`Campo64`   TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo152`  BIGINT NULL",
    "`Campo153`  BIGINT NULL",
    "`Campo111`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo112`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo113`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo114`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo115`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo116`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo117`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo122`  BIGINT NULL",
    "`Campo123`  BIGINT NULL",
    "`Campo127`  BIGINT NULL",
    "`Campo128`  BIGINT NULL",
    "`Campo133`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo137`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo139`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo141`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo143`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo144`  BIGINT NULL",
    "`Campo145`  BIGINT NULL",
    "`Campo146`  BIGINT NULL",
    "`Campo147`  BIGINT NULL",
    "`Campo148`  BIGINT NULL",
    "`Campo149`  BIGINT NULL",   # NOTA: el Excel tiene decimales (1.2, 2.4) → se truncan
    "`Campo154`  BIGINT NULL",
    "`Campo158`  DOUBLE NULL",   # Único campo decimal
    "`Campo178`  BIGINT NULL",
    "`Campo179`  BIGINT NULL",
    "`Campo182`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo183`  TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`Campo184`  BIGINT NULL",
    "`Campo185`  BIGINT NULL",
    "`DESCRIPCION` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL",
    "`datFecha`  DATE NULL",     # Mismo valor en todas las filas: mes del archivo (o --fecha / --forzar-fecha)
]


def insert_batch(cursor, batch, columns, table):
    """
    Inserta una lista de tuplas (batch) en la tabla usando executemany.
    executemany es más eficiente que múltiples INSERT individuales.
    """
    placeholders = ', '.join(['%s'] * len(columns))        # (%s, %s, %s, ...)
    col_names = ', '.join(f'`{c}`' for c in columns)       # (`Col1`, `Col2`, ...)
    sql = f"INSERT INTO `{table}` ({col_names}) VALUES ({placeholders})"
    cursor.executemany(sql, batch)  # Envía todos los registros del batch en una sola llamada


# ─────────────────────────────────────────────────────────────
# FUNCIÓN PRINCIPAL
# ─────────────────────────────────────────────────────────────

def main():
    setup_logging()

    # ── Parsear argumentos de línea de comando ──────────────
    parser = argparse.ArgumentParser(description='Importar Excel de importaciones a MySQL')
    parser.add_argument('--fecha', default=None,
                        help='Fecha del mes (YYYY-MM-DD). Opcional si el nombre del archivo trae mes+año; '
                             'por defecto prevalece la deducción del archivo salvo --forzar-fecha.')
    parser.add_argument('--forzar-fecha', action='store_true',
                        help='Imponer --fecha aunque el nombre del archivo sugiera otro mes.')
    parser.add_argument('--archivo', default='Importaciones Enero 2026 v2.xlsm',
                        help='Ruta al archivo Excel (.xlsm/.xlsx)')
    parser.add_argument('--batch', type=int, default=3000,
                        help='Registros por batch de inserción (default: 3000)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Solo valida sin insertar en BD')
    args = parser.parse_args()

    # ── Validar y parsear la fecha ───────────────────────────
    # Prioridad: mes/año deducidos del nombre del archivo (evita errores si el PHP envía --fecha fija).
    # Excepción: --forzar-fecha + --fecha impone la fecha de línea de comandos.
    fecha_infer = infer_fecha_from_filename(args.archivo)
    fecha_cli = None
    if args.fecha:
        try:
            fecha_cli = datetime.strptime(args.fecha, '%Y-%m-%d').date()
        except ValueError:
            log.error(f"Formato de fecha inválido: '{args.fecha}'. Use YYYY-MM-DD.")
            sys.exit(1)

    if fecha_cli and args.forzar_fecha:
        fecha_param = fecha_cli
        if fecha_infer and (fecha_cli.year, fecha_cli.month) != (fecha_infer.year, fecha_infer.month):
            log.warning(
                "--forzar-fecha: se usa %s; el nombre del archivo sugería %04d-%02d-01."
                % (fecha_param, fecha_infer.year, fecha_infer.month)
            )
        else:
            log.info("Usando --fecha %s (--forzar-fecha)." % fecha_param)
    elif fecha_infer:
        fecha_param = fecha_infer
        if fecha_cli and (fecha_cli.year, fecha_cli.month) != (fecha_infer.year, fecha_infer.month):
            log.info(
                "Mes/año desde el nombre del archivo → %s. Se ignora --fecha %s "
                "(añada --forzar-fecha si realmente debe usarse la fecha de línea de comandos)."
                % (fecha_param, fecha_cli)
            )
        else:
            log.info("Fecha usada (día 1 del mes deducido del archivo): %s." % fecha_param)
    elif fecha_cli:
        fecha_param = fecha_cli
        log.info("Usando --fecha %s (el nombre del archivo no permitió deducir mes/año)." % fecha_param)
    else:
        log.error(
            "Indique --fecha YYYY-MM-DD o use un nombre de archivo con mes en español y año "
            "(ej. 'Importaciones Marzo 2026 v2.xlsm')."
        )
        sys.exit(1)

    # ── Nombre de tabla dinámico: importaciones_YYYY_MM ─────
    # Ej: fecha 2026-01-01 → tabla importaciones_2026_01
    table_name = f"importaciones_{fecha_param.strftime('%Y_%m')}"

    # ── Verificar que el archivo existe ─────────────────────
    if not os.path.exists(args.archivo):
        log.error(f"Archivo no encontrado: {args.archivo}")
        sys.exit(1)

    # ── Mostrar configuración de inicio ─────────────────────
    log.info("=" * 60)
    log.info(f"INICIO DE IMPORTACIÓN")
    log.info(f"  Archivo : {args.archivo}")
    log.info(f"  Fecha   : {fecha_param} (todo el mes será esta fecha)")
    log.info(f"  Tabla   : {table_name}")
    log.info(f"  Batch   : {args.batch}")
    log.info(f"  Dry-run : {args.dry_run}")
    log.info("=" * 60)

    # ── Contadores para el reporte final ────────────────────
    stats = {
        'filas_leidas':     0,   # Total de filas leídas del Excel
        'filas_insertadas': 0,   # Total de filas enviadas a MySQL
        'filas_con_issues': 0,   # Filas que tuvieron al menos un ajuste de tipo
        'total_issues':     0,   # Total de ajustes de tipo realizados
        'issues_por_col':   defaultdict(int),  # Cuántos ajustes por columna
        'batches_ok':       0,   # Batches exitosos
        'batches_error':    0,   # Batches que fallaron
        'errores_db':       [],  # Mensajes de error de BD
    }

    # Muestra de ajustes para el log (guardamos máximo 200 ejemplos)
    all_issues_sample = []
    MAX_ISSUE_SAMPLES = 200

    conn = None
    cursor = None

    # ── Conectar y preparar la BD (solo en modo producción) ─
    if not args.dry_run:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Crear la tabla si no existe (primera vez del mes)
            create_table_if_not_exists(cursor, table_name, COLUMNS_DDL)
            conn.commit()

            # ── OPTIMIZACIONES DE VELOCIDAD PARA BULK INSERT ──
            # DISABLE KEYS: no actualiza índices en cada INSERT, los reconstruye al final
            cursor.execute(f"ALTER TABLE `{table_name}` DISABLE KEYS")
            # bulk_insert_buffer_size: buffer de 256MB para inserciones masivas
            cursor.execute("SET SESSION bulk_insert_buffer_size = 268435456")
            # foreign_key_checks=0: no valida claves foráneas durante inserción
            cursor.execute("SET SESSION foreign_key_checks = 0")
            # unique_checks=0: no verifica unicidad durante inserción
            cursor.execute("SET SESSION unique_checks = 0")
            conn.commit()
        except mysql.connector.Error as e:
            log.error(f"Error conectando a MySQL: {e}")
            sys.exit(1)

    # Hacer commit cada N batches (reduce overhead de commits)
    COMMIT_EVERY = 10

    # ── LOOP PRINCIPAL DE LECTURA E INSERCIÓN ───────────────
    try:
        batch = []  # Buffer acumulador de filas antes de insertar
        for row_dict in read_excel(args.archivo):
            stats['filas_leidas'] += 1
            row_issues = []  # Issues de esta fila en particular

            # Convertir fila del Excel a los tipos esperados por MySQL
            coerced = coerce_row(row_dict, DB_COLUMNS, fecha_param,
                                 stats['filas_leidas'] + 1,  # +1 por la fila de encabezado
                                 row_issues)

            # Registrar si hubo ajustes en esta fila
            if row_issues:
                stats['filas_con_issues'] += 1
                stats['total_issues'] += len(row_issues)
                for issue in row_issues:
                    stats['issues_por_col'][issue.col] += 1
                if len(all_issues_sample) < MAX_ISSUE_SAMPLES:
                    all_issues_sample.extend(row_issues)

            # Agregar la fila convertida al batch acumulador
            batch.append(tuple(coerced))

            # Cuando el batch llega al tamaño configurado, insertar
            if len(batch) >= args.batch:
                if not args.dry_run:
                    try:
                        insert_batch(cursor, batch, DB_COLUMNS, table_name)
                        stats['batches_ok'] += 1
                        stats['filas_insertadas'] += len(batch)
                        # Commit periódico (no en cada batch para mayor velocidad)
                        if stats['batches_ok'] % COMMIT_EVERY == 0:
                            conn.commit()
                    except mysql.connector.Error as e:
                        # Si el batch completo falla, intentar fila por fila
                        conn.rollback()
                        stats['batches_error'] += 1
                        err_msg = f"Batch fila ~{stats['filas_leidas']}: {e}"
                        stats['errores_db'].append(err_msg)
                        log.error(err_msg)
                        rescatadas = 0
                        for single in batch:
                            try:
                                insert_batch(cursor, [single], DB_COLUMNS, table_name)
                                conn.commit()
                                stats['filas_insertadas'] += 1
                                rescatadas += 1
                            except mysql.connector.Error as e2:
                                conn.rollback()
                                stats['errores_db'].append(f"  Fila individual falló: {e2}")
                        log.warning(f"  Rescatadas {rescatadas}/{len(batch)} filas del batch fallido.")
                else:
                    # En dry-run, solo contamos sin insertar
                    stats['filas_insertadas'] += len(batch)
                    stats['batches_ok'] += 1

                batch.clear()  # Vaciar buffer para el siguiente batch

                # Reporte de progreso cada 30.000 filas
                if stats['filas_leidas'] % 30000 == 0:
                    log.info(f"  Progreso: {stats['filas_leidas']:,} filas procesadas "
                             f"({stats['filas_insertadas']:,} insertadas)...")

        # ── Insertar el último batch (puede ser menor al tamaño configurado) ──
        if batch:
            if not args.dry_run:
                try:
                    insert_batch(cursor, batch, DB_COLUMNS, table_name)
                    stats['batches_ok'] += 1
                    stats['filas_insertadas'] += len(batch)
                except mysql.connector.Error as e:
                    conn.rollback()
                    stats['batches_error'] += 1
                    err_msg = f"Último batch: {e}"
                    stats['errores_db'].append(err_msg)
                    log.error(err_msg)
                    rescatadas = 0
                    for single in batch:
                        try:
                            insert_batch(cursor, [single], DB_COLUMNS, table_name)
                            conn.commit()
                            stats['filas_insertadas'] += 1
                            rescatadas += 1
                        except mysql.connector.Error as e2:
                            conn.rollback()
                            stats['errores_db'].append(f"  Fila individual falló: {e2}")
                    log.warning(f"  Rescatadas {rescatadas}/{len(batch)} filas del último batch.")
            else:
                stats['filas_insertadas'] += len(batch)
                stats['batches_ok'] += 1

        # ── FINALIZACIÓN: commit final y re-habilitar optimizaciones ──
        if not args.dry_run and conn:
            conn.commit()
            # Re-habilitar índices (MySQL los reconstruye ahora, una sola vez)
            cursor.execute(f"ALTER TABLE `{table_name}` ENABLE KEYS")
            # Restaurar configuración de sesión
            cursor.execute("SET SESSION foreign_key_checks = 1")
            cursor.execute("SET SESSION unique_checks = 1")
            conn.commit()
            log.info("Índices re-habilitados y commit final aplicado.")

    except Exception as e:
        # Error inesperado → rollback para no dejar datos a medias
        log.error(f"Error inesperado: {e}", exc_info=True)
        if not args.dry_run and conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        # Siempre cerrar cursor y conexión, aunque haya error
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            log.info("Conexión MySQL cerrada.")

    # ─────────────────────────────────────────────────────────
    # REPORTE FINAL
    # ─────────────────────────────────────────────────────────
    sep = "=" * 60
    log.info(sep)
    log.info("RESUMEN DE IMPORTACIÓN")
    log.info(sep)
    log.info(f"  Archivo procesado  : {args.archivo}")
    log.info(f"  Fecha asignada     : {fecha_param}")
    log.info(f"  Tabla              : {table_name}")
    log.info(f"  Modo               : {'DRY-RUN (sin inserción)' if args.dry_run else 'PRODUCCIÓN'}")
    log.info(f"  Filas leídas       : {stats['filas_leidas']:,}")
    log.info(f"  Filas insertadas   : {stats['filas_insertadas']:,}")
    log.info(f"  Filas con ajustes  : {stats['filas_con_issues']:,}")
    log.info(f"  Total ajustes      : {stats['total_issues']:,}")
    log.info(f"  Batches exitosos   : {stats['batches_ok']:,}")
    log.info(f"  Batches con error  : {stats['batches_error']:,}")

    if stats['issues_por_col']:
        log.info("")
        log.info("  Ajustes por columna:")
        for col, cnt in sorted(stats['issues_por_col'].items(), key=lambda x: -x[1]):
            log.info(f"    {col:<20} : {cnt:,} ajuste(s)")

    if all_issues_sample:
        log.info("")
        log.info(f"  Muestra de ajustes (primeros {len(all_issues_sample)}):")
        for issue in all_issues_sample[:50]:
            log.info(str(issue))
        if len(all_issues_sample) > 50:
            log.info(f"  ... y {len(all_issues_sample) - 50} ajustes más en el log completo.")

    if stats['errores_db']:
        log.info("")
        log.info(f"  Errores de BD ({len(stats['errores_db'])}):")
        for err in stats['errores_db'][:20]:
            log.info(f"    {err}")

    log.info(sep)
    log.info("FIN")
    log.info(sep)

    return 0 if stats['batches_error'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
