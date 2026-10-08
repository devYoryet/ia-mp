# Fase 2 · Device y Servicio técnico

Segunda pasada sobre lo que la fase 1 (farma) **descarta**. Clasifica en
categorías Device / Servicio técnico con dos señales:

- **palabra** en la glosa;
- **nombre del código ONU** de la fila.

Si calzan las dos señales, la fila queda en **verde**. Si calza una sola, va a
**revisión** (aprobar / no aprobar). El diseño y las mediciones están en
[`../docs/06-fase2-device.md`](../docs/06-fase2-device.md).

## Garantías: la fase 1 no se toca

| candado | cómo se comprueba |
|---|---|
| **Código aparte.** Rama `fase2-device` y carpeta `fase2/`. No modifica ni importa ningún archivo de la fase 1. `main` no recibe commits, así que el auto-deploy no corre y worker y panel no se reconstruyen. | `git diff --stat origin/main...fase2-device` muestra sólo archivos nuevos |
| **Proceso aparte.** Container `ia-mp-fase2` (proyecto Docker `ia-mp-fase2`, imagen `clasificador-f2`) con límites de 0,5 CPU y 768 MB. No comparte imagen, red, volúmenes ni `.env` con `ia-mp`. | `docker inspect` del worker y del panel: misma imagen, mismo `StartedAt` |
| **Usuario MySQL propio** `ia_fase2`: `SELECT` sobre el schema; `INSERT`/`UPDATE` sólo en `clasificador_f2_resultado`, `_onu_nombre` y `_corridas`. Sin `DELETE` ni DDL, máximo 4 conexiones. | `python admin.py verificar`: MySQL rechaza cada escritura en tablas ajenas |
| **Candados en el código.** La conexión de lectura es `READ ONLY`. La de escritura valida el destino antes de enviar la sentencia. El servicio no corre con `root`. | `tests/test_candados.py` |
| **Tablas nuevas** con prefijo `clasificador_f2_`. Ningún listado por patrón existente las incluye (`clasificador_ia_%`). | `schema.sql` sólo crea tablas `clasificador_f2_*` |
| **$0 de API.** No llama a Claude. | no importa `anthropic` |

## Piezas

| archivo | qué hace |
|---|---|
| `motor.py` | decide categoría y señal para una fila. Es puro: no toca la BD |
| `semilla.py` | configuración v1.1 medida; se carga una vez a las tablas |
| `reconciliar.py` | qué hacer con cada fila: insertar, actualizar, anular si pasó a farma. Nunca pisa una decisión humana |
| `barrido.py` | lee las fuentes por bloques de PK y escribe sólo en `clasificador_f2_*` |
| `servicio.py` | loop: barrido rápido (3 días) cada 10 minutos, profundo (60 días) a diario y cuando cambian las reglas |
| `backtest_fase2.py` | mide sin escribir: volumen por categoría y señal, y precisión de lo revisado |
| `admin.py` | tareas de una persona con credenciales de administrador: tablas, usuario, semilla, verificación |

## Operación

```bash
# medir sin escribir nada (con la config de la BD, o --semilla)
python backtest_fase2.py --dias 30 --muestra 5
python backtest_fase2.py --precision            # cuando haya revisiones

# desplegar / actualizar en gestor_oc (sólo toca /opt/ia-mp-fase2 y el container ia-mp-fase2)
cd /opt/ia-mp-fase2/fase2 && ./desplegar.sh

# apagar (no afecta al clasificador)
cd /opt/ia-mp-fase2/fase2 && docker compose down

# bitácora de corridas
SELECT * FROM clasificador_f2_corridas ORDER BY id DESC LIMIT 10;
```

**Reglas editables sin deploy:** las tablas `clasificador_f2_categorias`,
`_terminos` y `_onu`. El servicio detecta el cambio de versión y re-evalúa los
últimos 60 días.

**Horas:** las fechas son `NOW()` del MySQL, es decir hora de Chile. No es el
UTC de los containers.
