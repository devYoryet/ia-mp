# 06 · Fase 2: Device y Servicio técnico (diseño, 2026-10-08)

> Estado (2026-10-08): **E1 y E2 en producción.** Código en la rama `fase2-device`,
> carpeta [`fase2/`](../fase2/README.md); container `ia-mp-fase2` en gestor_oc
> (`/opt/ia-mp-fase2`). Tablas `clasificador_f2_*` creadas, semilla v1.1 cargada
> (huella `c304a41e56c8e525`).
>
> - **Candados:** `admin.py verificar` corrido desde el servidor rechazó 11 de 11 escrituras ajenas.
> - **Carga inicial (60 días):** 619.332 filas en 181 s; 4.326 en verde (≈72/día) y 1.935 a
>   revisión (≈32/día). 108 venían de "IA dijo interés y una persona eliminó después";
>   409, de `Bot Eliminado`.
> - **Producción intacta:** misma imagen, worker y panel sin reinicio, md5 idénticos; worker
>   a ritmo normal durante la carga.
>
> - **E3 (panel de revisión):** container `ia-mp-fase2-panel`, http://10.0.0.70:8810 (red interna /
>   VPN), con el login del equipo. Vistas Revisión / Verde / Revisadas / Anuladas, Excel y Resumen
>   (precisión medida y salud del barrido). Publicarlo en el dominio requiere una location en nginx
>   (no se hizo).
>
> Los números salen de datos reales medidos a $0, sin llamar a la API. Donde dice
> *inferido* es una estimación sobre muestras revisadas a mano, no una medición.

## Para qué

Megalabs pidió ver, además de farma, **insumos médicos (Device)** y **servicios
técnicos**, con esta lista: Oftalmología / Oftalmológico / Oftalmológica,
Microscopio, Lentes Intraoculares, Campímetro, OCT, Instrumental Oftalmológico,
Mantención, Preventiva, Correctiva, Guante, Detergente, Apósito.

Hoy todo eso **se descarta a propósito**, porque la fase 1 clasifica farma. La
fase 2 toma lo descartado y lo pasa por un segundo filtro con dos señales:

| palabra en la glosa | nombre del código ONU | resultado |
|:-:|:-:|---|
| ✓ | ✓ | **verde**: clasificado (Device o Servicio técnico) |
| ✓ | ✗ | **revisión**: una persona aprueba o no aprueba |
| ✗ | ✓ | **revisión**, sólo en las categorías donde el ONU es confiable (ver abajo) |
| ✗ | ✗ | sigue descartado |

## Reglas del diseño

1. **La fase 1 no se toca.** La fase 2 no modifica `cascada.py`, `worker.py`,
   `escritor.py` ni `detector.py`, y nunca escribe `pactivo`, `estado_gestor` ni
   `nombre_clasificador` en las tablas origen. Escribe sólo en sus propias tablas.
   Si la fase 2 falla o se apaga, farma sigue igual.
2. **Todo término, exclusión y código ONU es una fila con nombre** en una tabla.
   Agregar o quitar uno no requiere deploy. Es el mismo criterio de los vetos.
3. **$0 de API.** Son regex y búsquedas en memoria. La API no se usa en la fase 2.
4. **Se mide antes de activar.** `backtest_fase2.py` re-corre la fase 2 sobre N
   días de descartes. Cuando haya revisiones humanas, mide la precisión por
   categoría y por señal.

## Qué entra a la fase 2: todo lo que hoy está descartado, mirando hacia atrás

**Estado efectivo** de una fila = `estado_gestor` si una persona (o un bot del
legacy) ya decidió. Si no, lo que sugirió la IA (`clasificador_ia_log.interes_sugerido`).
Entra toda fila cuyo estado efectivo es **descarte**, por cualquiera de estas vías:

| vía | volumen medido (30 d) | entran a fase 2 |
|---|---:|---:|
| la IA la descartó (aprobado o no por el equipo) | 268.300 | ~2.680 |
| la IA dijo interés y **después** una persona la eliminó | 2.405 | 51 |
| la eliminó el legacy **antes** de que pasara el worker (`Bot Eliminado`), así que **no está en el log** | 8.100 | 197 (casi todas mantenciones) |

