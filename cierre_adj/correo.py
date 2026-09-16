"""Correo de resumen del cierre (mismo canal que usaba el cierre de Windows:
Gmail SMTP con licitaciones@pharmatender.cl). Si falta ALERT_EMAIL_PASSWORD
no se envía y solo se informa en el log."""

from __future__ import annotations

import html
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

REMITENTE = os.getenv("ALERT_EMAIL_SENDER", "licitaciones@pharmatender.cl")
DESTINOS = [x.strip() for x in os.getenv(
    "ALERT_EMAIL_RECIPIENTS", "soporte.ti@pharmatender.cl,y.danoun@pharmatender.cl").split(",") if x.strip()]
COLOR = {"ok": "#1e7e34", "aviso": "#b8860b", "falla": "#c82333", "omitida": "#777"}


def cuerpo_html(mes: str, estado: str, meses: dict, acciones: list, lineas_log: list[str]) -> str:
    filas = ""
    for m, resultados in meses.items():
        celdas = "".join(
            f"<td style='padding:3px 8px;color:{COLOR.get(r.estado, '#777')}' title='{html.escape(r.resumen)}'>"
            f"{r.id} {r.estado}</td>" for r in resultados)
        filas += f"<tr><td style='padding:3px 8px'><b>{m}</b></td>{celdas}</tr>"
    acc = "".join(f"<li>{html.escape(str(a))}</li>" for a in acciones) or "<li>sin cambios</li>"
    detalle = "".join(
        f"<li><b>{m} {r.id}</b> {html.escape(r.titulo)}: {html.escape(r.resumen)}</li>"
        for m, resultados in meses.items() for r in resultados if r.estado in ("falla", "aviso"))
    return (
        f"<div style='font-family:Arial,sans-serif'>"
        f"<h2 style='color:{COLOR.get(estado, '#555')}'>Cierre Adjudicadas {mes}: {estado.upper()}</h2>"
        f"<table border='1' style='border-collapse:collapse;font-size:12px'>{filas}</table>"
        f"<h3>Fallas y avisos</h3><ul>{detalle or '<li>ninguno</li>'}</ul>"
        f"<h3>Acciones</h3><ul style='font-size:12px'>{acc}</ul>"
        f"<h3>Log</h3><pre style='font-size:11px;background:#f6f6f6;padding:8px'>"
        f"{html.escape(chr(10).join(lineas_log[-300:]))}</pre>"
        f"<p style='color:#777;font-size:12px'>Panel: https://iabot.pharmatender.cl/legacy/cierre-adjudicadas</p></div>"
    )


def enviar(asunto: str, cuerpo: str, log=print) -> bool:
    clave = os.getenv("ALERT_EMAIL_PASSWORD", "")
    if not clave:
        log("Correo de resumen NO enviado: falta ALERT_EMAIL_PASSWORD en el .env")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, REMITENTE, ", ".join(DESTINOS)
    msg.attach(MIMEText(cuerpo, "html", "utf-8"))
    for intento in range(1, 4):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
                s.login(REMITENTE, clave)
                s.sendmail(REMITENTE, DESTINOS, msg.as_string())
            log(f"Correo de resumen enviado a {', '.join(DESTINOS)}")
            return True
        except smtplib.SMTPAuthenticationError as exc:
            log(f"Correo de resumen NO enviado: autenticación rechazada ({exc})")
            return False
        except Exception as exc:  # noqa: BLE001
            log(f"Correo de resumen: intento {intento} falló ({exc})")
            time.sleep(5)
    return False
