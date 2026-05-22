# Clasificador IA — compras ágiles y licitaciones

Sistema liviano que automatiza la clasificación que hoy hacen 3 personas en la app
`gestor_licitaciones`: por cada fila de `compra_agil` / `Licitaciones_diarias`
decide si es **de interés** y le asigna **pactivo / composición / presentación**
desde la lista controlada (`principal_app.diccionario_unidad`).

Corre en el servidor **gestor_oc**: el trabajo pesado lo hace la API de Anthropic,
el worker solo coordina. Panel web en `iabot.pharmatender.cl`.

## Modos de operación

| `MODO` | Qué hace | Cuándo |
|---|---|---|
| **`test`** (def.) | Clasifica filas que **ya clasificó una persona** y compara IA vs humano. **No escribe nada en `compra_agil`.** | Ahora — para medir el rendimiento sin tocar producción. |
| `produccion` | Escribe la clasificación en `compra_agil` como `nombre_clasificador='Bot IA'` (deja `estado_gestor` NULL → la persona confirma). | Cuando el backtest dé una precisión aceptable. |

Se cambia con una línea en `.env`. **El backtest no reemplaza al panel de
confirmación** — responde otra pregunta: *¿la IA es lo bastante buena para que
valga la pena el tiempo de revisión?* Primero se mide, después se produce.

## Hoja de ruta

- **Fase 0 — Backtest (actual).** `MODO=test`: clasificar algunos miles de filas
  ya resueltas por personas y medir acierto de interés y de pactivo.
- **Fase 1 — Producción con confirmación.** `MODO=produccion`: la IA propone en
  `compra_agil`; las personas confirman/corrigen en el panel o en el gestor.
- **Fase 2 — Recategorización.** Las filas descartadas (`estado_gestor=0`, ~90 %)
  se reclasifican en ~50 categorías generales (`sys_categorias_nivel1`) usando
  campos que ya existen en `compra_agil`.
- **Fase 3 — Más fuentes.** Integrar tratos directos, cotizaciones y consultas
  al mercado (el detector ya está parametrizado por tabla).

## Arquitectura

```
  clasico:3306                          gestor_oc (este proyecto, Python)
  ┌────────────────────┐   detector     ┌──────────────────────────────────┐
  │ compra_agil         │◀─────────────▶│ worker.py  (loop cada N minutos)  │
  │ Licitaciones_diarias│   escritor     │   detector → cascada → escritor   │
  │ diccionario_unidad  │──taxonomía────▶│ cascada: reglas → Claude (API)    │
  │ clasificador_ia_*   │◀──resultados───│ api/  panel web (iabot...cl)      │
  └────────────────────┘                └──────────────────────────────────┘
```

- **Etapa 1 (`reglas.py`)** — atajo de alta precisión: solo match EXACTO contra
  el diccionario. Conservador a propósito; **Claude es el motor principal**.
- **Etapa 2 (`clasificador_claude.py`)** — Claude (Opus 4.7) con la lista
  controlada en *prompt caching* y salida estructurada.
- **Mejora continua (`ejemplos.py`)** — cada corrección humana se vuelve un
  ejemplo few-shot para las clasificaciones siguientes.

### Nota sobre el tamaño del prompt
Pasarle toda la lista de pactivos al modelo era un problema con modelos de
contexto chico. Con Opus 4.7 (1M de contexto) + prompt caching la lista completa
(~2.400 pactivos) es viable y barata (se cachea a ~0.1x). Si hace falta afinar
costo/latencia/precisión, el siguiente paso es **acotar candidatos**: pre-filtrar
~30-50 pactivos plausibles por fila y pasar solo esos.

## Puesta en marcha (desarrollo)

```bash
pip install -r requirements.txt
cp .env.example .env                 # completar ANTHROPIC_API_KEY
mysql -h 10.0.0.69 -u root -p licitaciones_diarias_total_farma < schema/auditoria.sql

./run.sh test                        # una pasada de backtest
./run.sh api                         # panel en http://localhost:8800
```

## Contenedores (Docker)

Dos contenedores desde una sola imagen — `worker` (backend, clasifica) y `panel`
(frontend web de revisión). La BD no va en contenedor: es el MySQL del clásico.

```bash
docker compose up --build        # local
docker compose up -d --build     # servidor, en segundo plano
```

El panel queda en `http://localhost:8800`. El `.env` se inyecta en runtime
(no se hornea en la imagen). El contenedor trae su propio Python 3.12.

## Despliegue en gestor_oc

**Opción A — Docker (recomendada):** instalar Docker en gestor_oc, copiar el
proyecto y `docker compose up -d --build`. Luego `nginx` (`deploy/nginx-iabot.conf`)
publica `iabot.pharmatender.cl` → `127.0.0.1:8800`.

**Opción B — systemd:** sin Docker — `deploy/clasificador-worker.service` y
`deploy/clasificador-api.service`, más el nginx anterior.

En ambas: pedir al DNS el subdominio `iabot.pharmatender.cl` apuntando a la IP
pública de gestor_oc y emitir SSL con `certbot`.

## Archivos

| Archivo | Rol |
|---|---|
| `config.py` | Configuración central (lee `.env`) |
| `db.py` | Conexión MySQL al servidor clásico |
| `taxonomia.py` | Carga la lista controlada de pactivos |
| `reglas.py` | Etapa 1: filtro barato sin IA |
| `clasificador_claude.py` | Etapa 2: Claude con prompt caching |
| `ejemplos.py` | Few-shot a partir del feedback humano |
| `cascada.py` | Orquesta etapa 1 → etapa 2 |
| `detector.py` | Selecciona filas (pendientes o para backtest) |
| `escritor.py` | Persiste resultado (backtest o producción) |
| `worker.py` | Loop principal |
| `api/main.py` | Panel web: resumen, backtest y cola de revisión |
| `schema/auditoria.sql` | Tablas `clasificador_ia_log` y `clasificador_ia_backtest` |
| `deploy/` | nginx + systemd para gestor_oc |
