"""Etapa 2 — clasificación con Claude (API de Anthropic).

Usa prompt caching: la lista controlada va en un bloque estable del system prompt,
así se cobra ~0.1x en cada llamada posterior. Salida estructurada con messages.parse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

import costos
from config import config
from taxonomia import Taxonomia

log = logging.getLogger("clasificador")

PROMPT_VERSION = "v3"

SISTEMA_BASE = """Eres un clasificador experto de oportunidades de compra pública \
(mercadopublico.cl) para una empresa que distribuye medicamentos e insumos médicos.

Analiza el TÍTULO, la DESCRIPCIÓN y el campo VINCULOS (enlaces y detalle del \
producto) de cada compra ágil o licitación, y decide:

1. interes: 1 si corresponde a un medicamento o insumo médico que la empresa \
podría vender; 0 si es de otro rubro (papelería, alimentos, servicios, \
construcción, mobiliario, etc.).
2. Si interes = 1: asignar pactivo, composicion y presentacion ELIGIENDO \
EXACTAMENTE de las listas controladas entregadas más abajo. NUNCA inventes un \
valor fuera de esas listas. El **pactivo es la CATEGORÍA del catálogo** que \
identifica el producto: para un medicamento, su principio activo (ej.: para \
"enjuague bucal con fluoruro de sodio" el pactivo es "Fluoruro Sodio", no \
"Enjuague Bucal"); para un dispositivo o insumo SIN principio activo, la \
categoría del producto del catálogo (ej.: válvula hemostática, apósito, gel \
conductor). Si el producto combina varios principios activos y existe un \
pactivo combinado en la lista (ej. "Bisoprolol-Amlodipino"), usa el combinado.
3. MARCAS COMERCIALES: la glosa suele traer el NOMBRE DE MARCA del producto en \
vez del principio activo (ej. "Bramedil", "Ziagen"). Si reconoces la marca con \
CERTEZA, asigna el pactivo del catálogo que le corresponde. Si NO la reconoces \
con certeza, NO adivines ni inventes qué contiene — baja la confianza (< 0.6) \
para que un humano lo revise. Una confianza baja es mejor que una clasificación \
inventada.
4. DETECCIÓN DE PACTIVO NUEVO: si el texto describe con claridad un medicamento \
o insumo médico real, pero NINGÚN pactivo de la lista controlada le corresponde, \
marca pactivo_fuera_de_lista = 1 y escribe en pactivo_propuesto el nombre del \
pactivo que debería existir. Es un hallazgo para que un humano lo agregue a la \
lista. En ese caso deja pactivo en null.

