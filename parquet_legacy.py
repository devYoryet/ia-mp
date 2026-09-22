#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transformación de Excel a .parquet (módulo legacy /legacy/parquet).

Toma la limpieza de xlsx-to-parquet.py y la vuelve utilizable desde el panel con
archivos de cualquier tamaño: lee fila por fila (openpyxl read_only / pyxlsb) y
escribe el parquet por lotes, así un .xlsx de 126 MB no se carga entero en
memoria (gestor_oc tiene 8 GB).

Qué hace antes de convertir, leyendo el CONTENIDO (no el nombre del archivo):
  * separa las hojas de datos de las tablas dinámicas (TD1, TD2, ROC_DINAMICO…):
    una hoja es "de datos" si la primera fila son encabezados; las TD traen filas
    en blanco y filtros arriba. Las TD se ignoran y se informa.
  * reconoce el tipo de reporte por sus columnas y aplica su validación:
      insulinas      (hoja Base + TD): avisa qué hojas se ignoran.
      distribucion   (una hoja por mes): el archivo debe venir DESDE ENERO del
                     año del último mes, porque al cliente se le envía el
                     acumulado del año. Se mira la fecha de entrega, no el
                     nombre del archivo ni el de las hojas.
      oc_farma       (.xlsb, hoja Datos + ROC_DINAMICO)
      generico       cualquier otro: se convierten todas las hojas de datos.

Uso:
    python parquet_legacy.py --revisar "<archivo>"      # solo diagnóstico
    python parquet_legacy.py "<archivo>"                # revisa y convierte
Tokens de fin para el panel: PARQUET LISTO / ERROR CRÍTICO
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre")
TEMP_DIR = Path(os.getenv("LEGACY_TEMP_DIR", "/host/storage/temp"))
LOTE = int(os.getenv("PARQUET_LOTE_FILAS", "50000"))
# Señales de que una hoja es una tabla dinámica y no datos.
PISTAS_TD = ("(todas)", "(varios elementos)", "valores", "total general", "etiquetas de fila",
             "suma de", "cuenta de")


class ErrorArchivo(Exception):
    """Algo que el usuario debe corregir en el Excel antes de volver a subirlo."""


def normalizar(nombre) -> str:
    n = unicodedata.normalize("NFKD", str(nombre)).encode("ASCII", "ignore").decode("ASCII")
    n = re.sub(r"\s+", "_", n).lower()
    return re.sub(r"[^a-z0-9_]", "", n)


# ------------------------------------------------------------------ lectura ---

def _filas_xlsx(path: str, hoja: str, limite: int | None = None):
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[hoja]
        for i, fila in enumerate(ws.iter_rows(values_only=True)):
            if limite is not None and i >= limite:
                return
            yield list(fila)
    finally:
        wb.close()


def _filas_xlsb(path: str, hoja: str, limite: int | None = None):
    from pyxlsb import open_workbook

    with open_workbook(path) as wb, wb.get_sheet(hoja) as ws:
        for i, fila in enumerate(ws.rows()):
            if limite is not None and i >= limite:
                return
            yield [c.v for c in fila]


def hojas(path: str) -> list[str]:
    if path.lower().endswith(".xlsb"):
        from pyxlsb import open_workbook

        with open_workbook(path) as wb:
            return list(wb.sheets)
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def filas(path: str, hoja: str, limite: int | None = None):
    lector = _filas_xlsb if path.lower().endswith(".xlsb") else _filas_xlsx
    return lector(path, hoja, limite)


# --------------------------------------------------------------- diagnóstico ---

