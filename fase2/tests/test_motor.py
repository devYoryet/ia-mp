"""Casos reales (glosas medidas el 2026-10-08) contra la semilla v1.1."""
import pytest

import motor
import semilla

CATS, ERRORES = motor.compilar(semilla.CATEGORIAS)


def ev(desc, cod="", titulo=""):
    r = motor.evaluar(CATS, desc, titulo, cod)
    return (r.categoria, r.estado_auto, r.senal) if r else None


def test_semilla_compila_sin_errores():
    assert ERRORES == []
    assert [c.codigo for c in CATS] == ["SRV-MAN", "DEV-OFT", "DEV-MIC", "DEV-GUA", "DEV-DET", "DEV-APO"]


def test_normalizar():
    assert motor.normalizar("  Apósito  ESTÉRIL\t10x10 ") == "aposito esteril 10x10"


@pytest.mark.parametrize("desc,cod,esperado", [
    # Guantes
    ("GUANTE NITRILO TALLA M CAJA 100 UNIDADES", "42132203", ("DEV-GUA", "verde", "ambas")),
    ("GUANTES NITRILO TALLA M (100 UNIDADES)", "51141613", ("DEV-GUA", "revision", "solo_palabra")),  # ONU mal puesto
    ("Guantes desechables pack de 100 unidades talla L", "46181504", ("DEV-GUA", "verde", "ambas")),   # EPP confirma
    ("GUANTE CABRITILLA C/FORRO", "46181504", None),
    ("GUANTES DE BOX 16 ONZ", "49171603", None),
    ("Guante talla M", "42132205", ("DEV-GUA", "revision", "palabra_debil+onu")),
    ("Cofias (100 unidades) 5", "42132205", None),  # ONU solo no revisa en guantes
    # Mantención de equipos médicos
    ("SERVICIO DE MANTENCION PREVENTIVA A EQUIPOS AUTOCLAVE MARKET FORGE", "73152101", ("SRV-MAN", "verde", "ambas")),
    ("Mantenimiento correctivo Lámpara de Hendidura, Topcon, SL-D7", "42183021", ("SRV-MAN", "verde", "ambas")),
    ("MTTO PREVENTIVO Y CORRECTIVO HOLTER MORTARA", "81101701", ("SRV-MAN", "revision", "solo_palabra")),
    ("MANTENCION PREVENTIVA DE EXTINTORES", "46191601", None),
    ("Mantención preventiva camioneta Toyota", "73152101", None),
    ("ARTROSCOPIA REPARACION MENISCAL, según especificaciones", "42295112", None),
    # Oftalmología
    ("LENTE INTRAOCULAR MONOFOCAL TORICO", "31241501", ("DEV-OFT", "verde", "ambas")),
    ("CUCHILLETE 15 GRADOS", "42294525", ("DEV-OFT", "verde", "ambas")),  # v1.3: "cuchillete" es vocabulario oftálmico
    ("ESPATULA 23 G PARA DELAMINAR MEMBRANAS DE RETINA", "42294525", ("DEV-OFT", "revision", "solo_onu")),
    ("Servicio de catering 15 OCT 2026", "90101603", None),               # OCT = octubre
    ("Equipo OCT para estudio de retina", "", ("DEV-OFT", "revision", "solo_palabra")),
    ("Proyector EPSON 3800 lumenes XGA", "42183015", None),               # código de optotipos mal usado
    # Microscopía, detergentes, apósitos
    ("Microscopio Binocular 1000X con platina mecánica", "41111709", ("DEV-MIC", "verde", "ambas")),
    ("CAJAS PORTAOBJETOS PARA MICROSCOPIA 50 UNIDADES", "41121802", None),
    ("DETERGENTE ENZIMATICO NEUTRO PARA LAVAR INSTRUMENTAL QUIRURGICO", "42281704", ("DEV-DET", "verde", "ambas")),
    ("DETERGENTE EN POLVO 400 GRAMOS", "12161902", None),
    ("Detergente liquido 5 litros.", "42281704", ("DEV-DET", "revision", "palabra_debil+onu")),
    ("DETERGENTE EXTRAN ALCAL. 25 LTS", "12352204", ("DEV-DET", "revision", "solo_palabra")),  # 'Enzimas' comodín no cuenta
    ("APOSITO TRANSPARENTE PARA CATETER VENOSO CENTRAL", "42311526", ("DEV-APO", "verde", "ambas")),
    ("TOALLAS ANTISEPTICAS CON ALCOHOL ISOPROPILICO 70%", "42311532", None),  # ONU solo no revisa en apósitos
    # Nada que ver
    ("Computador All IN ONE (4). Según TTR", "43211507", None),
])
def test_casos_reales(desc, cod, esperado):
    assert ev(desc, cod) == esperado


