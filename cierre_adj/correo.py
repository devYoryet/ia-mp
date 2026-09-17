"""Resumen técnico del cierre (solo cuando falla). Se envía con correos.enviar:
mismo remitente y prefijo "[Automático]" que los avisos de negocio."""

from __future__ import annotations

import html
import os
import re

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
    """Resumen técnico (solo cuando el cierre falla). Mismo emisor que los avisos de negocio."""
    import correos
    texto = re.sub(r"<[^>]+>", " ", cuerpo)
    return correos.enviar(asunto, html.unescape(texto), ", ".join(DESTINOS), log)