- La tercera vía obliga a **barrer las tablas origen**, no sólo `clasificador_ia_log`.
- Si una fila descartada pasa después a interés farma (rescate humano), su
  registro de fase 2 se **anula**. No se entrega dos veces, y farma manda.
- Una decisión humana de fase 2 (aprobado / no aprobado) **nunca se pisa** con
  una re-evaluación automática.

## Las dos señales

### Señal 1: palabra (glosa)

- Se evalúa sobre `Descripcion` normalizada: sin tildes, minúsculas y con espacios
  colapsados. Cada término cubre singular y plural, como en la lección de los vetos.
- Un término puede exigir **contexto**, que se busca en la glosa **o** en el título.
  Ejemplo: "mantención" cuenta sólo si se habla de un equipo médico.
- Un término sin su contexto es **débil**: sólo manda a revisión si el ONU es *fuerte*.
- Las **exclusiones** anulan la categoría completa. Ejemplos: guante de cabritilla,
  detergente de ropa, reparación meniscal.

Palabras sueltas que **no** sirven tal cual (medido):

| palabra | sola, 30 d | por qué no sirve |
|---|---:|---|
| OCT | 56 | 100 % ruido: es "octubre". Se exige contexto oftalmológico o "coherencia óptica" |
| mantención / mantenimiento | 4.666 (155/día) | extintores, vehículos, aire acondicionado. Se exige objeto médico: queda en ~20/día |
| preventiva / correctiva | 1.241 / 724 | mismo caso; se exige objeto médico |
| reparación | — | choca con cirugía ("reparación meniscal / valvular / hernia") |
| calibración | — | se usa como adjetivo de producto ("regla de calibración"); queda inactiva |
| guante | 2.302 | la mitad es de trabajo (cabritilla, anticorte, multiflex); se exige calificativo médico |
| detergente | 331 | ropa, loza, pisos; se exige contexto clínico (enzimático, instrumental, ...) |

### Señal 2: nombre del código ONU (UNSPSC)

- **Fuente:** en `Licitaciones_diarias`, `Producto_Servicio` es el nombre UNSPSC del
  portal. Da un nombre por código en 12.946 de 12.948 códigos y cubre el
  **99,4–100 %** de los descartes de las tres fuentes:
  - compra ágil: código en `Item`;
  - licitaciones: código en `Cod_Onu`;
  - cotizaciones: código en `Item`.
- **Ojo:** en `compra_agil`, `Producto_Servicio` NO es el nombre ONU.
- Cada categoría lista sus códigos (exactos o por prefijo) con una **fuerza**:
  - `fuerte`: el código por sí solo identifica la categoría. Ejemplo: `42132205` Guantes quirúrgicos.
  - `confirma`: es coherente con la categoría pero no específico. Sólo confirma
    una palabra. Ejemplo: `73152101` Servicio de mantenimiento de equipo de fabricación.

**El ONU viene mal puesto en compra ágil con frecuencia** ("GUANTES NITRILO" con
código *Lidocaína*, "Cofias" con *Guantes quirúrgicos*). Por eso "sólo ONU → revisión"
está activo **sólo en Oftalmología**: sus códigos (`422945xx` cirugía oftálmica,
`421830xx` examen oftálmico) son específicos y aciertan ~87 % (inferido, muestra
de 15). En guantes, apósitos, microscopios y detergentes, el ONU solo es casi
puro ruido (muestras de 9 cada una).

Trampas medidas:

- `12352204 Enzimas` es un código comodín (3.078 descartes en 30 d). No se usa nunca.
- No existe un código limpio para la mantención de equipos médicos. Se carga como
  `73152101/2` (equipo "de fabricación"), así que la señal médica tiene que venir
  de la glosa o del título.

### Decisión por categoría

