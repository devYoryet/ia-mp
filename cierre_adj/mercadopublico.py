"""Mercado Público: API pública (listado y detalle) y acta de adjudicación del portal.

API (https://api.mercadopublico.cl, ticket en MP_API_TICKET):
  * listado por día: licitaciones.json?fecha=DDMMYYYY&estado=adjudicada
    Medido 2026-09-16 sobre agosto 2026: 6.239 licitaciones, de las cuales 23
    NO estaban en listado_api (el listado diario del portal las perdió). Es la
    segunda fuente del listado.
  * detalle: licitaciones.json?codigo=X trae Items con su Adjudicacion.
    Medido en 7 licitaciones (incluida una de 516 ítems): los ítems con
    adjudicación de la API == ítems 'Adjudicada' descargados del acta. Es la
    prueba de que un acta quedó completa.
  * Responde 429 si las llamadas van seguidas: pausa MP_API_PAUSA (6 s) y
    espera creciente ante 429.

Acta (portal): mismo parseo que licitacionesAdjudicadasOptimizado.py (Windows),
con una diferencia a propósito: si falla un ítem, la licitación NO se marca
completa (antes se hacía `continue` y quedaba estado=1 con el acta a medias).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from cierre_adj.bd import COLS_LICITACIONES

API = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
PORTAL = "https://www.mercadopublico.cl"
PAUSA_API = float(os.getenv("MP_API_PAUSA", "6"))
TIMEOUTS_PORTAL = tuple(int(x) for x in os.getenv("ADJ_TIMEOUTS_POR_INTENTO", "30,90,180").split(","))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_ultima_llamada = 0.0


class ErrorAPI(Exception):
    pass


def _ticket() -> str:
    t = os.getenv("MP_API_TICKET", "").strip()
    if not t:
        raise ErrorAPI("Falta MP_API_TICKET en el .env")
    return t


def _api(params: str, intentos: int = 6) -> dict:
    global _ultima_llamada
    url = f"{API}?{params}&ticket={_ticket()}"
    ultimo = None
    for i in range(1, intentos + 1):
        espera = PAUSA_API - (time.monotonic() - _ultima_llamada)
        if espera > 0:
            time.sleep(espera)
        _ultima_llamada = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            ultimo = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(15 * i)
                continue
            raise ErrorAPI(f"HTTP {exc.code} en {params}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            ultimo = exc
            time.sleep(10 * i)
    raise ErrorAPI(f"API sin respuesta tras {intentos} intentos ({params}): {ultimo}")


def listado_adjudicadas(dia: str) -> set[str]:
    """Códigos adjudicados que la API informa para `dia` ('YYYY-MM-DD')."""
    a, m, d = dia.split("-")
    datos = _api(f"fecha={d}{m}{a}&estado=adjudicada")
    return {x["CodigoExterno"].strip() for x in datos.get("Listado", []) if x.get("CodigoExterno")}


def detalle(codigo: str) -> dict | None:
    """Resumen del detalle de la API: estado, fecha de adjudicación y ítems.

    Devuelve None si la API no conoce el código."""
    datos = _api(f"codigo={codigo}")
    listado = datos.get("Listado") or []
    if not listado:
        return None
    lic = listado[0]
    items = (lic.get("Items") or {}).get("Listado") or []
    return {
        "codigo": codigo,
        "estado": lic.get("Estado"),
        "fecha_adjudicacion": ((lic.get("Fechas") or {}).get("FechaAdjudicacion") or "")[:10] or None,
        "items": len(items),
        "items_adjudicados": sum(1 for it in items if it.get("Adjudicacion")),
    }


# ------------------------------------------------------------------ portal ---

class SinActa(Exception):
    """El portal no publica acta de adjudicación ni de deserción (aún)."""


class ErrorTemporal(Exception):
    """Red, timeout o 5xx: se reintenta en la próxima corrida."""


def _html(url: str, sesion: requests.Session) -> BeautifulSoup:
    ultimo = None
    for t in TIMEOUTS_PORTAL:
        try:
            r = sesion.get(url, timeout=t)
            if r.status_code == 404:
                raise SinActa("404 en el portal")
            if r.status_code >= 500 or r.status_code == 403:
                ultimo = f"HTTP {r.status_code}"
                time.sleep(5)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as exc:
            ultimo = exc
            time.sleep(3)
    raise ErrorTemporal(f"portal sin respuesta: {ultimo}")


def _texto(soup, id_):
    el = soup.find(id=id_)
    if el is None:
        raise ValueError(f"no está el campo {id_}")
    return el.get_text()


def _fecha_sql(txt: str, respaldo: str | None) -> str | None:
    try:
        return datetime.strptime(txt.strip(), "%d/%m/%Y %H:%M").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return respaldo


def descargar_acta(licitacion: str, fecha_respaldo: str, sesion: requests.Session | None = None) -> dict:
    """Descarga y parsea el acta. No escribe en la BD.

    Devuelve {'estado': 'adjudicada'|'desierta'|'sin_items', 'filas': [tuplas en
    el orden de COLS_LICITACIONES], 'items': n, 'items_con_error': [..]}.
    Lanza SinActa o ErrorTemporal."""
    sesion = sesion or requests.Session()
    sesion.headers.setdefault("User-Agent", UA)
    soup = _html(f"{PORTAL}/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={licitacion}", sesion)

    desierta = soup.find(id="imgDesercion")
    if desierta is not None and desierta.has_attr("href"):
        return {"estado": "desierta", "filas": [], "items": 0, "items_con_error": []}
    adjud = soup.find(id="imgAdjudicacion")
    if adjud is None or not adjud.has_attr("href"):
        raise SinActa("Sin acta de adjudicación ni deserción")

    link_acta = PORTAL + adjud["href"]
    acta = _html(link_acta, sesion)
    try:
        fecha_pub = _texto(acta, "lblPublishDateShow")
        fecha_cierre = _texto(acta, "lblEndDateShow")
        fecha_adj = _texto(acta, "lblTitlePorcDateDesc")
        generales = {
            "ADQUISICION": licitacion,
            "CLIENTE": _texto(acta, "lblOrgDemRazonSocialDesc"),
            "RUT": _texto(acta, "lblOrgDemRUTDesc"),
            "DIRECCION": _texto(acta, "lblOrgDemDireccionDesc"),
            "COMUNA": _texto(acta, "lblOrgDemComunaDesc"),
            "REGION": _texto(acta, "lblOrgDemCiudadDesc"),
            "FECHAPUBLICACION": fecha_pub,
            "FECHACIERRE": fecha_cierre,
            "FECHAADJUDICACION": fecha_adj,
            "FECHASQL": _fecha_sql(fecha_adj, fecha_respaldo),
            "FECHASQLCIERRE": _fecha_sql(fecha_cierre, None),
            "FECHASQLPUBLICACION": _fecha_sql(fecha_pub, None),
            "LINK_ACTA": link_acta,
        }
    except ValueError as exc:
        raise ErrorTemporal(f"acta con formato inesperado: {exc}") from exc

    n_items = len(acta.find_all(id="rptBids_"))
    filas: list[tuple] = []
    con_error: list[str] = []
    for idx in range(n_items):
        ctl = f"{idx + 2:02d}"
        try:
            pre = f"grdItemOC_ctl{ctl}_ucAward_"
            item = {
                "PRODUCTO": _texto(acta, f"{pre}_lblNumber"),
                "CODONU": _texto(acta, f"{pre}lblCodeonu"),
                "DESCONU": _texto(acta, f"{pre}_LblSchemaTittle"),
                "ESPCOMPRADOR": _texto(acta, f"{pre}lblDescription"),
                "CANTIDAD": _texto(acta, f"{pre}_LblRBICuantityNumber"),
                "NUMPROD": _texto(acta, f"{pre}_lblNumber"),
            }
            tabla = acta.find(id=f"{pre}gvLines")
            if tabla is None:
                continue  # ítem sin ofertas: igual que el script original
            for tr in tabla.find_all("tr", {"class": ["cssPRCGridViewAltRow", "cssPRCGridViewRow"]}):
                tds = tr.find_all("td")
                proveedores = tds[0].text.strip()
                monto = tds[2].text
                fila = {
                    **generales, **item,
                    "PROVEEDORES": proveedores,
                    "RUTPROVEEDORES": proveedores.split()[0],
                    "RAZONSOCIALPROVEEDORES": proveedores.split(" ", 1)[1] if len(proveedores) > 12 else None,
                    "ESPECIFICACIONPROVEEDORES": tds[1].text.strip(),
                    "MONTOUNITARIOPROVEEDOR": monto.split()[1],
                    "CANTADJUDICADA": tds[3].text,
                    "NETOADJUDICADO": tds[4].text,
                    "ESTADO": tds[5].text.strip(),
                    "TIPOMONEDA": monto.split()[0],
                    "SUCURSALPROVEEDOR": None,
                    "DESCRIPCION": f"{item['ESPCOMPRADOR']} {tds[1].text.strip()}",
                }
                filas.append(tuple(fila[c] for c in COLS_LICITACIONES))
        except (ValueError, IndexError) as exc:
            con_error.append(f"ítem {ctl}: {exc}")
    return {
        "estado": "adjudicada" if filas else "sin_items",
        "filas": filas,
        "items": n_items,
        "items_con_error": con_error,
    }
