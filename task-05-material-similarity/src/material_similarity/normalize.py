"""Conservative normalization for technical material descriptions."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_DIMENSION_SEPARATOR = re.compile(
    r"(?:(?<=\d)|(?<=mm))\s*[x\N{MULTIPLICATION SIGN}]\s*(?=\d)"
)
_TECHNICAL_QUANTITY = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[^\W\d_]+)")
_UNIT = re.compile(
    r"(?<![\w.])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>"
    r"(?i:millimet(?:er|re)s?|milliseconds?|volts?|vac|vdc|v|"
    r"amperes?|amps?|a|watts?|w|joules?|j|celsius|cel|°c)|mm|ms)\b"
)
_VOLTAGE_MODE = re.compile(r"(?<=\d)v\s+(?P<mode>ac|dc)\b")

_CANONICAL_UNIT = {
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "mm": "mm",
    "millisecond": "ms",
    "milliseconds": "ms",
    "ms": "ms",
    "volt": "v",
    "volts": "v",
    "v": "v",
    "vac": "vac",
    "vdc": "vdc",
    "ampere": "a",
    "amperes": "a",
    "amp": "a",
    "amps": "a",
    "a": "a",
    "watt": "w",
    "watts": "w",
    "w": "w",
    "joule": "j",
    "joules": "j",
    "j": "j",
    "celsius": "cel",
    "cel": "cel",
    "°c": "cel",
}


def normalize_description(description: str) -> str:
    """Canonicalize safe variants without deleting technical evidence."""

    known_units = _UNIT.sub(_canonicalize_unit, description)
    normalized = _WHITESPACE.sub(" ", _casefold_description(known_units)).strip()
    normalized = _DIMENSION_SEPARATOR.sub("x", normalized)
    return _VOLTAGE_MODE.sub(lambda match: f"v{match.group('mode')}", normalized)


def _canonicalize_unit(match: re.Match[str]) -> str:
    return f"{match.group('number')}{_CANONICAL_UNIT[match.group('unit').casefold()]}"


def _casefold_description(description: str) -> str:
    """Case-fold prose while retaining case-sensitive SI quantities."""

    parts: list[str] = []
    previous_end = 0
    for match in _TECHNICAL_QUANTITY.finditer(description):
        parts.append(description[previous_end : match.start()].casefold())
        parts.append(f"{match.group('number')}{match.group('unit')}")
        previous_end = match.end()
    parts.append(description[previous_end:].casefold())
    return "".join(parts)
