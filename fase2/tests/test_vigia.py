"""Vigía: qué detecta y cuándo avisa (sin BD, sin red, sin correo)."""
from datetime import datetime, timedelta

import vigia

T0 = datetime(2026, 10, 22, 12, 0)


class FakeBD:
    def __init__(self, barrido_hace=5, entrega_hace=5, error=None, pendientes=10, viejas=3, recientes=0, ult=None):
        self.r = dict(barrido_hace=barrido_hace, entrega_hace=entrega_hace, error=error, pendientes=pendientes,
                      viejas=viejas, recientes=recientes, ult=ult)

    def uno(self, sql, params=()):
        s = " ".join(sql.split())
        if "FROM clasificador_f2_corridas" in s and "error IS NULL" in s:
            hace = self.r["entrega_hace" if "'entrega'" in s else "barrido_hace"]
            return None if hace is None else {"creado_en": T0 - timedelta(minutes=hace), "hace": hace}
        if "FROM clasificador_f2_corridas" in s and "error IS NOT NULL" in s:
            return self.r["error"] and {"creado_en": T0, "error": self.r["error"]}
        if "estado_auto='revision'" in s:
            return {"n": self.r["pendientes"], "desde": T0 - timedelta(days=5), "viejas": self.r["viejas"]}
        if "MAX(revisado_en)" in s:
            return {"ult": self.r["ult"], "recientes": self.r["recientes"]}
        raise AssertionError(s)


def test_todo_bien_no_hay_problemas():
    assert vigia.revisar_bd(FakeBD(recientes=4), entrega=True) == {}


def test_barrido_detenido_y_entrega_con_error():
    p = vigia.revisar_bd(FakeBD(barrido_hace=120, entrega_hace=None, error="OperationalError: 2003", recientes=1),
                         entrega=True)
    assert set(p) == {"barrido", "entrega"}
    assert "hace 120 min" in p["barrido"]
    assert "No hay ninguna corrida buena" in p["entrega"] and "OperationalError" in p["entrega"]


def test_un_error_suelto_no_avisa_si_hay_corrida_buena_reciente():
    assert vigia.revisar_bd(FakeBD(error="timeout", recientes=1), entrega=True) == {}


def test_entrega_apagada_no_se_vigila():
    assert "entrega" not in vigia.revisar_bd(FakeBD(entrega_hace=None, recientes=1), entrega=False)


def test_revision_detenida():
    p = vigia.revisar_bd(FakeBD(), entrega=True)
    assert set(p) == {"revision"} and "nunca se ha revisado" in p["revision"]
    assert vigia.revisar_bd(FakeBD(viejas=0), entrega=True) == {}        # todo lo pendiente es reciente
    assert vigia.revisar_bd(FakeBD(pendientes=0), entrega=True) == {}    # nada esperando
    assert vigia.revisar_bd(FakeBD(recientes=2), entrega=True) == {}     # alguien está revisando


def test_planificar_avisa_recuerda_y_resuelve():
    aviso, e1 = vigia.planificar({"barrido": "x"}, {}, T0)
    assert aviso["nuevos"] == ["barrido"]
    aviso, e2 = vigia.planificar({"barrido": "x"}, e1, T0 + timedelta(hours=1))
    assert aviso == {} and e2["problemas"]["barrido"]["aviso"] == e1["problemas"]["barrido"]["aviso"]
    aviso, e3 = vigia.planificar({"barrido": "x"}, e2, T0 + timedelta(hours=6))
    assert aviso["recordar"] == ["barrido"]
    assert e3["problemas"]["barrido"]["desde"] == e1["problemas"]["barrido"]["desde"]
    aviso, e4 = vigia.planificar({}, e3, T0 + timedelta(hours=7))
    assert aviso["resueltos"] == ["barrido"] and e4 == {"problemas": {}}
    assert vigia.planificar({}, e4, T0 + timedelta(hours=8))[0] == {}


def test_revision_se_recuerda_una_vez_al_dia():
    _, e = vigia.planificar({"revision": "x"}, {}, T0)
    assert vigia.planificar({"revision": "x"}, e, T0 + timedelta(hours=12))[0] == {}
    assert vigia.planificar({"revision": "x"}, e, T0 + timedelta(hours=24))[0]["recordar"] == ["revision"]


def test_redactar():
    actuales = {"panel": "Sin respuesta", "revision": "10 filas"}
    aviso, e = vigia.planificar(actuales, {}, T0)
    asunto, texto = vigia.redactar(aviso, actuales, e, "device.pharmatender.cl")
    assert "el panel de revisión no responde" in asunto and "nadie está revisando" in asunto
    assert "https://device.pharmatender.cl/resumen" in texto and "(nuevo)" in texto
    asunto, texto = vigia.redactar({"resueltos": ["panel"]}, {}, {"problemas": {}}, "")
    assert asunto.endswith("todo resuelto") and "RESUELTOS" in texto


def test_panel_caido_se_detecta():
    p = vigia.revisar_panel("", puerto_local=9)  # puerto sin servidor
    assert set(p) == {"panel"}
