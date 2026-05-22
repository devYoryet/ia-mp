-- Tabla de auditoría del clasificador IA.
-- Crear en la base licitaciones_diarias_total_farma del servidor clásico:
--   mysql -h 10.0.0.69 -u root -p licitaciones_diarias_total_farma < schema/auditoria.sql

CREATE TABLE IF NOT EXISTS clasificador_ia_log (
  id                    BIGINT       NOT NULL AUTO_INCREMENT,
  tabla_origen          VARCHAR(64)  NOT NULL,            -- compra_agil | Licitaciones_diarias
  fila_id               BIGINT       NOT NULL,            -- id de la fila clasificada
  descripcion           VARCHAR(1000),
  interes_sugerido      TINYINT,                          -- 1 = de interés, 0 = descartar
  pactivo_sugerido      VARCHAR(255),
  composicion_sugerida  VARCHAR(255),
  presentacion_sugerida VARCHAR(255),
  metodo                VARCHAR(40),                      -- regla_diccionario | claude
  confianza             DECIMAL(4,3),
  razon                 TEXT,
  pactivo_nuevo         VARCHAR(255),                     -- pactivo fuera de lista, propuesto por la IA
  modelo                VARCHAR(60),
  prompt_version        VARCHAR(20),
  tokens_in             INT,
  tokens_out            INT,
  cache_read_tok        INT,
  cache_write_tok       INT,
  costo_usd             DECIMAL(10,6),
  creado_en             DATETIME     NOT NULL,
  -- feedback humano (panel de revisión)
  revisado              TINYINT(1)   NOT NULL DEFAULT 0,
  revisado_por          VARCHAR(80),
  revisado_en           DATETIME,
  feedback_correcto     TINYINT(1),                       -- 1 = IA acertó, 0 = se corrigió
  feedback_pactivo      VARCHAR(255),                     -- pactivo correcto si se corrigió
  feedback_notas        TEXT,
  PRIMARY KEY (id),
  KEY idx_tabla_fila (tabla_origen, fila_id),
  KEY idx_revisado (revisado, creado_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Tabla de BACKTEST (modo test): compara la clasificación de la IA contra la
-- que ya dejó una persona. NO se escribe nada en compra_agil en este modo.
CREATE TABLE IF NOT EXISTS clasificador_ia_backtest (
  id                   BIGINT       NOT NULL AUTO_INCREMENT,
  tabla_origen         VARCHAR(64)  NOT NULL,
  fila_id              BIGINT       NOT NULL,
  descripcion          VARCHAR(1000),
  -- lo que dejó la persona
  humano_estado_gestor TINYINT,                 -- 1 = de interés, 0 = descartada
  humano_pactivo       VARCHAR(255),
  humano_composicion   VARCHAR(255),
  humano_presentacion  VARCHAR(255),
  -- lo que predijo la IA
  ia_interes           TINYINT,
  ia_pactivo           VARCHAR(255),
  ia_composicion       VARCHAR(255),
  ia_presentacion      VARCHAR(255),
  ia_confianza         DECIMAL(4,3),
  ia_metodo            VARCHAR(40),
  ia_razon             TEXT,
  ia_pactivo_nuevo     VARCHAR(255),            -- pactivo fuera de la lista, propuesto por la IA
  -- comparación
  coincide_interes      TINYINT(1),             -- IA acertó el interés
  coincide_pactivo      TINYINT(1),             -- IA acertó el pactivo
  coincide_composicion  TINYINT(1),             -- IA acertó la composición
  coincide_presentacion TINYINT(1),             -- IA acertó la presentación
  modelo               VARCHAR(60),
  tokens_in            INT,
  tokens_out           INT,
  cache_read_tok       INT,
  cache_write_tok      INT,
  costo_usd            DECIMAL(10,6),
  creado_en            DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_fila (tabla_origen, fila_id),
  KEY idx_creado (creado_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Reglas de negocio y correcciones que el equipo agrega desde el panel.
-- Se inyectan al system prompt: las correcciones ('errores a no repetir')
-- van en una sección aparte y con MÁS peso que las reglas generales.
CREATE TABLE IF NOT EXISTS clasificador_ia_reglas (
  id             BIGINT       NOT NULL AUTO_INCREMENT,
  tipo           ENUM('regla','correccion') NOT NULL,  -- regla=guia general / correccion=error puntual
  texto          TEXT         NOT NULL,                -- la regla, o el "por qué" obligatorio de la corrección
  fila_ref       VARCHAR(120),                         -- tabla#fila_id de origen (si es corrección)
  pactivo_malo   VARCHAR(255),
  pactivo_bueno  VARCHAR(255),
  creado_por     VARCHAR(80),
  creado_en      DATETIME     NOT NULL,
  activa         TINYINT(1)   NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  KEY idx_activa (activa, tipo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Libro de costos de la API de Anthropic. Una fila por llamada a Claude.
-- NUNCA se trunca: es el control de presupuesto real (gastado vs USD 350/mes).
CREATE TABLE IF NOT EXISTS clasificador_ia_costos (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  creado_en    DATETIME     NOT NULL,
  contexto     VARCHAR(20),                  -- test, produccion o ajuste
  modelo       VARCHAR(60),
  tokens_in    INT,
  tokens_out   INT,
  cache_read   INT,
  cache_write  INT,
  costo_usd    DECIMAL(10,6) NOT NULL,
  nota         VARCHAR(255),
  PRIMARY KEY (id),
  KEY idx_creado (creado_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
