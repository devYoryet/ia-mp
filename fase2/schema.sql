-- Fase 2 (Device / Servicio técnico) — tablas PROPIAS, todas con prefijo clasificador_f2_.
-- No altera ninguna tabla existente. Se crean en el clásico:
--   python admin.py schema      (con credenciales de administrador, una sola vez)
-- El prefijo no calza con los listados por patrón que ya existen
-- (salud.py usa LIKE 'clasificador_ia_%'; aplicar_schema.py, 'clasificador_ia%').
-- Fechas: NOW() del MySQL (hora de Chile), no datetime.now() del container (UTC).

-- Categorías de la fase 2 (Device / Servicio técnico).
CREATE TABLE IF NOT EXISTS clasificador_f2_categorias (
  id                   INT          NOT NULL AUTO_INCREMENT,
  codigo               VARCHAR(20)  NOT NULL,             -- DEV-GUA, SRV-MAN, ...
  linea                VARCHAR(40)  NOT NULL,             -- 'Device' | 'Servicio técnico'
  nombre               VARCHAR(120) NOT NULL,
  prioridad            INT          NOT NULL,             -- menor = gana si calzan varias
  onu_solo_a_revision  TINYINT(1)   NOT NULL DEFAULT 0,   -- ONU fuerte sin palabra → revisión
  activa               TINYINT(1)   NOT NULL DEFAULT 1,
  creado_por           VARCHAR(80),
  creado_en            DATETIME     NOT NULL,
  actualizado_en       DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_codigo (codigo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Términos (palabras) que incluyen o excluyen. Regex contra texto normalizado (sin tildes).
CREATE TABLE IF NOT EXISTS clasificador_f2_terminos (
  id                INT           NOT NULL AUTO_INCREMENT,
  categoria_codigo  VARCHAR(20)   NOT NULL,
  tipo              VARCHAR(10)   NOT NULL,              -- 'incluye' | 'excluye'
  nombre            VARCHAR(120)  NOT NULL,              -- la palabra del cliente o el nombre de la exclusión
  regex             VARCHAR(2000) NOT NULL,
  contexto_regex    VARCHAR(4000),                       -- sólo 'incluye': contexto exigido (glosa o título)
  orden             INT           NOT NULL DEFAULT 0,    -- del más específico al más genérico
  origen            VARCHAR(20)   NOT NULL DEFAULT 'cliente',  -- 'cliente' | 'sugerido'
  activa            TINYINT(1)    NOT NULL DEFAULT 1,
  creado_por        VARCHAR(80),
  creado_en         DATETIME      NOT NULL,
  actualizado_en    DATETIME      NOT NULL,
  PRIMARY KEY (id),
  KEY idx_cat (categoria_codigo, tipo, activa)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Códigos ONU (UNSPSC) por categoría: exacto o prefijo, con fuerza.
CREATE TABLE IF NOT EXISTS clasificador_f2_onu (
  id                INT          NOT NULL AUTO_INCREMENT,
  categoria_codigo  VARCHAR(20)  NOT NULL,
  codigo            VARCHAR(20)  NOT NULL,               -- 8 dígitos o prefijo (2/4/6...)
  fuerza            VARCHAR(10)  NOT NULL,               -- 'fuerte' | 'confirma'
  nota              VARCHAR(255),
  activa            TINYINT(1)   NOT NULL DEFAULT 1,
  creado_por        VARCHAR(80),
  creado_en         DATETIME     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_cat (categoria_codigo, activa)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Nombre UNSPSC de cada código (de Licitaciones_diarias.Producto_Servicio). Lo refresca el servicio.
CREATE TABLE IF NOT EXISTS clasificador_f2_onu_nombre (
  codigo          VARCHAR(20)  NOT NULL,
  nombre          VARCHAR(255) NOT NULL,
  n               INT          NOT NULL,
  actualizado_en  DATETIME     NOT NULL,
  PRIMARY KEY (codigo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Resultado: una fila por fila origen que la fase 2 reclama.
-- El barrido actualiza los campos automáticos; NUNCA toca decision / revisado_* / motivo.
CREATE TABLE IF NOT EXISTS clasificador_f2_resultado (
  id                 BIGINT        NOT NULL AUTO_INCREMENT,
  tabla_origen       VARCHAR(64)   NOT NULL,
  fila_id            BIGINT        NOT NULL,
  -- copia informativa de la fila origen
  licitacion         VARCHAR(255),
  fecha_publicacion  DATETIME,
  fecha_cierre       DATETIME,
  descripcion        VARCHAR(1000),
  titulo             VARCHAR(300),
  codigo_onu         VARCHAR(20),
  nombre_onu         VARCHAR(255),
  ia_interes         TINYINT,                            -- lo que sugirió la fase 1 (log)
  ia_metodo          VARCHAR(60),
  estado_gestor      TINYINT,                            -- estado en la tabla origen (NULL = sin confirmar)
  clasificador_f1    VARCHAR(80),                        -- quién fijó estado_gestor (persona o 'Bot Eliminado')
  pactivo_f1         VARCHAR(255),                       -- pactivo de la fase 1 (filas 'farma': interés farma que además es Device)
  -- decisión automática de la fase 2
  categoria          VARCHAR(20)   NOT NULL,
  linea              VARCHAR(40)   NOT NULL,
  categoria_nombre   VARCHAR(120)  NOT NULL,
  subcategoria       VARCHAR(120),
  terminos           VARCHAR(500),
  senal              VARCHAR(30)   NOT NULL,             -- ambas | solo_palabra | palabra_debil+onu | solo_onu
  estado_auto        VARCHAR(10)   NOT NULL,             -- verde | revision | farma (ya es interés farma, informativa)
  otras_categorias   VARCHAR(200),
  version_reglas     VARCHAR(16)   NOT NULL,
  vigente            TINYINT(1)    NOT NULL DEFAULT 1,   -- 0 = anulada (pasó a farma / ya no calza)
  motivo_no_vigente  VARCHAR(120),
  -- revisión humana (la escribe el panel, nunca el barrido)
  decision           VARCHAR(10),                        -- aprobado | rechazado
  categoria_final    VARCHAR(20),
  revisado_por       VARCHAR(80),
  revisado_en        DATETIME,
  motivo             TEXT,
  creado_en          DATETIME      NOT NULL,
  actualizado_en     DATETIME      NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_fila (tabla_origen, fila_id),
  KEY idx_cola (vigente, decision, estado_auto, categoria),
  KEY idx_cierre (fecha_cierre),
  KEY idx_creado (creado_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bitácora de cada barrido: qué leyó, qué hizo, cuánto tardó.
CREATE TABLE IF NOT EXISTS clasificador_f2_corridas (
  id              BIGINT        NOT NULL AUTO_INCREMENT,
  tipo            VARCHAR(20)   NOT NULL,                -- rapido | profundo | manual
  dias            INT           NOT NULL,
  version_reglas  VARCHAR(16)   NOT NULL,
  filas_leidas    INT           NOT NULL DEFAULT 0,
  descartes       INT           NOT NULL DEFAULT 0,
  pendientes      INT           NOT NULL DEFAULT 0,
  calzan          INT           NOT NULL DEFAULT 0,
  insertadas      INT           NOT NULL DEFAULT 0,
  actualizadas    INT           NOT NULL DEFAULT 0,
  anuladas        INT           NOT NULL DEFAULT 0,
  reactivadas     INT           NOT NULL DEFAULT 0,
  info            INT           NOT NULL DEFAULT 0,
  segundos        DECIMAL(8,1)  NOT NULL DEFAULT 0,
  error           TEXT,
  creado_en       DATETIME      NOT NULL,
  PRIMARY KEY (id),
  KEY idx_creado (creado_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Auditoría con IA de lo que la fase 2 NO rescató (auditoria_ia.py). Una fila por
-- fila revisada por la IA en un lote. Sólo informa: no cambia ninguna clasificación.
CREATE TABLE IF NOT EXISTS clasificador_f2_auditoria (
  id                 BIGINT        NOT NULL AUTO_INCREMENT,
  lote               VARCHAR(40)   NOT NULL,              -- corrida de auditoría (fecha + lote de la API)
  tabla_origen       VARCHAR(64)   NOT NULL,
  fila_id            BIGINT        NOT NULL,
  grupo              VARCHAR(20)   NOT NULL,              -- cercano | onu_salud | azar
  motivo_grupo       VARCHAR(160),                        -- por qué se eligió para revisar
  licitacion         VARCHAR(255),
  fecha_publicacion  DATETIME,
  descripcion        VARCHAR(1000),
  titulo             VARCHAR(300),
  codigo_onu         VARCHAR(20),
  nombre_onu         VARCHAR(255),
  ia_metodo          VARCHAR(60),                         -- cómo la descartó la fase 1
  clasificador_f1    VARCHAR(80),
  ia_categoria       VARCHAR(20),                         -- lo que propone la IA, o NINGUNA
  ia_confianza       VARCHAR(10),                         -- alta | media | baja
  ia_motivo          VARCHAR(300),
  modelo             VARCHAR(40),
  creado_en          DATETIME      NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_lote_fila (lote, tabla_origen, fila_id),
  KEY idx_lote (lote, ia_categoria)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