def _vacia(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def revisar_hoja(path: str, hoja: str) -> dict:
    """Mira las primeras filas: ¿es hoja de datos (encabezados en la fila 1) o TD?"""
    muestra = [f for f in filas(path, hoja, 12)]
    if not muestra:
        return {"hoja": hoja, "tipo": "vacia", "columnas": [], "motivo": "la hoja está vacía"}
    cabecera = muestra[0]
    llenas = [v for v in cabecera if not _vacia(v)]
    texto = " ".join(str(v).lower() for f in muestra[:10] for v in f if not _vacia(v))
    # Una TD deja la primera fila en blanco y pone filtros/rótulos más abajo.
    if not llenas or len(llenas) < max(2, len(cabecera) * 0.3):
        motivo = "la primera fila no son encabezados"
        if any(p in texto for p in PISTAS_TD):
            return {"hoja": hoja, "tipo": "dinamica", "columnas": [], "motivo": "tabla dinámica"}
        return {"hoja": hoja, "tipo": "sin_encabezado", "columnas": [], "motivo": motivo}
    if any(p in " ".join(str(v).lower() for v in cabecera if not _vacia(v)) for p in PISTAS_TD):
        return {"hoja": hoja, "tipo": "dinamica", "columnas": [], "motivo": "tabla dinámica"}
    return {"hoja": hoja, "tipo": "datos", "columnas": [str(v).strip() if v is not None else "" for v in cabecera],
            "motivo": ""}


COLUMNAS_TIPO = {
    "insulinas": {"id", "esp_comprador", "pactivopht", "marca"},
    "distribucion": {"cliente_destinatario", "fecha_de_entrega", "nombre_producto_comercial"},
    "oc_farma": {"id", "esp_comprador", "razon_social_cliente", "cant_pht", "precio_pht"},
}


def detectar_tipo(hojas_datos: list[dict]) -> str:
    for hoja in hojas_datos:
        cols = {normalizar(c) for c in hoja["columnas"]}
        for tipo, requeridas in COLUMNAS_TIPO.items():
            if requeridas <= cols:
                return tipo
    return "generico"


def _mes_de_hoja(nombre: str) -> tuple[int, int] | None:
    """'Abr 2026' / 'Enero 2026' -> (2026, 4). Solo para informar."""
    n = normalizar(nombre)
    anio = re.search(r"20\d{2}", n)
    for i, m in enumerate(MESES, 1):
        if normalizar(m)[:3] in n:
            return (int(anio.group()), i) if anio else None
    return None


def meses_de_fechas(path: str, hoja: str, columna: str, limite_filas: int = 4000) -> set:
    """Meses (año, mes) presentes en una columna de fecha, mirando el contenido."""
    it = filas(path, hoja, limite_filas + 1)
    cabecera = [normalizar(c) for c in next(it, [])]
    if columna not in cabecera:
        return set()
    idx = cabecera.index(columna)
    encontrados = set()
    for fila in it:
        if idx >= len(fila):
            continue
        v = fila[idx]
        if isinstance(v, (datetime, date)):
            encontrados.add((v.year, v.month))
        elif isinstance(v, str):
            m = re.match(r"(20\d{2})-(\d{2})", v.strip())
            if m:
                encontrados.add((int(m.group(1)), int(m.group(2))))
        elif isinstance(v, (int, float)) and 20000 < float(v) < 80000:
            # serial de Excel (días desde 1899-12-30), típico de .xlsb
            d = date.fromordinal(date(1899, 12, 30).toordinal() + int(v))
            encontrados.add((d.year, d.month))
    return encontrados


def revisar(path: str, log=print) -> dict:
    """Diagnóstico completo: hojas, tipo, avisos y errores que el usuario debe corregir."""
    nombre = os.path.basename(path)
    log(f"Archivo: {nombre} ({os.path.getsize(path) / 1_048_576:.1f} MB)")
    todas = [revisar_hoja(path, h) for h in hojas(path)]
    datos = [h for h in todas if h["tipo"] == "datos"]
    ignoradas = [h for h in todas if h["tipo"] != "datos"]
    for h in todas:
        if h["tipo"] == "datos":
            log(f"   hoja '{h['hoja']}': datos, {len(h['columnas'])} columnas")
        else:
            log(f"   hoja '{h['hoja']}': se ignora ({h['motivo']})")
    if not datos:
        raise ErrorArchivo(
            "ninguna hoja tiene los encabezados en la primera fila. Deje los títulos en la fila 1, "
            "sin filas ni columnas en blanco antes, y vuelva a subir el archivo.")
    tipo = detectar_tipo(datos)
    log(f"Tipo de reporte detectado por sus columnas: {tipo}")
    avisos, errores = [], []

    if ignoradas:
        avisos.append("Se ignoran las hojas de tabla dinámica: " + ", ".join(f"'{h['hoja']}'" for h in ignoradas) +
                      ". El parquet se arma solo con las hojas de datos.")
    if tipo == "insulinas":
        avisos.append("Reporte de insulinas: se usa la hoja de datos y se descarta la tabla dinámica que trae el "
                      "archivo. Los títulos quedan normalizados, sin espacios ni tildes.")
    if tipo == "distribucion":
        hoja = datos[0]["hoja"]
        meses = set()
        for h in datos:
            meses |= meses_de_fechas(path, h["hoja"], "fecha_de_entrega")
        if not meses:
            errores.append(f"no se pudo leer la columna 'Fecha de entrega' en la hoja '{hoja}': "
                           "sin ella no se puede comprobar desde qué mes viene el archivo.")
        else:
            anio = max(a for a, _ in meses)
            del_anio = sorted(m for a, m in meses if a == anio)
            log("   meses con datos (según la fecha de entrega): " +
                ", ".join(f"{MESES[m - 1]} {a}" for a, m in sorted(meses)))
            faltan = [m for m in range(1, max(del_anio) + 1) if m not in del_anio]
            if faltan:
                errores.append(
                    f"la distribución se envía al cliente acumulada desde enero, y este archivo parte en "
                    f"{MESES[min(del_anio) - 1]} {anio}. Falta(n) {', '.join(MESES[m - 1] for m in faltan)} {anio}. "
                    f"Vuelva a exportar desde enero {anio} y suba el archivo completo.")
    for a in avisos:
        log(f"   AVISO: {a}")
    for e in errores:
        log(f"   ERROR: {e}")
    return {"tipo": tipo, "hojas_datos": datos, "hojas_ignoradas": ignoradas, "avisos": avisos, "errores": errores}


# --------------------------------------------------------------- conversión ---

def nombres_unicos(columnas) -> list[str]:
    """Normaliza y desambigua: la distribución de Cenabast trae dos columnas
    'Cantidad por envase', y con nombres repetidos df[col] devuelve un DataFrame
    y la limpieza falla."""
    salida, vistos = [], {}
    for i, c in enumerate(columnas, 1):
        n = normalizar(c) or f"columna_{i}"
        vistos[n] = vistos.get(n, 0) + 1
        salida.append(n if vistos[n] == 1 else f"{n}_{vistos[n]}")
    return salida


def _limpiar(df):
    """Misma limpieza de xlsx-to-parquet.py, aplicada por lote."""
    import pandas as pd

    df.columns = nombres_unicos(df.columns)
    fuera = [c for c in df.columns if "unnamed" in c or not c] + [c for c in df.columns if df[c].isna().all()]
    df = df.drop(columns=list(dict.fromkeys(fuera)), errors="ignore")
    sin_info = {"sininformacion", "sin informacion", "sin_informacion", "sin dato", "sindato",
                "sin_dato", "n/a", "na", "nulo", "null", "vacio", "vacío", "nan", ""}

    def numero(v):
        if v is None or (isinstance(v, float) and v != v):
            return 0.0
        if isinstance(v, str):
            t = v.strip().lower()
            if t in sin_info:
                return 0.0
            t = t.replace("$", "").replace(" ", "").replace(".", "").replace(",", ".")
            try:
                return float(t)
            except ValueError:
                return 0.0
        return v

    for col in df.columns:
        if col.startswith(("cant", "precio", "valor", "total", "monto")):
            df[col] = pd.to_numeric(df[col].map(numero), errors="coerce").fillna(0.0)
        elif "fecha" in col:
            d = pd.to_datetime(df[col], errors="coerce", format="mixed")
            df[col] = d.dt.strftime("%Y-%m-%d").where(d.notna(), None)
        elif col.startswith("documento"):
            s = df[col].astype("string").str.strip().replace(r"(?i)^sin informacion$", pd.NA, regex=True)
            df[col] = pd.to_numeric(s, errors="coerce").fillna(1).astype("Int64")
    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].astype("string").str.strip()
    return df


