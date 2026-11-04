from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

from pydantic_core import to_jsonable_python

UNSERIALIZABLE = "[UNSERIALIZABLE]"
TRUNCATED = "[TRUNCATED]"
MAX_STRUCTURE_DEPTH = 32
MAX_STRUCTURE_NODES = 10_000
MAX_INTEGER_BITS = 1024
MAX_EVIDENCE_BYTES = 16 * 1024


def safe_text(value: object) -> str:
    """Escape lone surrogates, which Windows names allow but UTF-8 consumers reject."""
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def escape_surrogates(value: Any) -> Any:
    return safe_text(value) if isinstance(value, str) else value


def bounded_text(value: Any, limit: int) -> Any:
    """Escape text and cut it to the limit, marking any cut."""
    if not isinstance(value, str):
        return value
    text = safe_text(value)
    if len(text) <= limit:
        return text
    suffix = f" {TRUNCATED}"
    return text[: limit - len(suffix)] + suffix


def bounded_identifier(value: Any, limit: int) -> Any:
    """Keep a long identifier stable, because cutting it would break relationships."""
    if not isinstance(value, str):
        return value
    text = safe_text(value)
    if len(text) <= limit:
        return text
    return f"truncated-sha256-{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _plain_key(key: Any) -> bool:
    if isinstance(key, int) and not isinstance(key, bool):
        return key.bit_length() <= MAX_INTEGER_BITS
    return key is None or isinstance(key, str | bool | float)


def safe_structure(
    value: Any, _ancestors: tuple[int, ...] = (), _budget: list[int] | None = None
) -> Any:
    """Return JSON-compatible data, marking values it cannot represent faithfully."""
    budget = [MAX_STRUCTURE_NODES] if _budget is None else _budget
    if budget[0] <= 0:
        return TRUNCATED
    budget[0] -= 1
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, bool | float):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= MAX_INTEGER_BITS else UNSERIALIZABLE
    if isinstance(value, Iterator):
        return UNSERIALIZABLE
    if isinstance(value, Mapping | list | tuple):
        if id(value) in _ancestors:
            return UNSERIALIZABLE
        if len(_ancestors) >= MAX_STRUCTURE_DEPTH:
            return TRUNCATED
        nested = (*_ancestors, id(value))
        if not isinstance(value, Mapping):
            items: list[Any] = []
            for item in value:
                if budget[0] <= 0:
                    items.append(TRUNCATED)
                    break
                items.append(safe_structure(item, nested, budget))
            return items
        if not all(_plain_key(key) for key in value):
            return UNSERIALIZABLE
        mapping: dict[Any, Any] = {}
        for key, item in value.items():
            if budget[0] <= 0:
                mapping[TRUNCATED] = TRUNCATED
                break
            mapping[escape_surrogates(key)] = safe_structure(item, nested, budget)
        return mapping
    try:
        return safe_structure(to_jsonable_python(value), _ancestors, budget)
    except Exception:
        return UNSERIALIZABLE


def safe_evidence(value: Any) -> Any:
    """Return bounded evidence, replacing all of it rather than keeping a partial view."""
    evidence = safe_structure(value)
    if len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        return {TRUNCATED: f"Evidence exceeded {MAX_EVIDENCE_BYTES} bytes"}
    return evidence
