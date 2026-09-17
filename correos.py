"""Correos automáticos de gestión (Gmail SMTP).

Dos avisos de negocio que antes se mandaban a mano:
  * Item Detalle: cuando auto_item_detalle.py deja el mes cargado en clásico y prime.
  * Cierre Adjudicadas: cuando cierre_adjudicadas_completo.py cierra el mes sin fallas.

Cada aviso sale UNA vez por periodo: se marca con un archivo en LEGACY_TEMP_DIR
(.correo_<tipo>_<YYYYMM>.enviado). Los reintentos del cron o el repaso del día
15 no lo repiten.

Todo asunto lleva el prefijo CORREO_PREFIJO ("[Automático] ") para distinguir
estos avisos de los correos escritos a mano.

Configuración (.env): ALERT_EMAIL_SENDER, ALERT_EMAIL_PASSWORD (clave de
aplicación de Gmail) y, si se quiere cambiar, CORREO_ITEM_DETALLE_DESTINOS /
CORREO_CIERRE_ADJ_DESTINOS ("Nombre <email>, Nombre <email>"). Sin clave no se
envía nada y queda en el log.
"""

from __future__ import annotations

import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses
from pathlib import Path

TEMP_DIR = Path(os.getenv("LEGACY_TEMP_DIR", "/host/storage/temp"))
PREFIJO = os.getenv("CORREO_PREFIJO", "[Automático] ")
REMITENTE_POR_DEFECTO = "y.danoun@pharmatender.cl"
MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre")

DESTINOS_ITEM_DETALLE = (
    "Mauricio Moraga <m.moraga@pharmatender.cl>, "
    "Marcial Saavedra <m.saavedra@pharmatender.cl>, "
    "Benjamin Saavedra <b.saavedra@pharmatender.cl>, "
    "Guillermo Mellado <g.mellado@pharmatender.cl>"
)
DESTINOS_CIERRE_ADJ = (
    "Mauricio Moraga <m.moraga@pharmatender.cl>, "
    "Marcial Saavedra <m.saavedra@pharmatender.cl>, "
    "Soporte TI Pharmatender <soporte.ti@pharmatender.cl>, "
    "Benjamin Saavedra <b.saavedra@pharmatender.cl>, "
    "Guillermo Mellado <g.mellado@pharmatender.cl>, "
    "Hernan Opazo <h.opazo@pharmatender.cl>"
)


def nombre_mes(anio: int, mes: int) -> str:
    return f"{MESES[mes - 1]} {anio}"


def mes_siguiente(anio: int, mes: int) -> tuple[int, int]:
    return (anio + 1, 1) if mes == 12 else (anio, mes + 1)


def enviar(asunto: str, texto: str, destinos: str, log=print) -> bool:
    remitente = os.getenv("ALERT_EMAIL_SENDER", REMITENTE_POR_DEFECTO)
    asunto = PREFIJO + asunto
    clave = os.getenv("ALERT_EMAIL_PASSWORD", "").replace(" ", "")
    if not clave:
        log(f"Correo '{asunto}' NO enviado: falta ALERT_EMAIL_PASSWORD en el .env")
        return False
    direcciones = [e for _, e in getaddresses([destinos]) if e]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinos
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    html = "".join(f"<p>{linea}</p>" if linea else "" for linea in texto.split("\n\n"))
    msg.attach(MIMEText(f"<div style='font-family:Arial,sans-serif;font-size:14px'>{html}</div>", "html", "utf-8"))
    for intento in range(1, 4):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
                s.login(remitente, clave)
                s.sendmail(remitente, direcciones, msg.as_string())
            log(f"Correo '{asunto}' enviado a {', '.join(direcciones)}")
            return True
        except smtplib.SMTPAuthenticationError as exc:
            log(f"Correo '{asunto}' NO enviado: Gmail rechazó la clave de {remitente} ({exc.smtp_code})")
            return False
        except Exception as exc:  # noqa: BLE001
            log(f"Correo '{asunto}': intento {intento} falló ({exc})")
            time.sleep(10)
    return False


def _una_vez(tipo: str, anio: int, mes: int, asunto: str, texto: str, destinos: str, log) -> bool:
    marca = TEMP_DIR / f".correo_{tipo}_{anio}{mes:02d}.enviado"
    if marca.exists():
        log(f"Correo '{asunto}' ya se había enviado ({marca.read_text().strip()}); no se repite.")
        return False
    if enviar(asunto, texto, destinos, log):
        marca.write_text(f"enviado {datetime.now().isoformat(timespec='seconds')}")
        return True
    return False


def aviso_item_detalle(anio: int, mes: int, log=print) -> bool:
    """`anio`/`mes` = periodo cargado. El asunto lleva el mes en que se informa
    (el siguiente) y el cuerpo el periodo cargado: 'Item - Detalle Agosto 2026'
    avisa que está subido 'hasta Julio 2026'."""
    a2, m2 = mes_siguiente(anio, mes)
    asunto = f"Item - Detalle {nombre_mes(a2, m2)} - Clásico - Prime"
    texto = (
        "Estimados,\n\n"
        "Ya se encuentra subida la información en ambas plataformas correspondiente a item y detalle "
        f"hasta {nombre_mes(anio, mes)}.\n\n"
        "Saludos,"
    )
    return _una_vez("item_detalle", anio, mes, asunto, texto,
                    os.getenv("CORREO_ITEM_DETALLE_DESTINOS", DESTINOS_ITEM_DETALLE), log)


def aviso_cierre_adjudicadas(anio: int, mes: int, log=print) -> bool:
    nombre = nombre_mes(anio, mes)
    asunto = f"Cierre Adjudicadas - Reporte Gerencial, Cruce Lic. Adjudicadas - Gestor {nombre}"
    texto = (
        "Estimados,\n\n"
        f"Ya se encuentra efectuado el cierre de mes de Licitaciones Adjudicadas correspondiente {nombre}.\n\n"
        "Adicionalmente y junto al cierre también se encuentra disponible el archivo Gerencial que utiliza Mauricio.\n\n"
        "Saludos,"
    )
    return _una_vez("cierre_adjudicadas", anio, mes, asunto, texto,
                    os.getenv("CORREO_CIERRE_ADJ_DESTINOS", DESTINOS_CIERRE_ADJ), log)
