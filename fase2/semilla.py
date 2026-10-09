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

VERSION_SEMILLA = "v1.3"

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
    # v1.3 — faltantes encontrados por la auditoría con IA (lote 20261008-2154):
    r"|\b(anatomia\s+patolog|microtomo|criostato|procesador\w*\s+de\s+tejidos|estacion\w*\s+de\s+macroscop|armario\w*\s+de\s+(extraccion|seguridad|bioseguridad)"
    r"|campana\w*\s+(de\s+)?(extraccion|flujo|bioseguridad)|flujo\s+laminar|ventilador\w*\s+(de\s+)?(transporte|neonatal|pediatric|adulto|volumetric)"
    r"|monnal|drager|draeger|humidificador|fisher\s*&?\s*paykel|ultrason|gastroscop|broncoscop|duodenoscop|videoendoscop|ureteroscop|histeroscop|artroscop"
    r"|opticas?\s+(laparoscop|rigida|de\s+\d)|mesas?\s+(quirurg|de\s+operacion|ginecolog|de\s+examen|de\s+procedimiento|de\s+parto)"
    r"|camas?\s+(clinic|electric|hospital|de\s+parto|uci|uti)|camillas?|catres?\s+clinic|sillon\w*\s+(clinic|de\s+parto|oftalm|reclinable)|sillas?\s+de\s+ruedas"
    r"|capnograf|aferesis|electromiograf|polisomnograf|potenciales\s+evocados|cadwell|bombas?\s+de\s+(aspiracion|succion|jeringa|alimentacion)"
    r"|aspirador\w*\s+(quirurg|de\s+secreciones|clinic)|\btac\b|tomografo|resonancia\s+magnetic|rayos?\s+dental|radiograf|densitometr|fluoroscop"
    r"|(equipamiento|unidad|sillon|box|clinica|consulta)\w*\s+(\w+\s+){0,2}(dental|odontolog)|micromotor|turbina\s+dental"
    r"|equipos?\s+(\w+\s+){0,2}(critic|kinesiolog|rehabilit|fisioterap|electroterap|terapia)|equipamiento\s+(\w+\s+){0,2}(medic|clinic|biomedic|hospital|kinesiolog|salud)"
    r"|terapia\s+combinada|electroestimul|magnetoterap|lavadora\w*\s+(\w+\s+){0,2}(desinfect|descontamin|instrumental|ultrason|endoscop)|termodesinfect|desinfectadora"
    r"|osmosis\s+inversa\s+(\w+\s+){0,3}(dialisis|esteriliz|laboratorio)|refrigerador\w*\s+(\w+\s+){0,2}(clinic|vacuna|farmac|medic|laboratorio|biologic)"
    r"|ultra\s*-?\s*(freezer|congelador)|freezer\w*\s+(clinic|laboratorio|-\s*\d)|congelador\w*\s+(\w+\s+){0,2}(clinic|laboratorio|vacuna|-\s*\d)"
    r"|estufa\w*\s+(de\s+)?(cultivo|secado|laboratorio)|bano\s+maria|monitor\w*\s+(\w+\s+){0,1}(signos|multiparam|fetal|cardiac|paciente|desfibril|hemodinam|anestes|clinic|medic)"
    r"|detector\w*\s+de\s+radiacion|dosimetr|activimetr|lampara\w*\s+(\w+\s+){0,2}(quirurg|cialitic|scialitic|de\s+examen|de\s+fotocurado|dental|de\s+procedimiento)|fototerapia)\w*"
)
OFT_CTX = r"oftalm|retin|macul|tomograf|coherencia\s+optica|nervio\s+optico|glaucom|angio|segmento\s+anterior|cornea"
GUANTE_MEDICO = (r"\b(nitrilo|latex|vinilo|examen|examinacion|procedimiento|quirurg|esteril|desechable|clinic|medic|polvo"
                 r"|poliisopreno|isopreno|caja\s+(de\s+|x\s*)?100|100\s+(unidades|und|un)\b|ambidiestr"
                 r"|alto\s+riesgo|hipoalergenic|microtexturad|riesgo\w*\s+biologic)\w*")