def test_contexto_en_titulo():
    # La palabra está en la glosa; el objeto médico, sólo en el título.
    assert ev("MANTENIMIENTO PREVENTIVO segun anexo", "73152101",
              titulo="Mantención de electrocardiógrafos y bombas de infusión") == ("SRV-MAN", "verde", "ambas")


def test_servicio_gana_a_producto_y_registra_otras():
    r = motor.evaluar(CATS, "Mantención correctiva Microscopio Olympus CX31", "", "73152101")
    assert (r.categoria, r.estado_auto) == ("SRV-MAN", "verde")
    assert "DEV-MIC" in r.otras


def test_subcategoria_es_el_termino_mas_especifico():
    r = motor.evaluar(CATS, "LENTE INTRAOCULAR para cirugía oftalmológica", "", "422945")
    assert r.subcategoria == "Lentes intraoculares"
    assert r.terminos == ("Lentes intraoculares", "Intraocular", "Oftalmología")


def test_terminos_inactivos_no_cuentan():
    # 'Calibración' sigue inactiva (es adjetivo de producto).
    assert ev("Servicio de calibración de balanza de laboratorio", "") is None


def test_regex_invalida_se_saltea_sin_tumbar_el_resto():
    cfg = [{"codigo": "X", "linea": "Device", "nombre": "X", "prioridad": 1, "onu_solo_a_revision": False,
            "terminos": [{"nombre": "malo", "regex": "(", "contexto": None},
                         {"nombre": "bueno", "regex": r"\bgasa\b", "contexto": None}],
            "excluye": [], "onu": []}]
    cats, err = motor.compilar(cfg)
    assert len(err) == 1 and "malo" in err[0]
    assert motor.evaluar(cats, "gasa esteril", "", "").terminos == ("bueno",)


def test_version_estable_y_sensible():
    v = motor.version(semilla.CATEGORIAS)
    assert v == motor.version(semilla.CATEGORIAS)
    import copy
    otra = copy.deepcopy(semilla.CATEGORIAS)
    otra[3]["onu"].append({"codigo": "4213", "fuerza": "confirma", "activa": True})
    assert motor.version(otra) != v
    # Un término INACTIVO no cambia la versión (no cambia ningún resultado).
    inact = copy.deepcopy(semilla.CATEGORIAS)
    inact[0]["terminos"].append({"nombre": "z", "regex": "z", "contexto": None, "activa": False})
    assert motor.version(inact) == v


def test_sufijo_onu_de_cotizaciones():
    from barrido import glosa_sin_sufijo_onu
    assert glosa_sin_sufijo_onu("Talla M caja 100 Guantes quirúrgicos", "Guantes quirúrgicos") == "Talla M caja 100"
    assert glosa_sin_sufijo_onu("Guantes quirúrgicos", "Guantes quirúrgicos") == "Guantes quirúrgicos"  # nunca vacía
    assert glosa_sin_sufijo_onu("GUANTE NITRILO M", "Guantes quirúrgicos") == "GUANTE NITRILO M"
    # sin el sufijo, la palabra ya no sale del nombre ONU: queda sólo la señal ONU (que en guantes no revisa)
    assert ev(glosa_sin_sufijo_onu("Talla M caja 100 Guantes quirúrgicos", "Guantes quirúrgicos"), "42132205") is None


@pytest.mark.parametrize("desc,cod,esperado", [
    ("Se requiere la adquisición de guates de nitrilo talla S, acorde a las EETT.", "42132203", ("DEV-GUA", "verde", "ambas")),
    ("GUANTES NITRILO (PRESENTACIÓN BOX 50 UNIDADES) ESPECIFICACIONES EN LISTADO ADJUNTO", "42132205", ("DEV-GUA", "verde", "ambas")),
    ("GUANTES DE BOX 16 ONZ", "49171603", None),
])
def test_guantes_v12(desc, cod, esperado):
    assert ev(desc, cod) == esperado