def convertir(path: str, diagnostico: dict, log=print) -> Path:
    """Convierte las hojas de datos a UN parquet, leyendo y escribiendo por lotes."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    destino = TEMP_DIR / (Path(path).stem + ".parquet")
    escritor = None
    esquema = None
    total = 0
    try:
        for hoja in diagnostico["hojas_datos"]:
            it = filas(path, hoja["hoja"])
            cabecera = [str(c).strip() if c is not None else "" for c in next(it, [])]
            lote, n_hoja = [], 0
            for fila in it:
                if all(_vacia(v) for v in fila):
                    continue
                lote.append((list(fila) + [None] * len(cabecera))[:len(cabecera)])
                if len(lote) >= LOTE:
                    escritor, esquema, n = _escribir(lote, cabecera, destino, escritor, esquema, pd, pa, pq)
                    total += n
                    n_hoja += n
                    lote = []
                    log(f"   hoja '{hoja['hoja']}': {n_hoja:,} filas")
            if lote:
                escritor, esquema, n = _escribir(lote, cabecera, destino, escritor, esquema, pd, pa, pq)
                total += n
                n_hoja += n
            log(f"   hoja '{hoja['hoja']}': {n_hoja:,} filas convertidas")
    finally:
        if escritor:
            escritor.close()
    if not total:
        raise ErrorArchivo("el archivo no tiene filas de datos bajo los encabezados.")
    log(f"Parquet escrito: {destino.name} · {total:,} filas · {destino.stat().st_size / 1_048_576:.1f} MB")
    return destino


def _escribir(lote, cabecera, destino, escritor, esquema, pd, pa, pq):
    df = _limpiar(pd.DataFrame(lote, columns=cabecera))
    tabla = pa.Table.from_pandas(df, preserve_index=False)
    if escritor is None:
        esquema = tabla.schema
        escritor = pq.ParquetWriter(destino, esquema, compression="snappy")
    elif tabla.schema != esquema:
        # Lotes distintos pueden inferir tipos distintos (una columna con solo
        # nulos, por ejemplo): se fuerza el esquema del primer lote.
        tabla = tabla.cast(esquema, safe=False)
    escritor.write_table(tabla)
    return escritor, esquema, len(df)


def main() -> int:
    ap = argparse.ArgumentParser(description="Excel -> parquet con revisión previa")
    ap.add_argument("archivo")
    ap.add_argument("--revisar", action="store_true", help="solo diagnóstico, no convierte")
    a = ap.parse_args()
    if not os.path.exists(a.archivo):
        print(f"ERROR CRÍTICO: no existe el archivo {a.archivo}")
        return 1
    try:
        diag = revisar(a.archivo, print)
        if diag["errores"]:
            print("ERROR CRÍTICO: el archivo no se puede transformar todavía:")
            for e in diag["errores"]:
                print(f"   - {e}")
            return 1
        if a.revisar:
            print("Revisión terminada: el archivo está en condiciones de transformarse. PARQUET LISTO (solo revisión)")
            return 0
        destino = convertir(a.archivo, diag, print)
        print(f"PARQUET LISTO: {destino.name}")
        return 0
    except ErrorArchivo as exc:
        print(f"ERROR CRÍTICO: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR CRÍTICO: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