```
T  = algún término calza en la glosa, con su contexto (glosa o título), y no hay exclusión
Td = término calza pero falta su contexto (débil)
Of = código ONU fuerte de la categoría · Oc = fuerte o confirma
verde     si T y Oc                                   → señal "ambas"
revisión  si T y no Oc                                → "solo_palabra"
revisión  si Td y Of                                  → "palabra_debil+onu"
revisión  si Of, sin T ni Td, y onu_solo_a_revision   → "solo_onu"
nada      en otro caso
```

Si calzan varias categorías, gana la primera en **verde** por prioridad; si
ninguna está en verde, la primera en revisión. Las demás quedan anotadas como
`otras_categorias`. Mantención va primero: "mantención de microscopio" es un
servicio, no la compra de un microscopio.

## Categorías v1.1 y volumen medido (30 d, $0)

| código | línea | categoría | términos del cliente (+ variantes) | verde/día | revisión/día | precisión del verde (inferida) |
|---|---|---|---|---:|---:|---|
| `SRV-MAN` | Servicio técnico | Mantención de equipos médicos | Mantención, Preventiva, Correctiva (+ sugeridos: reparación, servicio técnico) **con objeto médico** | 17,4 | 2,6 | 27/30 en v1 |
| `DEV-OFT` | Device | Oftalmología | Lentes intraoculares (LIO), Campímetro, OCT, Instrumental oftalmológico, Oftalmología/o/a | 3,3 | 15,6 (13,5 sólo ONU) | 12/12 |
| `DEV-MIC` | Device | Microscopía | Microscopio | 1,3 | 0,8 | 12/12 (incluye escolares) |
| `DEV-GUA` | Device | Guantes médicos | Guante, **con calificativo médico** | 35,4 | 10,0 | 30/30 |
| `DEV-DET` | Device | Detergentes clínicos | Detergente, **con contexto clínico** | 1,8 | 2,3 | ~11/12 |
| `DEV-APO` | Device | Apósitos | Apósito | 0,1 | 0,4 | 2/2 |
| | | **total** | | **≈ 59** | **≈ 32** | |

- **Apósitos casi no aparecen**: la fase 1 ya los clasifica como farma (2.432
  filas en 30 d, sólo 402 descartadas).
- **Guantes son el 60 % del verde.** Esto pasa porque `Guante` salió del catálogo
  activo (no tiene cliente), así que hoy todo guante médico se descarta.
- La revisión de guantes "sólo palabra" (9,3/día) es en su mayoría guante médico
  con el ONU mal puesto (inferido ~85 % aprobables). Si las revisiones lo confirman
  con >95 %, se propone promover "guante + calificativo fuerte" a verde. Esa
  promoción se decide con datos, no ahora.
- La subcategoría es el término que calzó: preventiva / correctiva, LIO, OCT, etc.

Errores conocidos de v1.1 (a afinar en E1):

- `esteril*` como objeto médico deja pasar "cánula ... estéril" que dice "mantención" en otro sentido.
- "multímetro para mantenimiento biomédico" es un producto, no un servicio.
- La exclusión `lavadora` en detergentes bloquea "lavadora descontaminadora".

## Arquitectura

```
FASE 1 (sin cambios)                          FASE 2 (nueva, aparte)
worker → cascada → log + tabla origen         servicio `fase2` (misma imagen, otro proceso)
                                                cada 10 min: últimos 3 días por id (PK)
                                                diario:      últimos 60 días + cambios de reglas
                                                lee: tablas origen + log (sólo lectura)
                                                escribe: clasificador_f2_resultado
panel /device  ← revisión aprobar / no aprobar, filtros, Excel
panel /device/reglas ← categorías, términos, exclusiones, ONU (con "probar en 30 días")
```

Tablas nuevas, en el clásico (`licitaciones_diarias_total_farma`):