Reglas:
- DESCRIPCIONES POBRES: si la descripción es un código, está incompleta o solo \
remite a un anexo/adjunto sin detallar el producto, NO la descartes con \
seguridad. Si no puedes determinar qué es, pon confianza < 0.6 para que un \
humano la revise — descartar un insumo médico real por una descripción pobre es \
peor que revisarla.
- COMPOSICIÓN: EXTRAE la dosis o concentración de la descripción/VINCULOS siempre \
que aparezca — mg, ml, %, UI, o combinaciones tipo «5-10mg» o «40-12,5mg». \
PRESENTACIÓN: ampolla, comprimido, frasco, crema, etc. Solo si realmente no \
figuran, deja la composición o la presentación en «Sin Cla» (SIN s al final): es \
el comodín para que la oportunidad igual le llegue al cliente. Nunca las dejes \
vacías en una fila de interés.
- Ante cualquier duda, baja la confianza (< 0.7): es preferible que un humano \
revise a clasificar mal.
- 'razon' debe ser breve (una frase) y explicar la decisión.
- Responde solo con los campos estructurados solicitados.
"""


class Clasificacion(BaseModel):
    """Salida estructurada del clasificador."""

    interes: int = Field(description="1 = de interés (medicamento/insumo), 0 = descartar")
    pactivo: Optional[str] = Field(
        default=None, description="Pactivo EXACTO de la lista controlada, o null"
    )
    composicion: Optional[str] = Field(default=None, description="Composición normalizada, o null")
    presentacion: Optional[str] = Field(default=None, description="Presentación de la lista, o null")
    confianza: float = Field(description="Confianza de 0.0 a 1.0")
    razon: str = Field(description="Justificación breve, una frase")
    pactivo_fuera_de_lista: int = Field(
        default=0,
        description="1 si parece un medicamento/insumo real cuyo pactivo NO está "
        "en la lista controlada",
    )
    pactivo_propuesto: Optional[str] = Field(
        default=None, description="Nombre del pactivo nuevo propuesto (si fuera de lista)"
    )


@dataclass
class Uso:
    """Tokens y costo en USD de una llamada a la API."""

    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    costo_usd: float


_cliente: Optional[anthropic.Anthropic] = None


def cliente() -> anthropic.Anthropic:
    global _cliente
    if _cliente is None:
        if not config.anthropic_api_key:
            raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
        _cliente = anthropic.Anthropic(api_key=config.anthropic_api_key)
    return _cliente


def clasificar(
    descripcion: str,
    titulo: str,
    vinculos: str,
    taxonomia: Taxonomia,
    ejemplos: str = "",
    candidatos: "list[str] | None" = None,
) -> "tuple[Clasificacion, Uso]":
    """Clasifica una fila con Claude. La lista controlada (y los ejemplos
    validados, si hay) se cachean entre llamadas con prompt caching.
    `candidatos` (opcional) son los pactivos del catálogo cuyas palabras
    aparecen en la descripción — se pasan como PISTA en el mensaje de usuario,
    NO acotan el catálogo (sigue completo en el system prompt cacheado).
    Devuelve la clasificación y el costo/tokens de la llamada."""
    sistema = [
        {"type": "text", "text": SISTEMA_BASE},
        {"type": "text", "text": taxonomia.texto_para_prompt()},
    ]
    if ejemplos:
        sistema.append({"type": "text", "text": ejemplos})
    # el último bloque del prefijo estable lleva el breakpoint de caché
    sistema[-1]["cache_control"] = {"type": "ephemeral"}

    # Pista: pactivos del catálogo cuyas palabras aparecen en la descripción.
    # Va en el mensaje (no cacheable: cambia por fila). Claude SIGUE pudiendo
    # elegir cualquier pactivo del catálogo completo — esto es solo ayuda.
    pista = ""
    if candidatos:
        pista = (
            "Pistas: estos pactivos del catálogo contienen palabras de la "
            "descripción — considéralos PRIMERO. Si ninguno encaja con lo que "
            "describe el texto, elige cualquier otro del catálogo completo.\n"
            + "\n".join(f"- {c}" for c in candidatos) + "\n\n"
        )
    peticion = dict(
        model=config.modelo,
        max_tokens=2500,
        system=sistema,
        messages=[
            {
                "role": "user",
                "content": pista + f"Título: {titulo or '(sin título)'}\n"
                f"Descripción: {descripcion or '(sin descripción)'}\n"
                f"VINCULOS: {(vinculos or '(sin vínculos)')[:800]}",
            }
        ],
        output_format=Clasificacion,
    )
    # El "adaptive thinking" solo lo aceptan los modelos Opus; Haiku/Sonnet dan
    # error 400. Para esos se llama sin thinking — basta para esta tarea (en
    # Opus el thinking generaba apenas ~84 tokens de salida en promedio).
    if "opus" in config.modelo:
        peticion["thinking"] = {"type": "adaptive"}
    respuesta = cliente().messages.parse(**peticion)
    u = respuesta.usage
    t_in = u.input_tokens or 0
    t_out = u.output_tokens or 0
    c_write = u.cache_creation_input_tokens or 0
    c_read = u.cache_read_input_tokens or 0
    uso = Uso(
        tokens_in=t_in,
        tokens_out=t_out,
        cache_read=c_read,
        cache_write=c_write,
        costo_usd=costos.costo_usd(config.modelo, t_in, t_out, c_write, c_read),
    )
    log.debug("tokens in=%s out=%s cache_read=%s costo=$%.5f",
              t_in, t_out, c_read, uso.costo_usd)
    return respuesta.parsed_output, uso
