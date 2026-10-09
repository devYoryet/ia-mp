from motor import Resultado
from reconciliar import MOTIVO_FARMA, MOTIVO_REGLAS, campos_auto, estado_efectivo, reconciliar

RES = Resultado("DEV-GUA", "Device", "Guantes médicos", "verde", "ambas", ("Guante",), "42132203")
AUTO = campos_auto(RES)
INFO = {"descripcion": "GUANTE NITRILO", "estado_gestor": None, "ia_interes": 0}


def previo(**kw):
    base = {"id": 7, "vigente": 1, "decision": None, **INFO, **AUTO}
    base.update(kw)
    return base


def test_estado_efectivo():
    assert estado_efectivo(0, 1) == 0      # manda la persona / el bot
    assert estado_efectivo(None, 0) == 0   # si no, la IA
    assert estado_efectivo(None, None) is None


def test_pendiente_no_se_toca():
    assert reconciliar(previo(), None, AUTO, INFO, "v") == ("nada", {})


def test_inserta_descarte_que_calza():
    acc, c = reconciliar(None, 0, AUTO, INFO, "v1")
    assert acc == "insertar" and c["categoria"] == "DEV-GUA" and c["version_reglas"] == "v1" and c["vigente"] == 1


def test_no_inserta_lo_que_no_calza():
    assert reconciliar(None, 1, None, INFO, "v")[0] == "nada"   # interés farma que no es Device
    assert reconciliar(None, 0, None, INFO, "v")[0] == "nada"


def test_interes_farma_que_tambien_es_device_entra_como_farma():
    farma = campos_auto(RES, "farma")
    acc, c = reconciliar(None, 1, farma, dict(INFO, pactivo_f1="Aposito"), "v")
    assert acc == "insertar" and c["estado_auto"] == "farma" and c["pactivo_f1"] == "Aposito"
    # si estaba anulada por "pasó a farma", vuelve como 'farma'
    acc, c = reconciliar(previo(vigente=0, motivo_no_vigente=MOTIVO_FARMA), 1, farma, INFO, "v")
    assert acc == "reactivar" and c["estado_auto"] == "farma"
    # y si después la eliminan en farma, pasa a la revisión normal de Device
    acc, c = reconciliar(previo(estado_auto="farma"), 0, AUTO, INFO, "v")
    assert acc == "actualizar" and c["estado_auto"] == "verde"


def test_rescate_farma_anula_aunque_este_revisada():
    acc, c = reconciliar(previo(decision="aprobado"), 1, None, INFO, "v")
    assert acc == "anular" and c["vigente"] == 0 and c["motivo_no_vigente"] == MOTIVO_FARMA
    assert "decision" not in c


def test_cambio_de_reglas_anula_solo_lo_no_revisado():
    assert reconciliar(previo(), 0, None, INFO, "v")[1]["motivo_no_vigente"] == MOTIVO_REGLAS
    assert reconciliar(previo(decision="rechazado"), 0, None, INFO, "v")[0] == "nada"


def test_decision_humana_nunca_se_pisa():
    otro = campos_auto(Resultado("SRV-MAN", "Servicio técnico", "Mantención", "revision", "solo_palabra", ("Mantención",), None))
    acc, c = reconciliar(previo(decision="aprobado"), 0, otro, INFO, "v2")
    assert acc == "nada" and c == {}


def test_actualiza_lo_no_revisado_si_cambia_la_evaluacion():
    otro = dict(AUTO, estado_auto="revision", senal="solo_palabra")
    acc, c = reconciliar(previo(), 0, otro, INFO, "v2")
    assert acc == "actualizar" and c["estado_auto"] == "revision" and c["version_reglas"] == "v2"


def test_sin_cambios_es_nada():
    assert reconciliar(previo(), 0, AUTO, INFO, "v2") == ("nada", {})


def test_info_cambia_sin_tocar_lo_automatico():
    acc, c = reconciliar(previo(decision="aprobado"), 0, AUTO, dict(INFO, estado_gestor=0), "v")
    assert acc == "info" and c == {"estado_gestor": 0}


def test_reactiva_si_vuelve_a_descarte():
    acc, c = reconciliar(previo(vigente=0, motivo_no_vigente=MOTIVO_FARMA), 0, AUTO, INFO, "v")
    assert acc == "reactivar" and c["vigente"] == 1 and c["motivo_no_vigente"] is None
