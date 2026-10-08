"""El código nunca manda a MySQL una escritura sobre una tabla que no sea clasificador_f2_*."""
import pytest

from bd import BD, validar_escritura, validar_lectura


@pytest.mark.parametrize("sql", [
    "UPDATE compra_agil SET estado_gestor=1 WHERE id=1",
    "update `Licitaciones_diarias` set pactivo='x'",
    "INSERT INTO clasificador_ia_log (x) VALUES (1)",
    "INSERT INTO licitaciones_diarias_total_farma.compra_agil (x) VALUES (1)",
    "UPDATE clasificador_f2_terminos SET activa=0",          # config: la edita una persona, no el barrido
    "DELETE FROM clasificador_f2_resultado WHERE id=1",       # nunca DELETE
    "REPLACE INTO clasificador_f2_resultado VALUES (1)",
    "DROP TABLE clasificador_f2_resultado",
    "TRUNCATE clasificador_f2_resultado",
    "   ",
])
def test_escrituras_ajenas_bloqueadas(sql):
    with pytest.raises(PermissionError):
        validar_escritura(sql)


@pytest.mark.parametrize("sql,tabla", [
    ("INSERT INTO clasificador_f2_resultado (a) VALUES (1)", "clasificador_f2_resultado"),
    ("UPDATE clasificador_f2_resultado SET a=1 WHERE id=2", "clasificador_f2_resultado"),
    ("INSERT INTO `licitaciones_diarias_total_farma`.`clasificador_f2_corridas` (a) VALUES (1)", "clasificador_f2_corridas"),
])
def test_escrituras_propias_permitidas(sql, tabla):
    assert validar_escritura(sql) == tabla


def test_lectura_solo_select():
    validar_lectura("SELECT 1")
    with pytest.raises(PermissionError):
        validar_lectura("UPDATE compra_agil SET x=1")


def test_transaccion_valida_todo_antes_de_conectar():
    bd = BD({"host": "nunca", "port": 1, "user": "x", "password": "x", "database": "x"})
    with pytest.raises(PermissionError):
        bd.transaccion([("INSERT INTO clasificador_f2_corridas (a) VALUES (1)", ()),
                        ("UPDATE compra_agil SET estado_gestor=1 WHERE id=1", ())])
    assert bd._esc is None  # ni siquiera abrió la conexión