| tabla | contenido |
|---|---|
| `clasificador_f2_categorias` | código, línea, nombre, prioridad, `onu_solo_a_revision`, activa |
| `clasificador_f2_terminos` | categoría, tipo `incluye`/`excluye`, nombre, regex, `contexto_regex`, origen `cliente`/`sugerido`, activa, autor, fecha |
| `clasificador_f2_onu` | categoría, código o prefijo, fuerza `fuerte`/`confirma`, activa |
| `clasificador_f2_onu_nombre` | código → nombre UNSPSC (de `Licitaciones_diarias.Producto_Servicio`, se refresca a diario) |
| `clasificador_f2_resultado` | una fila por (`tabla_origen`, `fila_id`): licitación, categoría, subcategoría, estado `verde`/`revision`/`aprobado`/`rechazado`/`anulado`, señal, términos, código + nombre ONU, `otras_categorias`, vía de descarte (método fase 1 / `Bot Eliminado` / humano), versión de reglas, revisor, fecha, motivo |

Barrido idempotente:

- inserta lo nuevo;
- actualiza lo no revisado si cambian las reglas;
- marca `anulado` si la fila pasó a interés farma o dejó de calzar;
- no toca lo revisado.

Lee por rango de `id` (PK). Carga estimada: ~35 mil filas cada 10 min y ~700 mil
una vez al día, en bloques.

## Etapas de ejecución

| etapa | qué | quién lo hace | entregable / verificación |
|---|---|---|---|
| **E0** | medición y propuesta de categorías | Opus (hecho) | este documento |
| **E1** | motor puro `fase2.py` + esquema + semilla v1.1 + tests + `backtest_fase2.py` | **Opus**: la semántica (estado efectivo, idempotencia, regex) es donde un error cuesta | tests verdes; backtest de 30/60 días con volúmenes por categoría y señal |
| **E2** | servicio `fase2` en docker-compose + backfill de 60 días | **Opus**: toca el deploy de producción (cero drift) | tabla poblada; `git diff` sin cambios en cascada/worker/escritor/detector; worker farma sigue al día |
| **E3** | panel `/device` (revisión, verde, Excel) + `/device/reglas` (CRUD + probar) | **Sonnet**, con esta especificación y los patrones de `api/main.py`; revisa Opus | prueba en el navegador; aprobar / no aprobar persiste |
| **E4** | operación 2 semanas: revisión diaria y reporte semanal de precisión por categoría y señal | equipo + Opus/Sonnet para el reporte | promover o apagar baldes con datos; validar los términos sugeridos con Megalabs |
| **E5** | entrega a Megalabs | por decidir | Excel diario, correo o integración prime (`diccionario_unidad` / correo masivo) |

Hasta E5, **nada se envía automáticamente a ningún cliente**. El demo para
Megalabs (la "simulación de la plataforma" que pidieron) es posible desde E3,
con los 60 días de backfill.

## Pendientes de decisión (negocio)

1. **Alcance de Mantención / Preventiva / Correctiva.** Recomendado: sólo equipos
   médicos, clínicos y de laboratorio (~20/día). La alternativa, cualquier
   mantención, son ~155/día, casi todo ajeno a salud.
2. **Quién revisa la cola Device** (~32/día) y en qué horario.
3. **Términos sugeridos para validar con Megalabs** (entran como `sugerido`, se
   activan sin deploy):
   - Mantención: reparación, servicio técnico, calibración.
   - Oftalmología: facoemulsificación, vitrectomía, lámpara de hendidura,
     tonómetro, autorrefractómetro, biómetro, retinógrafo, viscoelástico, topógrafo corneal.
4. **Cómo se entrega** a Megalabs (E5).

## Apéndice A: semilla v1.1 (prototipo con que se midió)

Regex sobre texto normalizado (sin tildes, minúsculas). `onu`: `(código o prefijo, fuerza)`.

