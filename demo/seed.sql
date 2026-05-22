-- Datos de EJEMPLO, solo para revisar el frontend localmente (compose-demo.yml).
-- No tiene nada que ver con producción.

INSERT INTO clasificador_ia_backtest
  (tabla_origen, fila_id, descripcion, humano_estado_gestor, humano_pactivo,
   humano_composicion, humano_presentacion, ia_interes, ia_pactivo, ia_composicion,
   ia_presentacion, ia_confianza, ia_metodo, ia_razon, ia_pactivo_nuevo,
   coincide_interes, coincide_pactivo, coincide_composicion, coincide_presentacion,
   modelo, tokens_in, tokens_out, cache_read_tok, cache_write_tok, costo_usd, creado_en)
VALUES
  ('compra_agil',17390736,'CLORHEXIDINA 0,12% COLUTORIO, FRASCO 120 ML',1,'Clorhexidina','0,12%','Frasco',1,'Clorhexidina','0,12%','Frasco',0.920,'regla_diccionario','Coincidencia exacta del pactivo Clorhexidina.',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 55 MINUTE),
  ('compra_agil',17390740,'PARACETAMOL 500 MG COMPRIMIDOS',1,'Paracetamol','500mg','Comprimido',1,'Paracetamol','500mg','Comprimido',0.990,'historico','Descripción idéntica ya clasificada por una persona (7x).',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 52 MINUTE),
  ('compra_agil',17390755,'GUANTES DE NITRILO TALLA M CAJA X100',1,'Guante','Sin Cla','Caja',1,'Guante','Sin Cla','Caja',0.920,'regla_diccionario','Coincidencia exacta del pactivo Guante.',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 50 MINUTE),
  ('compra_agil',17390761,'Servicio de capacitación en manejo de drones',0,NULL,NULL,NULL,0,NULL,NULL,NULL,0.980,'claude','Servicio de capacitación, no es medicamento ni insumo médico.',NULL,1,NULL,NULL,NULL,'claude-opus-4-7',95,61,28862,0,0.017800,NOW()-INTERVAL 47 MINUTE),
  ('compra_agil',17390770,'SOLUCION FISIOLOGICA CLORURO DE SODIO 0,9% MATRAZ 1000 ML',1,'Suero Fisiologico','0,9%','Matraz',1,'Suero Fisiologico','0,9%','Matraz',0.900,'claude','Cloruro de sodio 0,9% en matraz corresponde a suero fisiológico.',NULL,1,1,1,1,'claude-opus-4-7',94,102,28862,0,0.017450,NOW()-INTERVAL 44 MINUTE),
  ('compra_agil',17390781,'IBUPROFENO SUSPENSION ORAL 200 MG/5 ML FRASCO',1,'Ibuprofeno','200mg/5ml','Frasco',1,'Ibuprofeno','200mg/5ml','Frasco',0.970,'historico','Descripción idéntica ya clasificada por una persona (4x).',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 40 MINUTE),
  ('compra_agil',17390799,'APOSITO TRANSPARENTE ADHESIVO ESTERIL 10X12 CM',1,'aposito','Sin Clas','Sin Clas',1,'aposito','Sin Cla','Sin Cla',0.880,'claude','Apósito adhesivo estéril, insumo de curación.',NULL,1,1,0,0,'claude-opus-4-7',101,88,28862,0,0.017600,NOW()-INTERVAL 36 MINUTE),
  ('compra_agil',17390812,'METFORMINA CLORHIDRATO 850 MG COMPRIMIDOS',1,'Metformina','850mg','Comprimido',1,'Metformina','850mg','Comprimido',0.960,'regla_diccionario','Coincidencia exacta del pactivo Metformina.',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 31 MINUTE),
  ('Licitaciones_diarias',452881519,'OXIDO DE ZINC, FRASCO DE 60 GRS',1,'Oxido de Zinc','Sin Cla','Frasco',1,'Oxido de Zinc','Sin Cla','Frasco',0.890,'claude','Óxido de zinc en frasco, insumo dermatológico.',NULL,1,1,1,1,'claude-opus-4-7',88,79,28862,0,0.017200,NOW()-INTERVAL 27 MINUTE),
  ('Licitaciones_diarias',452881540,'CLORHEXIDINA BIDON DE 3.8 LITROS',1,'Clorhexidina','Sin Cla','Bidon',1,'Clorhexidina','Sin Cla','Bidon',0.920,'regla_diccionario','Coincidencia exacta del pactivo Clorhexidina.',NULL,1,1,1,1,'claude-opus-4-7',0,0,0,0,0.000000,NOW()-INTERVAL 22 MINUTE),
  ('Licitaciones_diarias',452881587,'Neumáticos de invierno medidas 255/50 R20',0,NULL,NULL,NULL,0,NULL,NULL,NULL,0.990,'claude','Neumáticos para vehículos, no es del rubro médico.',NULL,1,NULL,NULL,NULL,'claude-opus-4-7',83,55,28862,0,0.016900,NOW()-INTERVAL 17 MINUTE),
  ('Licitaciones_diarias',452881612,'RELUGOLIX 40 MG / NORETISTERONA / ESTRADIOL COMPRIMIDOS',1,'Noretisterona-Estradiol','Sin Cla','Comprimido',1,'Relugolix-Noretisterona-Estradiol',NULL,'Comprimido',0.550,'claude','Combinación con Relugolix; el pactivo exacto no figura en la lista.','Relugolix-Noretisterona-Estradiol',1,0,0,1,'claude-opus-4-7',112,140,28862,0,0.020100,NOW()-INTERVAL 9 MINUTE),
  ('compra_agil',17390840,'Adquisición de material de oficina y papelería',0,NULL,NULL,NULL,1,'Sin Cla',NULL,NULL,0.610,'claude','Mención ambigua; podría incluir insumos — requiere revisión.',NULL,0,NULL,NULL,NULL,'claude-opus-4-7',77,96,28862,0,0.017900,NOW()-INTERVAL 4 MINUTE);

