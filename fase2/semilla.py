"""Semilla v1.1 de la configuración de la Fase 2 — la medida el 2026-10-08.

Se carga UNA vez a las tablas clasificador_f2_* con `admin.py semilla`. Después
la fuente de verdad son las tablas (editables sin deploy), no este archivo.

Las regex se escriben contra el texto NORMALIZADO (sin tildes, minúsculas):
ver `motor.normalizar`. Cada categoría declara sus términos del más específico
al más genérico: el primero que calza es la subcategoría.

`origen`: 'cliente' = palabra de la lista de Megalabs (o su variante);
'sugerido' = propuesta de Pharmatender. Los sugeridos con activa=False se
validan con el cliente antes de encenderlos.
Volúmenes y precisión: docs/06-fase2-device.md.
"""

VERSION_SEMILLA = "v1.1"

# Objeto médico para Servicio técnico: el equipo que se mantiene. Se busca en
# glosa o título.
OBJ_MEDICO = (
    r"\bequipos?\s+(\w+\s+){0,2}(medic|clinic|hospital|biomedic|de\s+(salud|laboratorio|oftalm|esteriliz|imagen|rayos|odontolog|dental|anestesia|monitoreo|kinesiolog|rehabilit))\w*"
    r"|\b(autoclave|esteriliza(dor|cion)|monitor(es)?\s+(de\s+)?(signos|multiparam|fetal|cardiac|paciente)|electrocardiograf|desfibrilador|\bdea\b|ecograf|ecotomograf"
    r"|ventilador(es)?\s+(mecanic|invasiv|no\s+invasiv)|incubadora|centrifuga|microscopi|oftalm|lampara\s+de\s+hendidura|biomicroscop|tonometr|refractometr|retinograf"
    r"|esteril\w*|steri\s*vac|laser\s+(urolog|oftalm|quirurg|medic)|campimetr|tomograf|rayos\s*x|\brx\b|mamograf|bombas?\s+de\s+infusion|camas?\s+clinic|sillon(es)?\s+dental|unidad(es)?\s+dental|equipo\s+dental|electrobisturi"
    r"|laparoscop|endoscop|cistoscop|colonoscop|anestesi|oximetr|esfigmo|holter|espirometr|audiometr|analizador(es)?\s+(\w+\s+){0,2}(hematolog|bioquim|gases|orina|inmuno)"
    r"|biomedic|facoemulsific|vitrectom|laser\s+(oftalm|yag|excimer)|negatoscop|doppler|electroencefalograf|cardiotocograf|monitor\s+fetal|termociclador|cabina\s+de\s+bioseguridad"
    r"|camara\s+hiperbaric|equipo\s+de\s+rayos|resonador|scanner\s+medic|angiograf|arco\s+en\s+c|torre\s+de\s+(video)?\s*(laparoscop|endoscop))\w*"
)
OFT_CTX = r"oftalm|retin|macul|tomograf|coherencia\s+optica|nervio\s+optico|glaucom|angio|segmento\s+anterior|cornea"
GUANTE_MEDICO = (r"\b(nitrilo|latex|vinilo|examen|examinacion|procedimiento|quirurg|esteril|desechable|clinic|medic|polvo"
                 r"|poliisopreno|isopreno|caja\s+(de\s+|x\s*)?100|100\s+(unidades|und|un)\b|ambidiestr)\w*")
DETERGENTE_CLINICO = (r"\b(enzimat|multienzim|pentaenzim|trienzim|tetraenzim|bienzim|instrumental|quirurg|clinic|hospital"
                      r"|laboratorio|esteriliz|endoscop|termodesinf|alcalin|extran|citometr|ultrasonic)\w*")


def _t(nombre, regex, contexto=None, origen="cliente", activa=True):
    return {"nombre": nombre, "regex": regex, "contexto": contexto, "origen": origen, "activa": activa}


def _e(nombre, regex):
    return {"nombre": nombre, "regex": regex, "origen": "sugerido", "activa": True}


def _o(codigo, fuerza, nota=""):
    return {"codigo": codigo, "fuerza": fuerza, "nota": nota, "activa": True}


_MICROSCOPIOS = ("41111701", "41111702", "41111703", "41111704", "41111705", "41111706", "41111709", "41111710",
                 "41111712", "41111719", "41111720", "41111721", "41111722", "41111723", "41111724", "41111726",
                 "41111727", "41111728", "41111729", "41111731", "41111733", "41111734", "41111735", "41111736")