def test_titulo_mas_onu_va_a_revision_nunca_a_verde():
    t = "Adquisición de Guantes de Nitrilo para Laboratorio"
    assert ev("Talla M - de acuerdo a especificaciones adjuntas", "42132203", titulo=t) == ("DEV-GUA", "revision", "titulo+onu")
    # sin ONU de la categoría, el título solo no basta
    assert ev("Talla M - de acuerdo a especificaciones adjuntas", "53102504", titulo=t) is None
    # el paraguas de la licitación no pisa una glosa que ya calzó en otra categoría
    r = motor.evaluar(CATS, "Microscopio binocular", "INSUMOS: GUANTES NITRILO, MICROSCOPIOS", "41111709")
    assert (r.categoria, r.senal) == ("DEV-MIC", "ambas")
    # exclusiones también miran el título
    assert ev("Talla M", "46181504", titulo="Guantes de cabritilla para bodega") is None


def test_diagnosticar_explica_lo_que_quedo_fuera():
    d = lambda desc, cod="", tit="": motor.diagnosticar(CATS, desc, tit, cod)
    assert d("GUANTE CABRITILLA C/FORRO", "46181504") == ["DEV-GUA · excluida por 'guante no médico'"]
    assert d("Mantención preventiva de generador eléctrico") == ["SRV-MAN · excluida por 'vehículo/inmueble'"]
    assert d("MANTENCION PREVENTIVA BODEGA") == ["SRV-MAN · 'Preventiva' sin contexto ni ONU fuerte"]
    assert d("Cofias (100 unidades)", "42132205") == ["DEV-GUA · código ONU sin palabra"]
    assert d("GUANTECITOS DE LATEX TALLA S") == ["raíz 'guant' sin calce"]
    assert d("Computador All in One", "43211507") == []



@pytest.mark.parametrize("desc,cod,titulo,esperado", [
    # casos reales que la auditoría con IA encontró fuera (lote 20261008-2154)
    ("MANTENCIÓN CORRECTIVA A EQUIPO DE ULTRASONIDO GE LOGIQ E", "42201703", "", ("SRV-MAN", "verde", "ambas")),
    ("Mantención correctiva de mesa quirúrgica marca Trumpf", "42191807", "", ("SRV-MAN", "verde", "ambas")),
    ("Mantención preventiva Microtomo automatico THERMO", "73152101", "MANTENCIÓN EQUIPOS DE ANATOMÍA PATOLÓGICA", ("SRV-MAN", "verde", "ambas")),
    ("MANTENCION PREVENTIVA COMPRESORES Y BOMBAS DE VACIO", "73152101", "", None),
    ("Tonómetro de aplanación", "", "", ("DEV-OFT", "revision", "solo_palabra")),
    ("340-9992 BLEFAROSTATO NEONATAL TERMINO 9MM", "", "", ("DEV-OFT", "revision", "solo_palabra")),
    ("340-3322 PROTECTOR OCULAR", "", "INSUMOS USO OFTALMOLOGICO CIRUGIA", ("DEV-OFT", "revision", "titulo")),
    ("Silla De Ruedas Estándar - Incluido Cojín Viscoelástico", "", "", None),
    ("22510103 - GUANTE QUIRURGICO SIN LATEX ESTERIL, SIN ACELERADOR QUIMICO", "42132205", "", ("DEV-GUA", "verde", "ambas")),
    ("Guante de nitrilo texturizado de alta resistencia química", "46181504", "", None),
    ("CAJAS DE GUANNTES DE NITRILO SIN POLVO TALLA XS", "42132203", "", ("DEV-GUA", "verde", "ambas")),
    ("DETERGENTE ENZIMATICO FORTE 5 LTS PARA LAVADORA MMM", "42281704", "", ("DEV-DET", "verde", "ambas")),
    ("Espuma multi-enzimática para prelavado de instrumental", "42281704", "", ("DEV-DET", "verde", "ambas")),
    ("DETERGENTE PH NEUTRO PARA CARPETA DE GIMNASIO", "47131810", "", None),
    ("ESTEREOMICROSCOPIO TRINOCULAR", "41111709", "", ("DEV-MIC", "verde", "ambas")),
    ("PARCHE MEDICO CURITA RECTANGULAR ESTERIL CAJA 100", "42311506", "", ("DEV-APO", "verde", "ambas")),
])
def test_v13_auditoria(desc, cod, titulo, esperado):
    assert ev(desc, cod, titulo=titulo) == esperado
