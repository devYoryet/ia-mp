"""Cierre Adjudicadas completo: licitaciones adjudicadas de Mercado Público.

Reemplaza la cadena que corría desde un portátil Windows (RPA con Chrome, SSH,
mysqldump y PowerShell) por Python nativo de este proyecto, con las mismas
conexiones que ya usa el panel:

    clásico  -> config.db_* (MYSQL_HOST, ...)              fuente de verdad
    prime    -> MYSQL_PRIME_*                               lo que ven los clientes
    OC       -> MYSQL_OC_* (127.0.0.1 en gestor_oc)         cruce consulta1/3/5

Módulos:
    bd.py              conexiones y copia de filas entre servidores
    mercadopublico.py  API (listado por día, detalle por código) y acta del portal
    reglas.py          cálculo independiente de resumen y consulta3/consulta5
    validaciones.py    chequeos de solo lectura (semáforos del panel)
    pasos.py           pasos que escriben
El orquestador es cierre_adjudicadas_completo.py (raíz del repo).
"""
