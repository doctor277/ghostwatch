from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python

UNSERIALIZABLE = "[UNSERIALIZABLE]"
MAX_STRUCTURE_DEPTH = 32


def safe_text(value: object) -> str:
    """Escape lone surrogates, which Windows names allow but UTF-8 consumers reject."""
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def escape_surrogates(value: Any) -> Any:
    return safe_text(value) if isinstance(value, str) else value


def safe_structure(value: Any, _ancestors: tuple[int, ...] = ()) -> Any:
    """Return JSON-compatible data, marking values it cannot represent faithfully."""
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping | list | tuple):
        if id(value) in _ancestors or len(_ancestors) >= MAX_STRUCTURE_DEPTH:
            return UNSERIALIZABLE
        nested = (*_ancestors, id(value))
        if not isinstance(value, Mapping):
            return [safe_structure(item, nested) for item in value]
        if not all(isinstance(key, str | bool | int | float) or key is None for key in value):
            return UNSERIALIZABLE
        return {
            escape_surrogates(key): safe_structure(item, nested) for key, item in value.items()
        }
    try:
        return safe_structure(to_jsonable_python(value), _ancestors)
    except Exception:
        return UNSERIALIZABLE
