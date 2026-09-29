"""Pruebas del registro de fuentes y de la limpieza del sufijo UNSPSC.

Correr:  python3 -m pytest tests/ -q   (no toca la BD ni la API)"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fuentes  # noqa: E402


def _glosas(cod, *descs):
    return [{"cod": cod, "Descripcion": d} for d in descs]


def test_registro_columnas_rubro():
    # compra_agil y cotizaciones: rubro en Item; Licitaciones_diarias: en Cod_Onu
    assert fuentes.FUENTES["compra_agil"].columna_rubro == "Item"
    assert fuentes.FUENTES["cotizaciones"].columna_rubro == "Item"
    assert fuentes.FUENTES["Licitaciones_diarias"].columna_rubro == "Cod_Onu"


def test_modelo_adjunto_por_fuente():
    assert fuentes.usa_modelo_adjunto("compra_agil")
    assert fuentes.usa_modelo_adjunto("Licitaciones_diarias")
    assert not fuentes.usa_modelo_adjunto("cotizaciones")


def test_umbral_modelo_pactivo_por_fuente():
    assert fuentes.umbral_modelo_pactivo("compra_agil", 0.30) == 0.30
    assert fuentes.umbral_modelo_pactivo("cotizaciones", 0.30) == 0.50


def test_worker_default_no_incluye_cotizaciones(monkeypatch):
    monkeypatch.delenv("FUENTES_WORKER", raising=False)
    assert fuentes.fuentes_worker() == ["compra_agil", "Licitaciones_diarias"]


def test_worker_rechaza_tabla_desconocida(monkeypatch):
    monkeypatch.setenv("FUENTES_WORKER", "compra_agil,tabla_inventada")
    try:
        fuentes.fuentes_worker()
    except ValueError:
        return
    raise AssertionError("debió rechazar una tabla no registrada")


def test_aprende_sufijo_comun():
    s = fuentes._aprender_sufijos(
        _glosas("51181508",
                "SEMAGLUTIDA 4MG/3ML SOL/INY DISP+AG Glucagon",
                "SEMAGLUTIDA 2MG/1,5ML SOL/INY DISP+AG Glucagon",
                "INSULINA GLARGINA 100UI/ML Glucagon")
        + _glosas("51201502",
                  "50-219-100-290-00 ALPELISIB 250MG Ciclosporina",
                  "50-219-200-064-00 TRAMETINIB 2MG Ciclosporina"))
    assert s == {"51181508": "Glucagon", "51201502": "Ciclosporina"}


def test_sufijo_de_varias_palabras():
    s = fuentes._aprender_sufijos(_glosas(
        "42171903",
        "DABIGATRAN 110 MG CAPSULA. Cajas de medicamentos de servicios médicos de urgencia",
        "BOTIQUIN BASICO Cajas de medicamentos de servicios médicos de urgencia"))
    assert s["42171903"] == "Cajas de medicamentos de servicios médicos de urgencia"


def test_no_aprende_con_una_sola_glosa_ni_con_glosas_identicas():
    s = fuentes._aprender_sufijos(
        _glosas("1", "TIPTOP 25MM COLOR COYOTE Hebillas")
        + _glosas("2", "ALPELISIB 250MG Ciclosporina", "ALPELISIB 250MG Ciclosporina"))
    assert s == {}


def test_nunca_se_come_la_glosa_entera():
    # 'X Glucagon' y 'Y Glucagon': el sufijo es 'Glucagon', no más
    s = fuentes._aprender_sufijos(_glosas("9", "A Glucagon", "B Glucagon"))
    assert s == {"9": "Glucagon"}


def test_preparar_fila_cotizaciones(monkeypatch):
    monkeypatch.delenv("LIMPIAR_SUFIJO_UNSPSC", raising=False)
    fuentes._SUFIJOS["cotizaciones"] = {"51181508": "Glucagon"}
    cruda = "SEMAGLUTIDA 4MG/3ML SOL/INY DISP+AG Glucagon"
    fila = {"id": 1, "Item": "51181508", "Descripcion": cruda,
            "VINCULOS": cruda + ".SEMAGLUTIDA 4MG/3ML SOLUCION INYECTABLE"}
    out = fuentes.preparar_fila("cotizaciones", fila)
    assert out["Descripcion"] == "SEMAGLUTIDA 4MG/3ML SOL/INY DISP+AG"
    assert out["_desc_cruda"] == cruda
    assert out["VINCULOS"] == "SEMAGLUTIDA 4MG/3ML SOL/INY DISP+AG.SEMAGLUTIDA 4MG/3ML SOLUCION INYECTABLE"
    assert fila["Descripcion"] == cruda  # la fila original no se toca


def test_preparar_fila_sin_sufijo_conocido_no_cambia():
    fuentes._SUFIJOS["cotizaciones"] = {"51181508": "Glucagon"}
    fila = {"Item": "99999999", "Descripcion": "ALGO Glucagon"}
    assert fuentes.preparar_fila("cotizaciones", fila) is fila
    fila2 = {"Item": "51181508", "Descripcion": "SEMAGLUTIDA sin el nombre al final"}
    assert fuentes.preparar_fila("cotizaciones", fila2) is fila2


def test_preparar_fila_compra_agil_es_identidad():
    fila = {"Item": "51181508", "Descripcion": "PARACETAMOL 500 MG Glucagon"}
    assert fuentes.preparar_fila("compra_agil", fila) is fila
    assert fuentes.preparar_fila("Licitaciones_diarias", fila) is fila


def test_ab_se_puede_apagar(monkeypatch):
    fuentes._SUFIJOS["cotizaciones"] = {"51181508": "Glucagon"}
    monkeypatch.setenv("LIMPIAR_SUFIJO_UNSPSC", "0")
    fila = {"Item": "51181508", "Descripcion": "SEMAGLUTIDA Glucagon"}
    assert fuentes.preparar_fila("cotizaciones", fila) is fila
