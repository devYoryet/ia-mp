"""Match del gestor 2: suscripción por diccionario, fila idéntica al legacy y candado."""
from datetime import date, datetime

import pytest

import entrega as e

CATS = {
    "DEV-GUA": {"codigo": "DEV-GUA", "linea": "Device", "nombre": "Guantes médicos"},
    "DEV-OFT": {"codigo": "DEV-OFT", "linea": "Device", "nombre": "Oftalmología"},
    "SRV-MAN": {"codigo": "SRV-MAN", "linea": "Servicio técnico", "nombre": "Mantención de equipos médicos"},
}


def test_suscripcion_sale_del_diccionario():
    dic = [
        {"intIdUnidad_negocio": 79, "pactivo": "Guantes médicos", "composicion": "Sin cla"},
        {"intIdUnidad_negocio": 79, "pactivo": "OFTALMOLOGIA", "composicion": "Lentes intraoculares"},
        {"intIdUnidad_negocio": 255, "pactivo": "Mantención de equipos médicos", "composicion": ""},
        {"intIdUnidad_negocio": 78, "pactivo": "Latanoprost", "composicion": "Sin cla"},   # farma: no cuenta
    ]
    s = e.suscripciones(dic, CATS)
    assert set(s) == {79, 255}
    assert ("DEV-GUA", None) in s[79] and ("DEV-OFT", "lentes intraoculares") in s[79]
    assert s[255] == [("SRV-MAN", None)]


def test_corresponde_con_filtro_de_subcategoria():
    subs = [("DEV-GUA", None), ("DEV-OFT", "lentes intraoculares")]
    assert e.corresponde(subs, "DEV-GUA", "Guante")
    assert e.corresponde(subs, "DEV-OFT", "Lentes intraoculares")
    assert not e.corresponde(subs, "DEV-OFT", "Campímetro")
    assert not e.corresponde(subs, "SRV-MAN", "Preventiva")


SRC = {c: f"v_{c}" for c in e.BASE + e.CONTACTO}
SRC.update({"Fecha_Publicacion": datetime(2026, 10, 10, 9), "Fecha_Cierre": datetime(2026, 10, 20, 15),
            "Licitacion": "1234-5-COT26", "Item": "42132203", "Cod_Onu": "3", "pactivo": None, "llave": None})


def test_fila_es_la_del_legacy_sin_marca():
    u = {"id": 79, "nombre": "Device", "rut": "94544000-7"}
    f = e.construir_fila("compra_agil", SRC, u, CATS["DEV-GUA"], "Guante", date(2026, 10, 13))
    esperadas = set(e.BASE + e.CONTACTO) | {"Campo21", "strRutCliente", "datFechaPublicacion", "datFechaCierre",
                                            "pactivo", "composicion", "presentacion", "datFechaSubida", "negocio"}
    assert set(f) == esperadas                          # los mismos campos que arma el gestor 1
    assert "llave" not in f                            # sin marca en tablas ajenas: queda NULL como siempre
    assert (f["pactivo"], f["composicion"], f["presentacion"], f["negocio"]) == ("Guantes médicos", "Guante", "Sin cla", "Device")
    assert e.clave_fila("Licitaciones_diarias_clientes_agil", f) == ("1234-5-COT26", "42132203", "3", "94544000-7")
    lic = e.construir_fila("Licitaciones_diarias", SRC, u, CATS["DEV-GUA"], None, date(2026, 10, 13))
    assert "monto_total" not in lic and lic["composicion"] == "Sin cla"   # licitaciones no tiene contacto


@pytest.mark.parametrize("sql", [
    "UPDATE licitaciones_diarias_intranet.Licitaciones_diarias_clientes_agil SET pactivo='x'",   # nunca UPDATE ajeno
    "DELETE FROM licitaciones_diarias_intranet.Licitaciones_diarias_clientes WHERE Licitacion=%s",  # DELETE sin condición de retiro
    "DELETE FROM clasificador_f2_entregas WHERE id=1",
    "INSERT INTO principal_app.diccionario_unidad (pactivo) VALUES ('x')",
    "INSERT INTO compra_agil (x) VALUES (1)",
    "UPDATE compra_agil SET estado_gestor=1",
    "TRUNCATE licitaciones_diarias_intranet.Licitaciones_diarias_clientes",
])
def test_candado_bloquea(sql):
    with pytest.raises(PermissionError):
        e.validar_sql_entrega(sql)


def test_candado_permite_solo_lo_previsto():
    e.validar_sql_entrega("INSERT INTO licitaciones_diarias_intranet.Licitaciones_diarias_clientes_agil (a) VALUES (%s)")
    e.validar_sql_entrega(e.SQL_LIBRO)
    for d in e.DESTINO.values():
        e.validar_sql_entrega(e.sql_retiro(d))          # el único DELETE: con PK, contenido propio y sin tocar
    assert "pactivo`=%s AND `negocio`=%s" in e.sql_retiro("Licitaciones_diarias_clientes")
    assert "intEstadoInterno" in e.sql_retiro("Licitaciones_diarias_clientes")


def test_conexion_de_lectura_no_escribe():
    c = e.Conexion({"host": "x", "port": 1, "user": "x", "password": "x", "database": "x"}, solo_lectura=True)
    with pytest.raises(PermissionError):
        c.ejecutar(e.SQL_LIBRO, ())