```python
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

CATEGORIAS = [
  dict(codigo="SRV-MAN", linea="Servicio técnico", nombre="Mantención de equipos médicos", prioridad=1, onu_solo_a_revision=False,
       terminos=[
         ("Mantención", r"\bmant[ei]n(c|s)ion\w*|\bmantenimiento\w*|\bmanutencion\w*", OBJ_MEDICO, "cliente"),
         ("Preventiva", r"\bpreventiv[oa]s?\b", OBJ_MEDICO, "cliente"),
         ("Correctiva", r"\bcorrectiv[oa]s?\b", OBJ_MEDICO, "cliente"),
         ("Reparación", r"\breparacion\w*|\breparar\b", OBJ_MEDICO, "sugerido"),
         ("Servicio técnico", r"\bservicio\s+tecnico\b", OBJ_MEDICO, "sugerido"),
       ],
       excluye=[
         ("mantención clínica", r"\bmant\w+\s+de\s+(la\s+)?(via\s+aerea|temperatura|permeabilidad|hidratacion|anestesia|presion|glicemia|cadena\s+de\s+frio)"),
         ("reparación quirúrgica", r"\breparacion\s+(\w+\s+){0,2}(menisc|valvul|tricusp|mitral|aortic|hernia|tendon|ligament|piel|tejido|herida|vascular|osea|cartilag|nervio|perine|manguito|rotador)\w*|\bkits?\s+(de\s+)?reparacion"),
         ("vehículo/inmueble", r"\b(vehiculo|camioneta|ambulancia|automovil|bus|motor\s+diesel|ascensor|aire\s+acondicionado|caldera|extintor|grupo\s+electrogeno|generador|techumbre|edificio|jardin|areas\s+verdes|piscina|impresora|computador|fotocopiadora)\w*"),
       ],
       onu=[("7315210", "confirma"), ("81111812", "confirma"), ("42295001", "confirma"), ("42", "confirma"), ("411", "confirma")]),
  dict(codigo="DEV-OFT", linea="Device", nombre="Oftalmología", prioridad=2, onu_solo_a_revision=True,
       terminos=[
         ("Lentes intraoculares", r"\blentes?\s+intra\s*-?\s*ocular\w*|\bintra\s*-?\s*ocular\w*\s+lens|\blio\b", None, "cliente"),
         ("Campímetro", r"\bcampimetr\w*|\bperimetr\w*\s+(computariz|automatiz|visual|humphrey|octopus)\w*", None, "cliente"),
         ("OCT", r"\boct\b", OFT_CTX, "cliente"),
         ("OCT (tomografía de coherencia óptica)", r"coherencia\s+optica", None, "cliente"),
         ("Instrumental oftalmológico", r"\binstrumental\s+(\w+\s+){0,3}oftalm\w*", None, "cliente"),
         ("Oftalmología", r"\boftalm\w*", None, "cliente"),
       ],
       excluye=[("veterinario", r"\bveterinari\w*|\bmascota\w*|\bcanin\w*|\bfelin\w*"),
                ("multimedia", r"\b(lumenes|telon|hdmi|epson|xga|multimedia|data\s*show|pantalla\s+(de\s+)?proyecc)\w*")],
       onu=[("422945", "fuerte"), ("421830", "fuerte"), ("42201718", "fuerte"), ("42295114", "fuerte"), ("42295126", "fuerte"),
            ("42295505", "fuerte"), ("42293504", "fuerte"), ("85121610", "fuerte"),
            ("42182005", "confirma"), ("42182014", "confirma"), ("31241501", "confirma")]),
  dict(codigo="DEV-MIC", linea="Device", nombre="Microscopía", prioridad=3, onu_solo_a_revision=False,
       terminos=[("Microscopio", r"\bmicroscopios?\b|\bmicrospcopio\w*", None, "cliente")],
       excluye=[("juguete", r"\bjuguete\w*|\bdidactic\w*"),
                ("insumo de microscopía", r"\b(porta\s*objeto|cubre\s*objeto|laminilla|aceite\s+de\s+inmersion|azul\s+de\s+metileno|tincion|colorante)\w*")],
       onu=[(c, "fuerte") for c in ("41111701","41111702","41111703","41111704","41111705","41111706","41111709","41111710",
                                    "41111712","41111719","41111720","41111721","41111722","41111723","41111724","41111726",
                                    "41111727","41111728","41111729","41111731","41111733","41111734","41111735","41111736","42295121")]
           + [("41122603", "confirma"), ("41122605", "confirma")]),
  dict(codigo="DEV-GUA", linea="Device", nombre="Guantes médicos", prioridad=4, onu_solo_a_revision=False,
       terminos=[("Guante", r"\bguantes?\b",
                  r"\b(nitrilo|latex|vinilo|examen|examinacion|procedimiento|quirurg|esteril|desechable|clinic|medic|polvo|poliisopreno|isopreno|caja\s+(de\s+|x\s*)?100|100\s+(unidades|und|un)\b|ambidiestr)\w*", "cliente")],
       excluye=[("guante no médico", r"\b(cabritilla|cabretilla|cuero|carnaza|forro|forrado|anticorte|multiflex|multiuso|multiproposito|mecanic|soldad|motorist|moto\b|arquer|portero|boxeo|futbol|jardin|alta\s+temperatura|termic|dielectric|electricist|aseo|domestic|hilo|lana|tejid|poliester|nylon|activex|invierno|polar|industrial|pvc|cocina|horno|ciclis|bicicleta|golf|equitacion|esqui|buceo|bombero|antivibra|construccion|agricol|pesca|box|seguridad|quimic|alta\s+resistencia|amarillo)\w*")],
       onu=[("42132201", "fuerte"), ("42132203", "fuerte"), ("42132204", "fuerte"), ("42132205", "fuerte"), ("42204005", "fuerte"),
            ("42295458", "confirma"), ("46181504", "confirma")]),
  dict(codigo="DEV-DET", linea="Device", nombre="Detergentes clínicos", prioridad=5, onu_solo_a_revision=False,
       terminos=[("Detergente", r"\bdetergentes?\b",
                  r"\b(enzimat|multienzim|pentaenzim|trienzim|tetraenzim|bienzim|instrumental|quirurg|clinic|hospital|laboratorio|esteriliz|endoscop|termodesinf|alcalin|extran|citometr|ultrasonic)\w*", "cliente")],
       excluye=[("detergente doméstico", r"\b(ropa|lavaloza|loza|lavavajilla|platos|lavadora|prendas|lavanderia|vehiculo|auto\b|resistente\s+a\s+(los\s+)?detergente|zapat|piso|vajilla|utensilio|alfombra|tapiz)\w*")],
       onu=[("42281704", "fuerte"), ("41103206", "fuerte"), ("42152450", "fuerte"), ("42312305", "confirma"), ("12161902", "confirma"), ("47131", "confirma")]),
  dict(codigo="DEV-APO", linea="Device", nombre="Apósitos", prioridad=6, onu_solo_a_revision=False,
       terminos=[("Apósito", r"\bapositos?\b", None, "cliente")],
       excluye=[],
       onu=[("42311532", "fuerte"), ("42311526", "fuerte"), ("42311506", "fuerte"), ("42311531", "fuerte"), ("42295406", "fuerte"), ("4231", "confirma")]),
]
```

## Apéndice B: cómo se midió

- **Universo:** `clasificador_ia_log` de 30 días unido a la tabla origen por PK
  (288.435 filas). Más las filas del mismo rango de `id` que **no** están en el log
  (8.091, casi todas `Bot Eliminado`).
- **Diccionario ONU:** `SELECT Cod_Onu, Producto_Servicio, COUNT(*) FROM Licitaciones_diarias GROUP BY 1,2`
  (13.035 pares, 12.948 códigos).
- **Glosa:** `LEFT(Descripcion, 600)`, normalizada con NFKD → ASCII → minúsculas.
- **Precisión:** sólo *inferida*, sobre muestras al azar de 12 a 30 filas por
  balde revisadas a mano. La medición real empieza con las revisiones de E4.
- **Entorno local:** `pymysql` en python3.12 falla por el `cryptography` del
  sistema. Se evita con `sys.modules['cryptography'] = None` antes de importar.
