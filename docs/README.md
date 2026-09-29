# Documentación del Clasificador IA

Cómo funciona hoy la clasificación automática de oportunidades de
mercadopublico.cl, fuente por fuente. `README.md` en la raíz está desactualizado:
la verdad está en el código y en estos documentos.

| documento | qué responde |
|---|---|
| [01-arquitectura.md](01-arquitectura.md) | Recorrido de una fila desde el scraper hasta el cliente: containers, tablas, quién escribe qué y cuándo |
| [02-cascada.md](02-cascada.md) | Las etapas de la cascada en orden: qué lee cada una, de dónde sale su conocimiento y cómo falla |
| [03-fuentes.md](03-fuentes.md) | Cada fuente (compra ágil, licitaciones, cotizaciones, y lo relevado de trato directo y consulta al mercado): tabla, columnas, scraper, cruce a clientes y rarezas |
| [04-agregar-una-fuente.md](04-agregar-una-fuente.md) | Lista de pasos para sumar una fuente nueva sin dejar huecos. Salió de hacerlo con cotizaciones |
| [05-medicion.md](05-medicion.md) | Cómo medir un cambio sin gastar API: backtest, funnel y reglas de evidencia |

Convención: cada afirmación numérica dice si fue **medida** (consulta corrida,
con fecha) o **inferida**.
