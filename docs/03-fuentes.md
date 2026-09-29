# 03 · Fuentes

Todas viven en el MySQL del clásico, schema `licitaciones_diarias_total_farma`.
Todas usan las mismas columnas de clasificación: `pactivo`, `composicion`,
`presentacion`, `estado_gestor` (NULL = pendiente, 1 = interés, 0 = descarte),
`nombre_clasificador` y `fecha_clasificacion`. Las columnas de datos, en cambio,
**no son iguales entre tablas aunque se llamen igual**.

El registro de código está en `fuentes.py`. Relevamiento medido el 2026-09-29,
salvo donde se indica.

## Resumen comparativo

| | compra ágil | licitaciones | **cotizaciones** | trato directo | consulta al mercado |
|---|---|---|---|---|---|
| tabla | `compra_agil` | `Licitaciones_diarias` | `cotizaciones` | `trato_directo_detalles` (+ `_listado`) | `consulta_mercado` (+ `preguntas_consulta_mercado`) |
| en la IA | ✅ producción | ✅ producción | 🟡 registrada; worker apagado (`FUENTES_WORKER`) | ⬜ no | ⬜ no |
| una fila es | una línea | una línea | una línea | una línea | la consulta completa |
| código de rubro | `Item` (UNSPSC) | **`Cod_Onu`** (UNSPSC) | `Item` (UNSPSC) | no hay; `producto_servicio` trae el nombre UNSPSC en texto | no hay |
| nº de línea | `Cod_Onu` | `Item` | `Cod_Onu` | `id` | — |
| `Descripcion` | varchar(10000) | **varchar(255)** | varchar(10000); **trae el sufijo UNSPSC** | `detalle_producto` | `nombre` / `descripcion` / `motivo` |
| volumen | ~15.000 filas/día entre compra ágil y licitaciones | | ~50 filas/día | ~530 filas/día | ~45/día |
| % de interés | | | ~5 % | 3–9 % | 3–5 % |
| qué asigna la persona | pact/comp/pres | pact/comp/pres | pact/comp/pres **reales** | 80 % `Varios Productos` / Sin cla | `Match Farma` / `Match device` |
| bot en el legacy | `estandarizar` (listas negras + diccionarios) | | ninguno | ninguno | ninguno |

## compra ágil (`compra_agil`)

- Rubro en `Item`; `Cod_Onu` es el correlativo de la línea. El índice por rubro es
  el que más volumen descarta (etapa 4).
- `Descripcion`: glosa del comprador. Después de un tab suelen venir especificaciones.
- Legacy: `CompraAgilController` (index / update / remove / `estandarizar`). El
  bot `estandarizar` corre cada 15 min vía `/importar_ca_automatico_estandarizacion`
  y marca `Bot Clasificado` / `Bot Eliminado`. **El worker de la IA no excluye esas
  filas**: toma todo lo que tenga `estado_gestor` NULL.
- Envío a clientes: `match:compras_agiles` en prime (el `importar:compras_agiles`
  del legacy está comentado).
- Scraper: **pendiente de relevar** en este documento. Ver la memoria
  `huecos-listado-compra-agil` sobre los huecos del listado de día.

## licitaciones (`Licitaciones_diarias`)

- **Al revés que compra ágil:** el rubro está en `Cod_Onu` y `Item` es el nº de línea.
- `Descripcion` es varchar(255). Cualquier cruce con otra tabla tiene que truncar a 255.
- `Estado_creado_adjunto`: verdad de terreno de "Adjunto" (la escribe una persona,
  nunca un bot). Sirve para evaluar, no como feature.
- En Licitaciones no debe haber una rama automática de Adjunto (ninguna receta
  pasó de 12,5 % de precisión).
- Scraper y envío a clientes: **pendiente de relevar** en este documento.

## cotizaciones

