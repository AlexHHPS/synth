"""Specificity of a minutes point (0 generic points). Shared by the worker and the evaluation."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

MONTHS = "enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre"
WEEKDAYS = "lunes martes miercoles jueves viernes sabado domingo"
CONCRETE = re.compile(rf"\d|\b({MONTHS.replace(' ', '|')}|{WEEKDAYS.replace(' ', '|')})\b|\b(porque|ya que|debido a|para que)\b|[«\"]")
GENERIC = re.compile(r"\b(se (discutieron|hablo de|trataron|comentaron) (varios|diversos|distintos) (temas|aspectos|puntos)"
                     r"|situacion general|en general|aspectos importantes|temas importantes)\b")


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def count(term: str, text: str) -> int:
    return len(re.findall(rf"(?<![\w-]){re.escape(normalize(term))}(?![\w-])", normalize(text)))


def is_generic(point: str, concrete_terms: Iterable[str] = ()) -> bool:
    plain = normalize(point)
    if GENERIC.search(plain):
        return True
    if CONCRETE.search(plain) or any(count(t, point) for t in concrete_terms):
        return False
    words = re.findall(r"[\w-]+", point)
    proper = [t for t in words[1:] if t[:1].isupper() or (len(t) > 1 and t.isupper())]
    return not proper
