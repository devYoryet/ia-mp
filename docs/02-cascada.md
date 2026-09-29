# 02 · La cascada de clasificación

Punto de entrada: `cascada.clasificar_fila(tabla, fila, ...)`. El cuerpo está en
`_clasificar_fila_impl`. Las etapas van **de la más barata a la más cara**. La
primera que resuelve devuelve un `Resultado` y las demás no corren. El nombre de
esa etapa queda en `clasificador_ia_log.metodo`, y ahí está la causa de un
error. No hay que suponer que fue Claude.

Para ver el estado medido de cada etapa: `python3 funnel_cascada.py 30`.

## Paso previo: preparación por fuente

`fuentes.preparar_fila(tabla, fila)` corre antes de todo. Para compra ágil y
licitaciones devuelve la misma fila, sin cambios. Para cotizaciones quita el
sufijo UNSPSC que el scraper pega a la glosa (ver [03-fuentes.md](03-fuentes.md#cotizaciones))
y guarda la glosa cruda en `_desc_cruda`. El histórico (etapa 2) y
`modelo_descarte` (etapas 2, 5 y 6) usan la cruda, porque ahí el sufijo suma. Las
ramas que asignan pactivo usan la limpia.

**Ajustes por fuente** (declarados en `fuentes.py`, no en la cascada):
`usar_modelo_adjunto` (apagado en cotizaciones) y `umbral_modelo_pactivo`
(0,50 en cotizaciones; el global es 0,30).

## Etapas

| # | `metodo` | lee | conocimiento de | resultado | ¿consulta vetos? |
|---|---|---|---|---|---|
| 0 | `veto_<nombre>` | `Descripcion` | `clasificador_ia_reglas` tipo=veto, `aplica_a` incluye `inicio_cascada` (+ jabón con excepción, en código) | descarte duro, conf 0.97 | — |
| 1 | `cruce_base` | `Descripcion` | coincidencia EXACTA normalizada contra ~1,4 M de textos de OC reales: `0001_td_oc.Base` + `analisis_precios.Base` (EspComprador / EspProveedor) | interés, pact/comp/pres **verbatim**, conf 0.95 | ❌ |
| 2 | `historico` | `Descripcion` (cruda) | la misma glosa ya clasificada por una **persona** en la **misma tabla** (se excluyen bots: `^(Bot\|BOT\|IA_)`) | la etiqueta humana más frecuente | ❌ |
| 3 | `modelo_adjunto` | Título + Descripción | ML binario "¿el detalle está en el anexo?" | interés, pactivo `Adjunto` | ❌ |
| 4 | `descarte_item` | código de rubro (`fuentes.columna_rubro`) | códigos con ≥20 vistas humanas y 0 interés, **de la misma tabla** | descarte, conf 0.97 | ❌ |
| 5 | `regla_diccionario` / `conflicto_regla_modelo` | `Descripcion` **sola** (el título no se mira, a propósito) | nombres del catálogo activo: primero pactivo combinado, después simple | interés conf 0.90, o descarte si `modelo_descarte` ≥ 0.97 contradice un match simple | ✅ |
| 6 | `modelo_descarte` | `Descripcion` | ML binario entrenado con compra_agil + Licitaciones_diarias | descarte si prob ≥ 0.97 | ❌ |
| 7 | `modelo_pactivo` | `Descripcion` | ML multiclase (~1.600 pactivos) de 4 fuentes etiquetadas | interés si prob ≥ 0.30 | ✅ |
| 8 | `modelo_marcas` / `modelo_marcas_posible` | Título + Descripción + VINCULOS | ML de marcas comerciales (sólo compra_agil + licitaciones) | verde si ≥ 0.50; amarillo "revisar" si ≥ 0.30 | ✅ |
| 9 | `claude` | todo + catálogo activo + reglas + correcciones + marcas + top-K candidatos | Haiku con prompt cacheado | interés / descarte / pactivo nuevo propuesto | ✅ |
| Z | `<metodo>_pact_inactivo` | — | catálogo activo | **final guard**: un interés con pactivo fuera del catálogo pasa a descarte | — |

### Detalles que importan

- **Etapa 1 va antes que el descarte por rubro** a propósito. Una coincidencia
  exacta con una OC real protege a un producto médico de un rubro mal asignado.
  - Se ignora si el pactivo ya no está en el catálogo activo.
  - Se ignora si la glosa indica un compuesto de 3 componentes y el hit es de 1 o 2 (`SUFIJO_COMPUESTO`).
  - Las coletillas genéricas ("según adjunto", "canasta 2"…) no se indexan (`cruce_base._CLAVES_GENERICAS`).
- **Etapa 2**:
  - Un descarte histórico con menos de `UMBRAL_DESCARTE_HISTORICO` (5) votos no se confía.
  - Si el histórico dice interés con 1 o 2 votos y `modelo_descarte` está ≥ 0.97 seguro de lo contrario, sale como **CONFLICTO REVISAR** (interés sin pactivo, `pactivo_propuesto` = el histórico).
- **Etapa 3** no rinde en producción: 38 % de precisión por un problema de base-rate. Ver la memoria `modelo-adjunto-base-rate`.
- **Etapa 4** tiene una excepción (camino 2). Si la glosa es **sólo** una referencia a un anexo y el título es médico, no se descarta por rubro y la fila sigue hacia Claude.
- **Etapa 5**:
  - Corta la glosa en "Excip:".
  - Normaliza "con vasoconstrictor" a epinefrina (así puede armar el pactivo combinado).
  - Tiene vetos por rama (glicerina, lubricante…) y el veto de antibiótico en formato no medicamento (tiras, tests, cemento).
- **Etapas 7, 8 y 9** ignoran el meta-pactivo `Adjunto` en los modelos (sólo Claude lo asigna) y anulan un pactivo simple cuando la glosa indica compuesto.

## Composición y presentación

- Las ramas 1 y 2 devuelven comp/pres **verbatim**. Los cruces posteriores con los
  clientes hacen match estricto, así que no se "corrigen".
- Las ramas 5, 7, 8 y 9 eligen comp/pres en este orden:
  1. lo que se extrae de la glosa (`taxonomia.extraer_de_glosa`);
  2. la opción del histórico humano de ese pactivo que mejor encaja con la glosa (`preclasificador.elegir_comp_pres_por_descripcion`);
  3. la moda del pactivo (`comp_pres_por_pactivo`).
  
  ⚠ La moda aplasta formatos raros (caso Cladribina `CM/CM REC` → Ampolla).
- **Por tabla:** el histórico de comp/pres se precarga desde la tabla de la fuente
  (`precargar_comp_pres(TABLAS)`). Una fuente nueva usa **su propio** histórico.
- Al final, para todas las ramas:
  - `canonizar_comp` / `canonizar_pres` validan la comp/pres contra las opciones reales del pactivo;
  - si la comp quedó en el comodín `Sin cla` pero la glosa trae la dosis, `recuperar_comp_de_glosa` la recupera.
- Comodines: **`Sin cla`** (sin s) significa "no se pudo determinar", y en el cruce
  con los clientes hace match **no estricto**. **`Sin Clas`** (con s) es un valor
  real del catálogo y hace match estricto.

## Vetos: cómo se elimina algo

Toda eliminación es una **regla con nombre** en `clasificador_ia_reglas`
(`tipo='veto'`, `regex_pattern`, `aplica_a`, `pactivo_filtro`) y se edita en
`/reglas`, sin deploy. **No se escriben `set` ni `if` con nombres de producto en
Python.** `aplica_a` sólo tiene efecto en `inicio_cascada`, `regla_diccionario`,
`modelo_pactivo`, `modelo_marcas` y `claude`. Un veto con
`aplica_a='cruce_base'` no hace nada y no avisa. Si la BD de vetos no carga,
rige un respaldo escrito en `cascada.py`.

## Por qué un error cae donde cae

1. `SELECT metodo, razon, confianza, veto_aplicado FROM clasificador_ia_log WHERE tabla_origen=? AND fila_id=?`.
2. `regla_diccionario` / `modelo_*` con comp/pres rara → probablemente la moda histórica.
3. `cruce_base` con glosa genérica → una clave contaminada del índice.
4. `*_pact_inactivo` → el final guard. ¿El pactivo debería estar en `pactivos_extra`?
5. Una regla del prompt **sólo sirve si la fila llega a Claude**. Si una rama
   barata la resuelve antes, la corrección tiene que ser un veto temprano.
