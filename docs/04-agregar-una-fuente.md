# 04 · Cómo agregar una fuente nueva

La primera vez se hizo con **cotizaciones** (sep-2026). El orden importa: cada
paso se mide antes de pasar al siguiente, y nada llega a producción sin backtest.

## 1. Relevar la fuente (sin escribir código)

Responder con consultas, no con supuestos, y anotarlo en [03-fuentes.md](03-fuentes.md):

- [ ] **Tabla y grano:** ¿una fila es una línea o una oportunidad completa? ¿Cuál es la clave única?
- [ ] **Texto a clasificar:** ¿en qué columna está? ¿El scraper le pega algo? (En cotizaciones pega el nombre UNSPSC.)
- [ ] **Rubro:** ¿hay código UNSPSC? ¿En qué columna? (Los nombres engañan: `Item` y `Cod_Onu` se intercambian entre tablas.)
- [ ] **Clasificación humana actual:** ¿quién clasifica? ¿Con qué campos? ¿Qué pactivos usa? (¿reales, `Varios Productos`, `Match Farma`?)
- [ ] **Histórico etiquetado:** cuántas filas de interés y de descarte hay. ¿`fecha_clasificacion` está poblada en ambas?
- [ ] **Scraper:** qué script es, qué cron, a qué hora llegan las filas.
- [ ] **Envío a clientes:** qué proceso cruza la tabla con `diccionario_unidad` y con qué criterio. Eso define qué significa "bien clasificado".
- [ ] **Solapamiento:** ¿sus glosas se repiten en compra ágil? ¿Su rubro predice el interés? Eso dice qué etapas de la cascada se pueden reutilizar.

## 2. Registrarla en `fuentes.py`

```python
Fuente("cotizaciones", "Cotizaciones", columna_rubro="Item",
       columna_fecha_muestreo="Fecha_Publicacion",
       limpiar_sufijo_unspsc=True),
```

Con sólo registrarla:

| queda automático | dónde |
|---|---|
| filtro "Tabla" en `/revision`, `/estadisticas` y card de pendientes en `/` | `api/main.py` |
| búsqueda por número (`?licitacion=`), fecha de publicación/cierre, cola sincronizada con el legacy | `api/main.py` (`_sql_col_origen`, `NOT EXISTS` por fuente) |
| aprobar / corregir / descartar escribe en la tabla correcta | `sync_pendientes.py` (valida contra `TABLAS_VALIDAS`) |
| descarte por rubro con el histórico **propio** | `descarte_items.py` |
| histórico de glosa idéntica y comp/pres del histórico **propio** | `preclasificador.py` (precarga por tabla) |
| backtest de la fuente | `backtest_cascada.py --tablas <tabla>` |

**Supuesto:** la tabla tiene las columnas estándar (`id`, `Titulo`, `Descripcion`,
`VINCULOS`, `Item`, `Cod_Onu`, `Licitacion`, `Fecha_Publicacion`, `Fecha_Cierre`,
`Demandante` y las de clasificación). Trato directo y consulta al mercado
**no las tienen**: van a necesitar una vista o un mapeo de columnas en el
registro antes de este paso.

Qué **no** queda automático, y hay que decidir fuente por fuente:

- [ ] Columnas del export Excel (`_LEGACY_COLS` en `api/main.py`). Cotizaciones reutiliza las de compra ágil.
- [ ] Limpieza del texto (`preparar_fila`). Si la fuente necesita otra, agregarla ahí con su prueba.
- [ ] Modelos entrenados (`entrenar_*.py`): siguen entrenando sólo con compra_agil + Licitaciones_diarias. Sumar la fuente al entrenamiento es un cambio aparte y se mide aparte.
- [ ] Correcciones al prompt (`reglas_negocio.py`): el JOIN que trae la glosa de una corrección conoce sólo esas dos tablas.
- [ ] Reportes (`analizar_produccion.py`, `reporte_*`): revisar si filtran por tabla.

## 3. Pruebas unitarias

`python3 -m pytest tests/ -q`. Toda limpieza o regla nueva de la fuente lleva
pruebas con casos **reales** (ver `tests/test_fuentes.py`).

## 4. Backtest sin API ($0)

```bash
python3 backtest_cascada.py --tablas cotizaciones --semanas 8 --por-semana 250 --etiqueta cotz_base
```

Como la fuente nunca pasó por producción, no hay respuestas de Claude guardadas.
Las filas que llegarían a Claude se **cuentan aparte** ("llegarían a Claude sin
respuesta guardada") con cuántas de ellas son interés humano. Eso mide:

- qué resuelven gratis las etapas baratas y con qué error;
- cuántas llamadas a Claude hacen falta, y por lo tanto el costo;
- cuánto interés depende de Claude.

Cada decisión de la fuente (limpiar sí o no, rubro propio o compartido) es **un
cambio por corrida**, con `--comparar`.

## 5. Backtest con Claude, acotado

Requiere **autorización explícita**: gasta API. Pocas filas (~300), para medir la
rama que el paso 4 no puede ver.

## 6. Activar en producción

1. Commit + push (el auto-deploy lleva el código). La fuente queda registrada pero el worker **no** la procesa.
2. En el host: agregar la tabla a `FUENTES_WORKER` en `/opt/ia-mp/.env` y hacer `docker compose up -d worker`.
   - ⚠ La primera pasada toma **todas** las filas con `estado_gestor` NULL de esa tabla. Antes de activar, contar cuántas hay y de qué fechas son (en trato directo hay ~11.000 de sep-2025).
3. `salud.py` empieza a contar los pendientes de la fuente (lee el mismo `FUENTES_WORKER`).
4. Seguimiento de la primera semana: `python3 funnel_cascada.py 7 <tabla>` y las correcciones humanas en `/revision?tabla=<tabla>&estado=revisadas`.
