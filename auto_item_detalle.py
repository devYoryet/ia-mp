#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Descarga + carga AUTOMATICA de Item Detalle (zip mensual oc-da de transparencia).

Reemplaza el flujo manual (bajar el .zip a mano -> descomprimir -> subir el CSV
por https://iabot.pharmatender.cl/legacy/item-detalle). Pensado para correr por
cron los primeros dias de cada mes: importa el MES ANTERIOR (cuando su data ya
quedo cerrada en el portal). Es IDEMPOTENTE: si el periodo ya quedo importado,
no vuelve a hacer nada, asi que el cron puede dispararlo varias veces sin riesgo.

Pasos de una corrida:
  1. Resuelve el periodo objetivo (default: mes anterior; o --periodo YYYY-M).
  2. Si existe el marcador .item_detalle_<YYYYMM>.done  ->  termina (ya esta).
  3. HEAD al blob: el archivo existe (200) y su tamano es plausible.
  4. Descarga el .zip por streaming y verifica tamano == Content-Length (con
     reintentos dentro de la misma corrida).
  5. Descomprime el unico CSV interno.
  6. VALIDA que el dato sirve: no viene vacio/truncado y las fechas (FechaEnvio)
     caen mayoritariamente dentro del periodo descargado.
  7. DROP de la tabla YYYYMM (clasico + prime) para partir limpio en reintentos
     (evita duplicar filas si una corrida previa quedo a medias).
  8. Corre bin/ScriptCSV.py --tabla YYYYMM --server clasico (espeja a prime).
  9. Si el primario quedo OK -> escribe el marcador .done (no se reintenta mas).

Todo se escribe al MISMO log que ve el panel (import_oc_csv.log) para que el
flujo automatico sea visible en la vista /legacy/item-detalle, con los mismos
tokens finalizadores que ya entiende el front.

Uso:
    python auto_item_detalle.py                 # mes anterior
    python auto_item_detalle.py --periodo 2026-5
    python auto_item_detalle.py --periodo 2026-5 --force   # ignora marcador
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# csv: campos largos (Descripcion) pueden exceder el limite por defecto.
csv.field_size_limit(10_000_000)

BASE_URL = "https://transparenciachc.blob.core.windows.net/oc-da"
# Misma carpeta que usan el panel y los scripts legacy (montada en el container).
TEMP_DIR = Path(os.getenv("LEGACY_TEMP_DIR", "/host/storage/temp"))
LOG_NAME = "import_oc_csv.log"  # el mismo log que polea el panel de item-detalle
BIN_DIR = Path(__file__).resolve().parent / "bin"
SCRIPT_CSV = BIN_DIR / "ScriptCSV.py"

# Umbrales de validacion (configurables por env por si cambia el volumen).
MIN_BYTES_CSV = int(os.getenv("ITEM_DETALLE_MIN_BYTES", str(10 * 1024 * 1024)))  # 10 MB
MIN_FILAS_MUESTRA = int(os.getenv("ITEM_DETALLE_MIN_MUESTRA", "3000"))
MUESTRA_FILAS = int(os.getenv("ITEM_DETALLE_MUESTRA", "20000"))
FRAC_MES_MIN = float(os.getenv("ITEM_DETALLE_FRAC_MES", "0.85"))
DESCARGA_REINTENTOS = int(os.getenv("ITEM_DETALLE_REINTENTOS", "3"))

# Tokens que el front (legacy.py) reconoce como "proceso terminado".
TOK_OK = "TERMINADA CON EXITO"
TOK_ERR = "ERROR CRITICO"
TOK_FIN = "FIN"

ESPEJO_DE = {"clasico": "prime", "prime": "clasico"}

_log_fh = None


class Reintentable(Exception):
    """Fallo transitorio: tiene sentido reintentar en la proxima corrida del cron."""


# ----------------------------------------------------------------- logging ---

def _abrir_log() -> None:
    global _log_fh
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        _log_fh = open(TEMP_DIR / LOG_NAME, "w", buffering=1)
    except OSError as exc:
        print(f"[WARN] No se pudo abrir el log {LOG_NAME}: {exc}")
        _log_fh = None


def _cerrar_log() -> None:
    global _log_fh
    if _log_fh:
        try:
            _log_fh.close()
        except OSError:
            pass
        _log_fh = None


def log(msg: str) -> None:
    linea = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(linea, flush=True)
    if _log_fh:
        try:
            _log_fh.write(linea + "\n")
            _log_fh.flush()
        except OSError:
            pass


# ----------------------------------------------------------------- periodo ---

def periodo_anterior(hoy: date | None = None) -> tuple[int, int]:
    hoy = hoy or date.today()
    if hoy.month == 1:
        return hoy.year - 1, 12
    return hoy.year, hoy.month - 1


def parse_periodo(txt: str) -> tuple[int, int]:
    partes = txt.replace("/", "-").split("-")
    if len(partes) != 2:
        raise ValueError(f"--periodo invalido: {txt!r} (esperado YYYY-M)")
    y, m = int(partes[0]), int(partes[1])
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        raise ValueError(f"--periodo fuera de rango: {txt!r}")
    return y, m


# ----------------------------------------------------------------- guardas ---

def proceso_manual_vivo() -> bool:
    """True si hay una subida manual de item-detalle en curso (no colisionar).

    OJO: el panel reutiliza ESTE mismo .pid cuando dispara la corrida automatica
    desde el boton "Cargar ahora" (lo escribe justo despues del Popen). Si no
    ignoraramos nuestro propio PID (y el del proceso que nos lanzo), la corrida
    se auto-bloquearia y terminaria en 3 segundos sin importar nada.
    """
    pf = TEMP_DIR / ".item-detalle.pid"
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid in (os.getpid(), os.getppid()):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- descarga ---

def head_verificar(url: str) -> int:
    """HEAD: confirma 200 y devuelve el Content-Length. Lanza Reintentable si falla."""
    req = urlrequest.Request(url, method="HEAD")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            size = int(resp.headers.get("Content-Length", "0"))
            lastmod = resp.headers.get("Last-Modified", "?")
    except HTTPError as exc:
        if exc.code == 404:
            raise Reintentable(
                f"el archivo del periodo aun no existe en el portal (HTTP 404): {url}"
            ) from exc
        raise Reintentable(f"HEAD HTTP {exc.code} en {url}") from exc
    except (URLError, OSError) as exc:
        raise Reintentable(f"HEAD fallo ({exc}) en {url}") from exc

    if size < MIN_BYTES_CSV:
        raise Reintentable(f"Content-Length sospechosamente chico ({size} bytes) en {url}")
    log(f"HEAD OK · {size/1_048_576:.1f} MB · Last-Modified: {lastmod}")
    return size


def descargar(url: str, destino: Path, size_esperado: int) -> None:
    """Descarga por streaming y verifica el tamano final. Reintenta DESCARGA_REINTENTOS."""
    ultimo = None
    for intento in range(1, DESCARGA_REINTENTOS + 1):
        try:
            log(f"Descargando (intento {intento}/{DESCARGA_REINTENTOS})...")
            t0 = time.monotonic()
            bajado = 0
            with urlrequest.urlopen(url, timeout=120) as resp, open(destino, "wb") as fh:
                while True:
                    bloque = resp.read(1024 * 1024)
                    if not bloque:
                        break
                    fh.write(bloque)
                    bajado += len(bloque)
            real = destino.stat().st_size
            if real != size_esperado:
                raise Reintentable(
                    f"tamano descargado ({real}) != esperado ({size_esperado})"
                )
            log(f"Descarga OK · {real/1_048_576:.1f} MB en {time.monotonic()-t0:.1f}s")
            return
        except (URLError, OSError, Reintentable) as exc:
            ultimo = exc
            log(f"[WARN] descarga fallida: {exc}")
            time.sleep(min(5 * intento, 30))
    raise Reintentable(f"descarga fallida tras {DESCARGA_REINTENTOS} intentos: {ultimo}")


def descomprimir(zip_path: Path) -> Path:
    """Extrae el unico CSV del zip y devuelve su ruta."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            miembros = [m for m in z.namelist() if m.lower().endswith(".csv")]
            if not miembros:
                raise Reintentable(f"el zip no contiene ningun .csv (contenido: {z.namelist()})")
            nombre = miembros[0]
            info = z.getinfo(nombre)
            if info.file_size < MIN_BYTES_CSV:
                raise Reintentable(
                    f"el CSV interno viene casi vacio ({info.file_size} bytes)"
                )
            log(f"Descomprimiendo {nombre} ({info.file_size/1_048_576:.1f} MB)...")
            # Borra un CSV viejo del mismo nombre antes de extraer: si quedo de
            # una corrida previa con otro dueno, z.extract daria Permission
            # denied. El dir es group-writable, asi que el unlink procede.
            try:
                (TEMP_DIR / nombre).unlink(missing_ok=True)
            except OSError:
                pass
            z.extract(nombre, TEMP_DIR)
    except zipfile.BadZipFile as exc:
        raise Reintentable(f"zip corrupto: {exc}") from exc
    destino = TEMP_DIR / nombre
    log(f"CSV listo: {destino}")
    return destino


def validar(csv_path: Path, year: int, month: int) -> None:
    """Verifica que el CSV no venga vacio y que FechaEnvio caiga en el periodo."""
    prefijo = f"{year}-{month:02d}"
    with open(csv_path, encoding="latin-1", newline="") as f:
        r = csv.reader(f, delimiter=";")
        try:
            header = next(r)
        except StopIteration:
            raise Reintentable("el CSV no tiene ni cabecera")
        try:
            idx = header.index("FechaEnvio")
        except ValueError:
            raise Reintentable(f"no se encontro la columna FechaEnvio en {header[:5]}...")

        total = en_mes = 0
        for fila in r:
            total += 1
            if idx < len(fila) and fila[idx][:7] == prefijo:
                en_mes += 1
            if total >= MUESTRA_FILAS:
                break

    if total < MIN_FILAS_MUESTRA:
        raise Reintentable(f"muy pocas filas en el CSV ({total}); parece incompleto")
    frac = en_mes / total if total else 0.0
    log(f"Validacion · muestra={total} filas · {en_mes} con FechaEnvio en {prefijo} ({frac:.1%})")
    if frac < FRAC_MES_MIN:
        raise Reintentable(
            f"el contenido NO corresponde al periodo {prefijo}: solo {frac:.1%} de las "
            f"fechas caen en el mes (minimo {FRAC_MES_MIN:.0%})"
        )
    log("Validacion OK: el dato corresponde al periodo.")


# ------------------------------------------------------------------- carga ---

def _vault():
    sys.path.insert(0, str(BIN_DIR))
    from vault_linux_helper import VaultLinuxManager  # noqa: E402

    return VaultLinuxManager()


def drop_tabla(yyyymm: str, server: str) -> None:
    """DROP TABLE IF EXISTS en el primario y su espejo (idempotencia en reintentos)."""
    vault = _vault()
    servidores = [server]
    espejo = ESPEJO_DE.get(server)
    if espejo:
        servidores.append(espejo)
    for srv in servidores:
        try:
            conn = vault.get_linux_mysql_connection(
                database="oc_items_segmentado", force_local=False, server=srv
            )
            cur = conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS `{yyyymm}`")
            conn.commit()
            cur.close()
            conn.close()
            log(f"DROP TABLE `{yyyymm}` en {srv} (limpieza previa) OK")
        except Exception as exc:  # noqa: BLE001
            # No es critico: si la tabla no existia, ScriptCSV la crea igual.
            log(f"[WARN] no se pudo dropear `{yyyymm}` en {srv}: {exc}")


def importar(csv_path: Path, yyyymm: str, server: str) -> None:
    """Corre bin/ScriptCSV.py y tee su salida al log. Lanza si el primario no quedo OK."""
    args = [
        sys.executable, "-u", str(SCRIPT_CSV),
        "--excel", str(csv_path),
        "--tabla", yyyymm,
        "--server", server,
    ]
    log(f"Lanzando ScriptCSV.py --tabla {yyyymm} --server {server} ...")
    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(BIN_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        text=True,
        bufsize=1,
    )
    primario_ok = False
    token_ok = f"=== {server.upper()}: OK ==="
    for linea in proc.stdout:  # type: ignore[union-attr]
        linea = linea.rstrip("\n")
        if linea:
            log(linea)
        if token_ok in linea:
            primario_ok = True
    proc.wait()
    if not primario_ok:
        raise RuntimeError(
            f"ScriptCSV no reporto exito en el primario {server} (rc={proc.returncode})"
        )


# -------------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser(description="Descarga+carga automatica de Item Detalle")
    ap.add_argument("--periodo", help="YYYY-M (default: mes anterior)")
    ap.add_argument("--server", default="clasico", choices=["clasico", "prime"])
    ap.add_argument("--force", action="store_true", help="ignora el marcador .done")
    args = ap.parse_args()

    if args.periodo:
        year, month = parse_periodo(args.periodo)
    else:
        year, month = periodo_anterior()
    yyyymm = f"{year}{month:02d}"

    _abrir_log()
    log(f"=== AUTO Item Detalle · periodo {year}-{month:02d} (tabla {yyyymm}) ===")

    marcador = TEMP_DIR / f".item_detalle_{yyyymm}.done"
    if marcador.exists() and not args.force:
        log(f"Periodo {yyyymm} YA importado ({marcador.read_text().strip()}). Nada que hacer.")
        log(TOK_FIN)
        _cerrar_log()
        return 0

    if proceso_manual_vivo():
        log("Hay una subida MANUAL de item-detalle en curso; se omite la corrida automatica.")
        log(TOK_FIN)
        _cerrar_log()
        return 0

    url = f"{BASE_URL}/{year}-{month}.zip"  # OJO: mes SIN cero a la izquierda
    zip_path = TEMP_DIR / f"oc_{year}-{month}.zip"
    csv_path: Path | None = None
    rc = 0
    try:
        log(f"Origen: {url}")
        size = head_verificar(url)
        descargar(url, zip_path, size)
        csv_path = descomprimir(zip_path)
        validar(csv_path, year, month)
        drop_tabla(yyyymm, args.server)
        importar(csv_path, yyyymm, args.server)
        marcador.write_text(
            f"importado {datetime.now().isoformat(timespec='seconds')} via auto_item_detalle"
        )
        log(f"OK · periodo {yyyymm} importado y marcado como hecho. {TOK_OK}.")
    except Reintentable as exc:
        log(f"{TOK_ERR}: {exc}")
        log("(transitorio) se reintentara en la proxima corrida del cron.")
        rc = 2
    except Exception as exc:  # noqa: BLE001
        log(f"{TOK_ERR}: {exc}")
        rc = 1
    finally:
        for p in (zip_path, csv_path):
            try:
                if p and p.exists():
                    p.unlink()
            except OSError:
                pass
        _cerrar_log()
    return rc


if __name__ == "__main__":
    sys.exit(main())