**Tabla** `cotizaciones`: MyISAM, latin1. 96.207 filas desde 2022-03, unas 1.700
al mes. Código de cotización `NNNN-NN-SC26`. Clave única
`(Licitacion, Item, Cod_Onu)`. Mismo esquema que compra ágil, incluidos los
datos de contacto y `monto_total`.

**Cómo la arma el scraper**
(`/home/platform/CRONJOBS/PYTHON/scraping/cotizaciones.py`, cron de root del
clásico, cada hora de 6 a 23 h y de madrugada):

- Selenium con ClaveÚnica. Descarga el Excel de "Buscar cotizaciones" → `cotizaciones_listado`.
- Por cada cotización publicada abre la ficha y, por cada producto de la grilla, inserta una fila:
  - `Item` = código UNSPSC; `Cod_Onu` = correlativo.
  - **`Descripcion` = glosa del producto + `' '` + nombre UNSPSC del portal.**
  - `Titulo` = glosa (en CENABAST, con el código CENABAST entre paréntesis).
  - `VINCULOS` = `Descripcion` + `'.'` + descripción general de la cotización.
  - `Producto_Servicio` = descripción general de la cotización.

**El sufijo UNSPSC es un peligro, no sólo ruido.** Muchas veces es el nombre de
**otro** fármaco: "SEMAGLUTIDA 4MG/3ML SOL/INY DISP+AG **Glucagon**",
"ALPELISIB 250MG **Ciclosporina**", "RAVULIZUMAB 300MG… **Abciximab**". Sin
limpiarlo, la etapa 5 o Claude pueden asignar el pactivo del sufijo.
`fuentes.preparar_fila` lo quita:

- El sufijo de cada código se **aprende de los datos**: son las palabras finales
  que comparten todas las glosas distintas de ese código.
- Cubre el **97,6 %** de las filas (medido 2026-09-29).
- `sys_onu_codigos_analizados` **no sirve** para esto: en 1.352 códigos su `nombre` es una glosa.
- Las filas sin sufijo conocido se dejan intactas. Casi todas son códigos vistos
  una sola vez y no farma.

**Quién clasifica:** 100 % personas (Benjamín Saavedra, Evelyn Muñoz, Carolina
Burgos), sin bot. Histórico: 7.203 filas de interés con pact/comp/pres reales y
88.989 descartes.

⚠ Desde mar-2026 los descartes quedan con `fecha_clasificacion` NULL. Por eso el
backtest muestrea por `Fecha_Publicacion` (`columna_fecha_muestreo`).

**Envío a clientes, por dos caminos:**
1. Legacy `ImportarCotizaciones` (gestor_2021, cada hora):
   - toma `estado_gestor=1` de los últimos 7 días;
   - cruza con `principal_app.diccionario_unidad`: pactivo igual + comp/pres por LIKE, o comodín `Sin cla`;
   - escribe en `licitaciones_diarias_intranet.Licitaciones_diarias_clientes_cotizaciones` (clásico y prime).
2. Prime `CorreoMasivoAutomaticoController::matchDiccionarioCotizaciones`:
   - clientes con `permiso_id=38`;
   - deduplica con `correos_masivos.cotizaciones_enviadas`.
   - ⚠ Inferido del código: agrupa por `(Licitacion, Item)`, así que dos líneas del mismo código UNSPSC colapsan.

**Lo que se reutiliza de compra ágil (medido sobre 2.000 cotizaciones recientes):**
- **El rubro UNSPSC predice el interés.**
  - Con el histórico de compra ágil de 12 meses: 98,9 % de acierto, 0 falsos negativos en los rubros puro-descarte.
  - Con el histórico propio: 0 falsos negativos en 1.296 filas puro-descarte.
- **La glosa casi no se repite en compra ágil:** 0 % de coincidencia exacta y 8,75 %
  quitando el sufijo. El histórico útil es el **propio** de cotizaciones.

**Configuración de la cascada para cotizaciones** (`fuentes.py`). Cada punto se
midió con backtest de 12 semanas (4.082 filas reales, $0), un cambio por corrida:

