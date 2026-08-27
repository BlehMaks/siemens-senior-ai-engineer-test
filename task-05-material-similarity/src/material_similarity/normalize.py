"""Conservative normalization for technical material descriptions."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_DIMENSION_SEPARATOR = re.compile(r"(?<=\d)\s*[x\N{MULTIPLICATION SIGN}]\s*(?=\d)")
_UNIT = re.compile(
    r"(?<![\w.])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>"
    r"millimet(?:er|re)s?|mm|milliseconds?|ms|volts?|vac|vdc|v|"
    r"amperes?|amps?|a|watts?|w|joules?|j|celsius|cel|°c)\b"
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

    normalized = _WHITESPACE.sub(" ", description.casefold()).strip()
    normalized = _UNIT.sub(_canonicalize_unit, normalized)
    normalized = _DIMENSION_SEPARATOR.sub("x", normalized)
    return _VOLTAGE_MODE.sub(lambda match: f"v{match.group('mode')}", normalized)


def _canonicalize_unit(match: re.Match[str]) -> str:
    return f"{match.group('number')}{_CANONICAL_UNIT[match.group('unit')]}"
