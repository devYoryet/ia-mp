# 05 · Cómo medir un cambio

## Reglas

1. **Ningún cambio entra sin backtest antes y después**, y se mide **un cambio por corrida**.
2. **Los backtests masivos no llaman a la API.** Un container de backtest gastó
   $15–25 al día sin que se viera, hasta jun-2026.
3. **Medido ≠ inferido.** Cada número de un reporte dice de dónde sale.
4. Antes de medir, verificar que los `.joblib` locales son los desplegados:
   `md5sum *.joblib` en local contra
   `docker exec ia-mp-worker-1 md5sum /app/*.joblib` en gestor_oc.
   El harness avisa si falta un modelo, pero no si es otro.

## Qué cuenta como error

| evento | qué es | columna |
|---|---|---|
| la persona lo **elimina** | la IA dijo interés, la persona descartó | `FP` |
| la persona lo **edita** | interés correcto, pactivo equivocado | `pact✗` |
| la persona lo **rescata** | la IA descartó, la persona clasificó | `FN` |
| (no es error) | pactivo correcto, sólo cambió comp/pres | `c/p` |

Un FN pesa más que un FP: un FP cuesta una revisión, un FN es una oportunidad que
no llega. Además, los FN están sub-reportados en el feedback del panel, porque
las hojas de descartes se aprueban en bloque.

## Herramientas (todas a $0)

| script | para qué |
|---|---|
| `backtest_cascada.py` | A/B de la cascada. Reutiliza las respuestas de Claude guardadas en `clasificador_ia_log` |
| `funnel_cascada.py [días] [tabla]` | qué le llega a cada etapa en producción, qué resuelve y con qué error |
| `salud.py` | estado operativo por hora: containers, costo, pendientes por fuente, alertas |
| `reporte_corregidas.py` | qué corrigieron las personas |
| `pytest tests/` | pruebas unitarias sin BD ni API |

### backtest_cascada.py

```bash
python3 backtest_cascada.py --semanas 4 --por-semana 400 --etiqueta antes
# ... UN cambio ...
python3 backtest_cascada.py --semanas 4 --por-semana 400 --etiqueta despues
python3 backtest_cascada.py --comparar antes despues
```

- La muestra es **determinista** (`ORDER BY MD5(id)`): 'antes' y 'después' evalúan las mismas filas.
- `--tablas cotizaciones` mide otra fuente. Por defecto mide compra ágil + licitaciones.
  El harness se niega a comparar corridas de tablas o segmentos distintos.
- `--segmento {todo,cenabast,adjunto,interes}`: un cambio dirigido se mide **en su
  segmento** (¿arregla lo que dice arreglar?) **y** en el global (¿rompió algo en el resto?).
- La ventana se arma con la columna de fecha de cada fuente
  (`fuentes.columna_fecha_muestreo`). En cotizaciones es `Fecha_Publicacion`,
  porque los descartes no tienen `fecha_clasificacion`.
- **Fuente sin historia en el log** (una fuente nueva): las filas que llegarían a
  Claude no tienen respuesta guardada. No entran en las métricas y se informan
  aparte ("llegarían a Claude sin respuesta guardada … de ellas N son interés humano").
- **Sesgo conocido** (afecta igual a ambos lados del A/B): los índices de
  `cruce_base`, `historico`, `descarte_item` y el histórico de comp/pres se
  construyen **hoy**, con etiquetas posteriores a la ventana. Esas ramas se ven
  algo mejor que en su momento.
- **Poder estadístico:** con ~3.200 filas comparables y ~40 errores, sólo se
  detectan cambios de ±20–25 % del error total. Un cambio esperado chico se mide
  con más filas o en su segmento.

## Caso de referencia: refactor de fuentes (2026-09-29)

Un cambio estructural (tablas en un registro único) tiene que dar **exactamente**
el mismo resultado:

```
pre_fuentes  vs  post_fuentes   (2 semanas × 300 filas/tabla, compra ágil + licitaciones)
FP 13 → 13 · pactivo malo 5 → 5 · FN 1 → 1 · c/p 7 → 7 · SIN CAMBIO
totales y desglose por método idénticos en ambas semanas
```

El panel se verificó igual:

- Se ejecutó el SQL de la cola viejo y el nuevo sobre la BD viva, en el mismo instante: 361 = 361 filas, mismo orden.
- Mismos conteos de supergrupos.
- Se renderizaron todas las páginas con el código original y el nuevo (`TestClient`).

## Rarezas de medición conocidas

- `/estadisticas` cuenta "Humano clasificó" por `fecha_clasificacion`. En
  cotizaciones los descartes no tienen fecha, así que sólo se cuentan los intereses.
- `creado_en` del log está en UTC; `NOW()` del MySQL, en hora de Chile fija (−04).
  Cualquier ventana "últimas 24 h" contra el log está corrida 4 h.