CATEGORIAS = [
    {
        "codigo": "SRV-MAN", "linea": "Servicio técnico", "nombre": "Mantención de equipos médicos",
        "prioridad": 1, "onu_solo_a_revision": False, "activa": True,
        "terminos": [
            _t("Preventiva", r"\bpreventiv[oa]s?\b", OBJ_MEDICO),
            _t("Correctiva", r"\bcorrectiv[oa]s?\b", OBJ_MEDICO),
            _t("Reparación", r"\breparacion\w*|\breparar\b", OBJ_MEDICO, origen="sugerido"),
            _t("Servicio técnico", r"\bservicio\s+tecnico\b", OBJ_MEDICO, origen="sugerido"),
            _t("Mantención", r"\bmant[ei]n(c|s)ion\w*|\bmantenimiento\w*|\bmanutencion\w*", OBJ_MEDICO),
            # Inactivo: "calibración" se usa como adjetivo de producto ("regla de calibración").
            _t("Calibración", r"\bcalibracion\w*|\bcalibrar\b", OBJ_MEDICO, origen="sugerido", activa=False),
        ],
        "excluye": [
            _e("mantención clínica", r"\bmant\w+\s+de\s+(la\s+)?(via\s+aerea|temperatura|permeabilidad|hidratacion|anestesia|presion|glicemia|cadena\s+de\s+frio)"),
            _e("reparación quirúrgica", r"\breparacion\s+(\w+\s+){0,2}(menisc|valvul|tricusp|mitral|aortic|hernia|tendon|ligament|piel|tejido|herida|vascular|osea|cartilag|nervio|perine|manguito|rotador)\w*|\bkits?\s+(de\s+)?reparacion"),
            _e("vehículo/inmueble", r"\b(vehiculo|camioneta|ambulancia|automovil|bus|motor\s+diesel|ascensor|aire\s+acondicionado|caldera|extintor|grupo\s+electrogeno|generador|techumbre|edificio|jardin|areas\s+verdes|piscina|impresora|computador|fotocopiadora)\w*"),
        ],
        "onu": [
            _o("7315210", "confirma", "Servicio de mantenimiento / reparación de equipo (de fabricación)"),
            _o("81111812", "confirma", "Mantenimiento y soporte de hardware"),
            _o("42295001", "confirma", "Unidades de mantenimiento o accesorios de endoscopios"),
            _o("42", "confirma", "Segmento 42: equipo médico"),
            _o("411", "confirma", "Equipo de laboratorio"),
        ],
    },
    {
        "codigo": "DEV-OFT", "linea": "Device", "nombre": "Oftalmología",
        "prioridad": 2, "onu_solo_a_revision": True, "activa": True,
        "terminos": [
            _t("Lentes intraoculares", r"\blentes?\s+intra\s*-?\s*ocular\w*|\bintra\s*-?\s*ocular\w*\s+lens|\blio\b"),
            _t("Campímetro", r"\bcampimetr\w*|\bperimetr\w*\s+(computariz|automatiz|visual|humphrey|octopus)\w*"),
            _t("OCT", r"\boct\b", OFT_CTX),
            _t("OCT (tomografía de coherencia óptica)", r"coherencia\s+optica"),
            _t("Instrumental oftalmológico", r"\binstrumental\s+(\w+\s+){0,3}oftalm\w*"),
            # Sugeridos para validar con el cliente (inactivos).
            _t("Facoemulsificación", r"\bfacoemulsific\w*|\bfaco\b", origen="sugerido", activa=False),
            _t("Vitrectomía", r"\bvitrectom\w*|\bvitreo\w*", origen="sugerido", activa=False),
            _t("Lámpara de hendidura", r"\blamparas?\s+de\s+hendidura\b|\bbiomicroscop\w*", origen="sugerido", activa=False),
            _t("Tonómetro", r"\btonometr\w*", origen="sugerido", activa=False),
            _t("Autorrefractómetro", r"\bauto\s*-?\s*refractometr\w*|\bqueratometr\w*", origen="sugerido", activa=False),
            _t("Biómetro", r"\bbiometr\w*\s+(optic|ocular)\w*|\biol\s*master\b", origen="sugerido", activa=False),
            _t("Retinógrafo", r"\bretinograf\w*|\bcamaras?\s+(retinal|no\s+midriatic)\w*", origen="sugerido", activa=False),
            _t("Viscoelástico", r"\bviscoelastic\w*", origen="sugerido", activa=False),
            _t("Topógrafo corneal", r"\btopograf\w*\s+cornea\w*", origen="sugerido", activa=False),
            _t("Oftalmología", r"\boftalm\w*"),
        ],
        "excluye": [
            _e("veterinario", r"\bveterinari\w*|\bmascota\w*|\bcanin\w*|\bfelin\w*"),
            _e("multimedia", r"\b(lumenes|telon|hdmi|epson|xga|multimedia|data\s*show|pantalla\s+(de\s+)?proyecc)\w*"),
        ],
        "onu": [
            _o("422945", "fuerte", "Cirugía oftálmica"),
            _o("421830", "fuerte", "Examen oftálmico"),
            _o("42201718", "fuerte", "Ecógrafos oftalmológicos"),
            _o("42295114", "fuerte", "Equipo de facoemulsificación"),
            _o("42295126", "fuerte", "Fragmatome retinal vítreo"),
            _o("42295505", "fuerte", "Implantes oftálmicos"),
            _o("42293504", "fuerte", "Aspiración o irrigación oftálmica"),
            _o("85121610", "fuerte", "Servicios oftalmológicos"),
            _o("42182005", "confirma", "Oftalmoscopios u otoscopios (mixto)"),
            _o("42182014", "confirma", "Accesorios de otoscopia u oftalmoscopia"),
            _o("31241501", "confirma", "Lentes (genérico)"),
        ],
    },
    {
        "codigo": "DEV-MIC", "linea": "Device", "nombre": "Microscopía",
        "prioridad": 3, "onu_solo_a_revision": False, "activa": True,
        "terminos": [_t("Microscopio", r"\bmicroscopios?\b|\bmicrospcopio\w*")],
        "excluye": [
            _e("juguete", r"\bjuguete\w*|\bdidactic\w*"),
            _e("insumo de microscopía", r"\b(porta\s*objeto|cubre\s*objeto|laminilla|aceite\s+de\s+inmersion|azul\s+de\s+metileno|tincion|colorante)\w*"),
        ],
        "onu": [_o(c, "fuerte", "Microscopios") for c in _MICROSCOPIOS]
               + [_o("42295121", "fuerte", "Microscopios quirúrgicos"),
                  _o("41122603", "confirma", "Papel para lentes de microscopio"),
                  _o("41122605", "confirma", "Aceite de inmersión")],
    },
    {
        "codigo": "DEV-GUA", "linea": "Device", "nombre": "Guantes médicos",
        "prioridad": 4, "onu_solo_a_revision": False, "activa": True,
        "terminos": [_t("Guante", r"\bguantes?\b", GUANTE_MEDICO)],
        "excluye": [
            _e("guante no médico", r"\b(cabritilla|cabretilla|cuero|carnaza|forro|forrado|anticorte|multiflex|multiuso|multiproposito|mecanic|soldad|motorist|moto\b|arquer|portero|boxeo|futbol|jardin|alta\s+temperatura|termic|dielectric|electricist|aseo|domestic|hilo|lana|tejid|poliester|nylon|activex|invierno|polar|industrial|pvc|cocina|horno|ciclis|bicicleta|golf|equitacion|esqui|buceo|bombero|antivibra|construccion|agricol|pesca|box|seguridad|quimic|alta\s+resistencia|amarillo)\w*"),
        ],
        "onu": [
            _o("42132201", "fuerte", "Cajas o dispensadores de guantes médicos"),
            _o("42132203", "fuerte", "Guantes médicos de examen"),
            _o("42132204", "fuerte", "Cubre guantes médicos"),
            _o("42132205", "fuerte", "Guantes quirúrgicos"),
            _o("42204005", "fuerte", "Guantes de resguardo radiológico"),
            _o("42295458", "confirma", "Secado o empolvado de guantes quirúrgicos"),
            _o("46181504", "confirma", "Guantes protectores (EPP)"),
        ],
    },
    {
        "codigo": "DEV-DET", "linea": "Device", "nombre": "Detergentes clínicos",
        "prioridad": 5, "onu_solo_a_revision": False, "activa": True,
        "terminos": [_t("Detergente", r"\bdetergentes?\b", DETERGENTE_CLINICO)],
        "excluye": [
            _e("detergente doméstico", r"\b(ropa|lavaloza|loza|lavavajilla|platos|lavadora|prendas|lavanderia|vehiculo|auto\b|resistente\s+a\s+(los\s+)?detergente|zapat|piso|vajilla|utensilio|alfombra|tapiz)\w*"),
        ],
        # Nunca 12352204 'Enzimas': es un código comodín (3.078 descartes en 30 días).
        "onu": [
            _o("42281704", "fuerte", "Detergentes o limpiadores de instrumentos"),
            _o("41103206", "fuerte", "Detergentes de laboratorio"),
            _o("42152450", "fuerte", "Limpiadores de instrumental odontológico"),
            _o("42312305", "confirma", "Productos enzimáticos de desbridamiento"),
            _o("12161902", "confirma", "Detergentes surfactantes"),
            _o("47131", "confirma", "Productos de limpieza"),
        ],
    },
    {
        "codigo": "DEV-APO", "linea": "Device", "nombre": "Apósitos",
        "prioridad": 6, "onu_solo_a_revision": False, "activa": True,
        "terminos": [_t("Apósito", r"\bapositos?\b")],
        "excluye": [],
        "onu": [
            _o("42311532", "fuerte", "Apósitos secos"),
            _o("42311526", "fuerte", "Vendajes de apósitos"),
            _o("42311506", "fuerte", "Vendas o apósitos compresores"),
            _o("42311531", "fuerte", "Coberturas para apósitos"),
            _o("42295406", "fuerte", "Apósitos de gasa quirúrgicos"),
            _o("4231", "confirma", "Cuidado de heridas"),
        ],
    },
]