| corrida | cambio | FP | pact✗ | FN | c/p | a Claude |
|---|---|---|---|---|---|---|
| `cotz_sin_limpieza` | cascada de compra ágil tal cual | 21 | 15 | 0 | 39 | 404 |
| `cotz_con_limpieza` | glosa sin sufijo UNSPSC en todas las etapas | 27 | 15 | 0 | 29 | 494 |
| `cotz_limpia_desc_cruda` | limpia para asignar pactivo; **cruda** para histórico y `modelo_descarte` | 24 | 15 | 0 | 29 | 396 |
| `cotz_sin_adjunto` | `modelo_adjunto` apagado (0 de 11 aciertos) | 18 | 10 | 0 | 29 | 405 |
| `cotz_umbral050` | umbral de `modelo_pactivo` 0,50 (en 0,30–0,50 acertaba 5 de 17) | **17** | **7** | **0** | **29** | 410 |

- **Por qué la glosa cruda para descartar:** el nombre UNSPSC es buena señal de *rubro*.
  - Medido en 13.496 filas de 2026: con la glosa cruda, `modelo_descarte` resuelve 829 descartes más, con el mismo riesgo (2 contra 3 de 632 intereses).
  - Para el *pactivo* es al revés: con la glosa limpia el diccionario acierta 614 intereses más de 7.203 y comete la mitad de errores de pactivo (192 contra 396).
- **Resultado:** las etapas gratuitas resuelven ~90 %, sin falsos negativos. A Claude llegarían unas **34 filas por semana**.
  - Esa rama no se puede medir sin gastar API.
  - Costo inferido: centavos al mes.
- **Pendiente:** `modelo_marcas` quedó con la mayor parte del error restante (14 filas, 9 FP). Es el mismo patrón: los modelos entrenados con compra ágil y licitaciones no transfieren bien.

**Bug conocido del legacy** (no es de este repo): `CotizacionesController::remove`
borra de `..._clientes_agil` en vez de `..._clientes_cotizaciones`. Descartar
una cotización ya enviada no la retira del cliente.

## trato directo (relevado, todavía no integrado)

- `trato_directo_detalles`: una línea por producto. Repite datos de la cabecera
  (`nombre`, `descripcion`, `justificacion`, `proveedor_*`, `monto_total`). Código `NNNN-NN-FTD26`.
- Columnas con otros nombres (minúsculas). El legacy los renombra para reutilizar
  la interfaz: `codigo_trato AS Licitacion`, `nombre AS VINCULOS`.
- El 98,8 % ya tiene la OC emitida (son compras hechas).
- En el 80 % del interés la persona pone `Varios Productos` / `Sin cla`. En la
  práctica, sólo decide el interés. `diccionario_unidad` tiene `Varios Productos`
  en 53 unidades de negocio.
- Envío: prime `matchTratosDirectos` (permiso 45).
- **Scraper: no está en ningún cron conocido.** Las filas llegan en lotes cada unos
  40 min; hay conexiones root desde la IP de VPN 10.212.134.200, sin identificar.

## consulta al mercado (relevado, todavía no integrado)

- `consulta_mercado`: una fila por consulta, sin ítems ni rubro. Código `NNNN-NN-RFI26`.
  Las preguntas están en `preguntas_consulta_mercado`.
- Taxonomía propia: el pactivo es `Match Farma` o `Match device`. Es un clasificador
  de rubro, no de principio activo.
- Envío: prime `matchConsultasMercado`, sólo para clientes con `Match Farma` /
  `Match device` en el diccionario (permiso 44).
- Scraper: `compra_agil_test/extractor_consultas_al_mercadov2.py` (API
  `servicios-consultas-prd.mercadopublico.cl`, cron cada 7 h). Las inserciones
  horarias no calzan con ese cron; puede haber otra máquina cargando.