DETERGENTE_CLINICO = (r"\b(enzimat|multienzim|pentaenzim|trienzim|tetraenzim|bienzim|instrumental|quirurg|clinic|hospital"
                      r"|laboratorio|esteriliz|endoscop|termodesinf|alcalin|extran|citometr|ultrasonic"
                      r"|secuenciador|merck|polivalente)\w*")  # "neutro"/"reactivo" medidos: metían detergentes de gimnasio y reactivos


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
        # Medido (auditoría IA): bajo un título "insumos oftalmológicos", 39 de 80 líneas lo eran
        # aunque el código ONU fuera genérico → revisión.
        "titulo_solo_a_revision": True,
        "terminos": [
            _t("Lentes intraoculares", r"\blentes?\s+intra\s*-?\s*ocular\w*|\bintra\s*-?\s*ocular\w*\s+lens|\blio\b"),
            _t("Campímetro", r"\bcampimetr\w*|\bperimetr\w*\s+(computariz|automatiz|visual|humphrey|octopus)\w*"),
            _t("OCT", r"\boct\b", OFT_CTX),
            _t("OCT (tomografía de coherencia óptica)", r"coherencia\s+optica"),
            _t("Instrumental oftalmológico", r"\binstrumental\s+(\w+\s+){0,3}oftalm\w*"),
            # Sugeridos para validar con el cliente (inactivos).
            # Sugeridos encendidos en v1.3: la auditoría con IA encontró estos productos fuera.
            _t("Facoemulsificación", r"\bfacoemulsific\w*|\bfaco\b", origen="sugerido"),
            _t("Vitrectomía", r"\bvitrect\w*|\bvitreo\w*", origen="sugerido"),  # vitrectomía y vitrector
            _t("Lámpara de hendidura", r"\blamparas?\s+de\s+hendidura\b|\bbiomicroscop\w*", origen="sugerido"),
            _t("Tonómetro", r"\btonometr\w*", origen="sugerido"),
            _t("Autorrefractómetro", r"\bauto\s*-?\s*refractometr\w*|\bqueratometr\w*", origen="sugerido"),
            _t("Biómetro", r"\bbiometr\w*\s+(optic|ocular)\w*|\biol\s*master\b", origen="sugerido"),
            _t("Retinógrafo", r"\bretinograf\w*|\bcamaras?\s+(retinal|no\s+midriatic)\w*", origen="sugerido"),
            _t("Viscoelástico", r"\bviscoelastic\w*", r"oftalm|ocular|faco|catarat|camara\s+anterior|hialuron|metilcelulos|cohesiv|dispersiv",
               origen="sugerido"),  # sin contexto calzaba "cojín viscoelástico"
            _t("Topógrafo corneal", r"\btopograf\w*\s+cornea\w*", origen="sugerido"),
            # v1.3: vocabulario de cirugía y diagnóstico ocular que la auditoría encontró fuera.
            _t("Instrumental de cirugía ocular", r"\bblefarostat\w*|\bcuchillete\w*|\bparacentesis\b|\bcapsul(orrexis|otomia)\w*"
               r"|\banillo\w*\s+(de\s+tension\s+)?(endo)?capsular\w*|\bdepresor\w*\s+escleral\w*", origen="sugerido"),
            _t("Implante o prótesis ocular", r"\bimplante\w*\s+(\w+\s+){0,2}(ocular|orbitari|palpebral|glaucoma|de\s+paul|ahmed)\w*"
               r"|\besfera\w*\s+ocular\w*|\bbola\w*\s+de\s+enucleacion|\bconformador\w*\s+ocular\w*|\bprotesis\s+ocular\w*|\bexplante\w*", origen="sugerido"),
            _t("Vía lagrimal", r"\bcanula\w*\s+(\w+\s+){0,2}lagrimal|\bvia\s+lagrimal|\bsonda\w*\s+bicanalicular|\bintubacion\s+lagrimal", origen="sugerido"),
            _t("Diagnóstico ocular", r"\boptotipo\w*|\bsnellen\b|\bparches?\s+oculares?\b|\blentes?\s+de\s+contacto\s+terapeutic\w*"
               r"|\b(cinta|tira|papel)\w*\s+(de\s+)?fluoresce\w*", origen="sugerido"),
            _t("Intraocular", r"\bintra\s*-?\s*ocular\w*", origen="sugerido"),
            _t("Oftalmología", r"\boftalm\w*|\boftal\b"),  # "(OFTAL.)" abreviado
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
        "terminos": [_t("Microscopio", r"\b\w*microscopios?\b|\bmicrospcopio\w*"),  # incluye estereomicroscopio
                     _t("Lupa estereoscópica", r"\blupas?\s+(\w+\s+){0,1}(binocular|estereoscopic)\w*", origen="sugerido")],
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
        "terminos": [_t("Guante", r"\bguan{0,2}tes?\b", GUANTE_MEDICO)],  # guante(s), typos "guates" y "guanntes"
        "excluye": [
            _e("guante no médico", r"\b(cabritilla|cabretilla|cuero|carnaza|forro|forrado|anticorte|multiflex|multiuso|multiproposito|mecanic|soldad|motorist|moto\b|arquer|portero|boxeo|futbol|jardin|alta\s+temperatura|termic|dielectric|electricist|aseo|domestic|hilo|lana|tejid|poliester|nylon|activex|invierno|polar|pvc|cocina|horno|ciclis|bicicleta|golf|equitacion|esqui|buceo|bombero|antivibra|construccion|agricol|pesca|de\s+box\b|onzas?\b|onz\b|amarillo)\w*"),
            # v1.3: separada de la anterior. "Resistente a químicos" o "alta resistencia" también lo dice un
            # guante quirúrgico ("sin acelerador químico"); sólo excluye si el código ONU NO es de guante médico.
            {**_e("guante industrial", r"\b(quimic|alta\s+resistencia|industrial|seguridad)\w*"), "salvo_onu_fuerte": True},
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
        "terminos": [_t("Detergente", r"\bdetergentes?\b", DETERGENTE_CLINICO),
                     # v1.3 (auditoría IA): productos de lavado de instrumental que no dicen "detergente".
                     _t("Enzimático", r"\b(espuma|spray|jabon|solucion|limpiador)\w*\s+(\w+\s+){0,2}(multi|tri|tetra|cuatri|penta|bi|poli)?\s*-?\s*enzimatic\w*"
                        r"|\b(multi|tri|tetra|cuatri|penta)\s*-?\s*enzimatic\w*|\bneodisher\b|\bdeconex\b|\bendozi[mn]e?\b", origen="sugerido"),
                     _t("Limpiador de instrumental", r"\b(limpiador|solucion\w*\s+limpiadora|removedor|desincrustante)\w*\s+(\w+\s+){0,4}"
                        r"(instrumental|instrumentos?\s+quirurg|electrobisturi|endoscop)\w*", origen="sugerido")],
        "excluye": [
            # v1.3: sin "lavadora" (bloqueaba detergentes para lavadoras de instrumental; la ropa la excluye "ropa").
            _e("detergente doméstico", r"\b(ropa|lavaloza|loza|lavavajilla|platos|prendas|lavanderia|vehiculo|auto\b|resistente\s+a\s+(los\s+)?detergente|zapat|piso|vajilla|utensilio|alfombra|tapiz)\w*"),
        ],
        # Nunca 12352204 'Enzimas': es un código comodín (3.078 descartes en 30 días).
        "onu": [
            _o("42281704", "fuerte", "Detergentes o limpiadores de instrumentos"),
            _o("41103206", "fuerte", "Detergentes de laboratorio"),
            _o("42152450", "fuerte", "Limpiadores de instrumental odontológico"),
            _o("42312305", "confirma", "Productos enzimáticos de desbridamiento"),
            _o("12161902", "confirma", "Detergentes surfactantes"),
        ],
    },
    {
        "codigo": "DEV-APO", "linea": "Device", "nombre": "Apósitos",
        "prioridad": 6, "onu_solo_a_revision": False, "activa": True,
        "terminos": [_t("Apósito", r"\bapositos?\b"),
                     # v1.3 (auditoría IA): curitas y marcas de apósitos.
                     _t("Curita o marca de apósito", r"\bcuritas?\b|\bmepi(tel|lex)\b|\btegaderm\b|\bduoderm\b|\baquacel\b|\ballevyn\b"
                        r"|\bcomfeel\b|\bbiatain\b|\bhydrocoll\b", origen="sugerido")],
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