INSERT INTO clasificador_ia_log
  (tabla_origen, fila_id, descripcion, interes_sugerido, pactivo_sugerido,
   composicion_sugerida, presentacion_sugerida, metodo, confianza, razon,
   pactivo_nuevo, modelo, prompt_version, costo_usd, creado_en, revisado,
   revisado_por, revisado_en, feedback_correcto)
VALUES
  ('compra_agil',17391001,'AMOXICILINA 500 MG CAPSULAS',1,'Amoxicilina','500mg','Capsula','regla_diccionario',0.920,'Coincidencia exacta del pactivo Amoxicilina.',NULL,'claude-opus-4-7','v1',0.000000,NOW()-INTERVAL 30 MINUTE,0,NULL,NULL,NULL),
  ('compra_agil',17391005,'JERINGA DESECHABLE 5 ML CAJA X100',1,'Jeringa','5ml','Caja','claude',0.870,'Jeringa desechable, insumo médico.',NULL,'claude-opus-4-7','v1',0.017300,NOW()-INTERVAL 25 MINUTE,0,NULL,NULL,NULL),
  ('compra_agil',17391010,'TRASTUZUMAB DERUXTECAN 100 MG VIAL',1,NULL,NULL,'Vial','claude',0.520,'Anticuerpo oncológico; pactivo no presente en la lista controlada.','Trastuzumab Deruxtecan','claude-opus-4-7','v1',0.021000,NOW()-INTERVAL 20 MINUTE,0,NULL,NULL,NULL),
  ('Licitaciones_diarias',452882001,'SUERO GLUCOSADO 5% MATRAZ 500 ML',1,'Suero glucosado','5%','Matraz','historico',0.990,'Descripción idéntica ya clasificada por una persona (6x).',NULL,'claude-opus-4-7','v1',0.000000,NOW()-INTERVAL 14 MINUTE,0,NULL,NULL,NULL),
  ('Licitaciones_diarias',452882010,'Servicio de mantención de ascensores',0,NULL,NULL,NULL,'claude',0.960,'Servicio de mantención, fuera del rubro médico.',NULL,'claude-opus-4-7','v1',0.016800,NOW()-INTERVAL 7 MINUTE,0,NULL,NULL,NULL),
  ('compra_agil',17390640,'VENDA ELASTICA 10 CM X 4 M',1,'Venda Elastica','Sin Cla','Sin Cla','regla_diccionario',0.920,'Coincidencia exacta del pactivo Venda Elastica.',NULL,'claude-opus-4-7','v1',0.000000,NOW()-INTERVAL 3 HOUR,1,'Evelyn Muñoz',NOW()-INTERVAL 2 HOUR,1),
  ('compra_agil',17390655,'TONER PARA IMPRESORA HP LASERJET',0,NULL,NULL,NULL,'claude',0.940,'Insumo de impresión, fuera del rubro médico.',NULL,'claude-opus-4-7','v1',0.017100,NOW()-INTERVAL 3 HOUR,1,'Evelyn Muñoz',NOW()-INTERVAL 2 HOUR,1);
