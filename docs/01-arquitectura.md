# 01 · Arquitectura: de mercadopublico.cl al cliente

## El negocio en una línea

Pharmatender vende **información categorizada**. Cada oportunidad de compra
pública se etiqueta con `pactivo / composicion / presentacion` de un catálogo
controlado y después se cruza con el diccionario de cada cliente. La etiqueta
define a quién le llega. Un pactivo mal puesto hace que la oportunidad **no le
llegue a nadie**. Un pactivo sin cliente activo se descarta a propósito: eso no
es un error.

## Recorrido de una fila

```
 mercadopublico.cl
      │  scrapers del clásico (cron / Selenium / API)            ← NO son de este repo
      ▼
 MySQL clásico · licitaciones_diarias_total_farma
   compra_agil · Licitaciones_diarias · cotizaciones · ...        estado_gestor = NULL
      │
      │  worker (container ia-mp-worker-1, cada INTERVALO_SEGUNDOS)
      │    detector.filas_pendientes  → estado_gestor NULL y sin registro en el log
      │    cascada.clasificar_fila    → Resultado (interés, pact/comp/pres, método)
      │    escritor.aplicar_produccion
      ▼
 fila origen: pactivo/comp/pres + nombre_clasificador='Bot IA'   estado_gestor SIGUE NULL
 clasificador_ia_log: la sugerencia, el método, la razón, el costo
      │
      │  panel (container ia-mp-panel-1, iabot.pharmatender.cl) · /revision
      │    la persona aprueba / corrige / descarta
      │    revisar_hoja → JSON en /app/pending → sync_pendientes.aplicar_lote
      ▼
 fila origen: estado_gestor 1|0 + pact/comp/pres finales + nombre de la persona
 clasificador_ia_log: revisado=1, feedback_correcto, feedback_pactivo
 clasificador_ia_reglas: el motivo de una corrección (tipo='correccion')
      │
      │  procesos de envío (legacy gestor_2021 y prime)          ← NO son de este repo
      ▼
 principal_app.diccionario_unidad (pact/comp/pres por cliente, comodín 'Sin cla')
 → tablas *_clientes_* y correos masivos
```

Tres reglas del diseño:

1. **La IA no aprueba nada.** El worker deja `estado_gestor` en NULL. Sólo una
   persona lo pasa a 1 o a 0. (`AUTO_APLICAR_DESCARTES` existe, pero está en `false`).
2. **El legacy sigue vivo en paralelo.** Las mismas personas clasifican en
   `gestor_licitaciones` (bi.pharmatender.cl) fuera del horario del panel. Si una
   persona clasifica una fila en el legacy, la cola del panel la oculta
   (`NOT EXISTS` por fuente en `/revision`). El worker igual la procesa.
3. **Nada de lo aprobado se pierde.** El panel guarda el lote en disco antes de
   escribir en la BD. Si MySQL está caído, un cron reintenta cada 5 minutos
   (`sync_pendientes.py`).

## Containers y hosts

| pieza | dónde | qué |
|---|---|---|
| `ia-mp-worker-1` | gestor_oc, `/opt/ia-mp` | `python worker.py`: clasifica en loop |
| `ia-mp-panel-1` | gestor_oc, red del host, puerto 8800, nginx → iabot.pharmatender.cl | FastAPI `api/main.py`: revisión, reglas, estadísticas, módulos legacy |
| MySQL clásico | 10.0.0.69 (no está en un container) | tablas origen + tablas `clasificador_ia_*` |
| prime | 10.0.0.68:8806 | clientes vivos (`pharmatender.company` + `users`) para el catálogo activo; correos masivos |

**Deploy:** un push a `main` en `github.com/devYoryet/ia-mp` dispara GitHub
Actions (`.github/workflows/deploy.yml`). Primero corre `compileall` y, si pasa,
hace SSH a gestor_oc: `git reset --hard origin/main && docker compose up -d --build`.
El `.env` del host **no** está en git. Ahí se fijan `MODO`, `FUENTES_WORKER`,
los umbrales y las claves.

**Modelos grandes:** `modelo_pactivo.joblib` (537 MB) y `modelo_marcas.joblib`
(76 MB) están en `.gitignore` y viven en `/opt/ia-mp`. Si falta un `.joblib`,
su rama de la cascada **se saltea en silencio**.

## Tablas del clasificador (en el clásico)

| tabla | qué guarda |
|---|---|
| `clasificador_ia_log` | una fila por clasificación de producción: `tabla_origen`, `fila_id`, sugerencia, `metodo`, `razon`, `confianza`, `veto_aplicado`, costo. Después, la revisión humana (`revisado`, `feedback_correcto`, `feedback_pactivo`). **Es la cola del panel y la memoria del backtest.** |
| `clasificador_ia_reglas` | `tipo='regla'`: texto que va al prompt de Claude. `tipo='correccion'`: el motivo de cada corrección humana. `tipo='veto'`: regex con nombre, con `aplica_a` (rama) y `pactivo_filtro`, editable en `/reglas` **sin deploy** |
| `clasificador_ia_costos` | libro de costos de la API. No se purga nunca |
| `clasificador_ia_backtest` | backtest viejo con API, ya apagado. No se usa |
| `pactivos_extra` | pactivos agregados a mano al catálogo activo (CRUD en `/pactivos-extra`). El worker detecta el cambio y recarga sin reiniciar |

## Catálogo activo (qué pactivos existen hoy)

Lo arman `taxonomia.cargar_taxonomia` y `catalogo_activo.py` en cada arranque y
en cada refresco diario:

```
catálogo activo = pactivos de 0001_td_oc.Base (OC reales, "sagrados")
                ∪ pactivos del diccionario de clasificación que tenga ≥1 cliente ACTIVO
                ∪ pactivos_extra (activos)
                ∪ {Adjunto}                      (meta-pactivo, siempre)
cliente ACTIVO  = compañía de prime con más de 1 usuario vivo (y que no sea la cuenta interna 12345678-5)
```

El **final guard** de la cascada descarta cualquier interés cuyo pactivo no esté
en este catálogo (ver [02-cascada.md](02-cascada.md)).

## Refrescos y relojes

- El worker recarga todo (catálogo, índices, descartes, modelos, sufijos UNSPSC)
  **cada 24 horas**. Si cambian `pactivos_extra` o los vetos, recarga al
  instante: compara un hash en cada ciclo.
- Los containers escriben `datetime.now()` en **UTC**. El MySQL del clásico
  responde en hora de Chile fija (−04). Entre `creado_en` y `NOW()` hay **4 h de
  desfase**.

## Qué fuentes procesa el worker

Las define **`FUENTES_WORKER`** en el `.env` (por defecto
`compra_agil,Licitaciones_diarias`). Todas las fuentes que el sistema conoce
están declaradas **una sola vez** en `fuentes.py`. El panel, el backtest y el
descarte por rubro leen ese registro. Una fuente puede estar registrada (el
panel la muestra, el backtest la mide) sin que el worker la clasifique todavía.
Ver [04-agregar-una-fuente.md](04-agregar-una-fuente.md).
