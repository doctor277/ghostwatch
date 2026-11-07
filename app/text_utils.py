from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import fields, is_dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

UNSERIALIZABLE = "[UNSERIALIZABLE]"
TRUNCATED = "[TRUNCATED]"
TRUNCATED_SUFFIX = f" {TRUNCATED}"
MAX_STRUCTURE_DEPTH = 32
MAX_STRUCTURE_NODES = 10_000
MAX_INTEGER_BITS = 1024
MAX_EVIDENCE_BYTES = 16 * 1024
# Only these reach the converter: it would drain iterators inside anything else.
LEAF_TYPES = (date, time, timedelta, Decimal, UUID, Path, re.Pattern, bytes)


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
    # Escaping only grows text, so nothing past the limit can reach the result.
    text = safe_text(value[: limit + 1])
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX


def bounded_identifier(value: Any, limit: int) -> Any:
    """Keep a long identifier stable, because cutting it would break relationships."""
    if not isinstance(value, str):
        return value
    text = safe_text(value)
    if len(text) <= limit:
        return text
    return f"truncated-sha256-{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class _Budget:
    """Work allowed for one structure, so shared references cannot multiply it."""

    def __init__(self) -> None:
        self.nodes = MAX_STRUCTURE_NODES
        self.text = MAX_EVIDENCE_BYTES

    def fits(self, size: int) -> bool:
        if size <= self.text:
            return True
        self.text = -1
        self.nodes = 0
        return False

    def spend(self, size: int) -> bool:
        if not self.fits(size):
            return False
        self.text -= size
        return True

    @property
    def exceeded(self) -> bool:
        return self.text < 0


def _plain_key(key: Any) -> bool:
    if isinstance(key, int) and not isinstance(key, bool):
        return key.bit_length() <= MAX_INTEGER_BITS
    return key is None or isinstance(key, str | bool | float)


def _record_fields(value: Any) -> dict[str, Any] | None:
    """Read a record one level at a time, because deep conversion would drain iterators."""
    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in type(value).model_fields}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return None


def _sequence_items(value: Any, nested: tuple[int, ...], budget: _Budget) -> list[Any]:
    items: list[Any] = []
    for item in value:
        if budget.nodes <= 0:
            items.append(TRUNCATED)
            break
        items.append(_structure(item, nested, budget))
    return items


def _set_items(value: Any, nested: tuple[int, ...], budget: _Budget) -> Any:
    if len(value) > budget.nodes:
        return TRUNCATED
    # Sorted on the encoded form, because set order varies between processes.
    return sorted((_structure(item, nested, budget) for item in value), key=json.dumps)


def _mapping_items(value: Mapping[Any, Any], nested: tuple[int, ...], budget: _Budget) -> Any:
    for key in value:
        if budget.nodes <= 0:
            return TRUNCATED
        budget.nodes -= 1
        if not _plain_key(key):
            return UNSERIALIZABLE
    mapping: dict[Any, Any] = {}
    for key, item in value.items():
        if budget.nodes <= 0 or (isinstance(key, str) and not budget.spend(len(key))):
            mapping[TRUNCATED] = TRUNCATED
            break
        mapping[escape_surrogates(key)] = _structure(item, nested, budget)
    return mapping


def _convert(value: Any, ancestors: tuple[int, ...], budget: _Budget) -> Any:
    if isinstance(value, str):
        return safe_text(value) if budget.spend(len(value)) else TRUNCATED
    if value is None or isinstance(value, bool | float):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= MAX_INTEGER_BITS else UNSERIALIZABLE
    if isinstance(value, Iterator):
        return UNSERIALIZABLE
    if isinstance(value, Enum):
        return _structure(value.value, ancestors, budget)
    if isinstance(value, bytes) and not budget.fits(len(value)):
        return TRUNCATED
    if isinstance(value, LEAF_TYPES):
        return _structure(to_jsonable_python(value), ancestors, budget)
    record = _record_fields(value)
    if record is None and not isinstance(value, Mapping | list | tuple | set | frozenset):
        return UNSERIALIZABLE
    if id(value) in ancestors:
        return UNSERIALIZABLE
    if len(ancestors) >= MAX_STRUCTURE_DEPTH:
        return TRUNCATED
    nested = (*ancestors, id(value))
    if record is not None:
        return _mapping_items(record, nested, budget)
    if isinstance(value, Mapping):
        return _mapping_items(value, nested, budget)
    if isinstance(value, set | frozenset):
        return _set_items(value, nested, budget)
    return _sequence_items(value, nested, budget)


def _structure(value: Any, ancestors: tuple[int, ...], budget: _Budget) -> Any:
    """Return JSON-compatible data, marking values it cannot represent faithfully."""
    if budget.nodes <= 0:
        return TRUNCATED
    budget.nodes -= 1
    try:
        return _convert(value, ancestors, budget)
    except Exception:
        return UNSERIALIZABLE


def safe_evidence(value: Any) -> Any:
    """Return bounded evidence, replacing all of it rather than keeping a partial view."""
    budget = _Budget()
    evidence = _structure(value, (), budget)
    if budget.exceeded or (
        len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) > MAX_EVIDENCE_BYTES
    ):
        return {TRUNCATED: f"Evidence exceeded {MAX_EVIDENCE_BYTES} bytes"}
    if isinstance(value, Mapping) and not isinstance(evidence, dict):
        return {evidence: evidence}
    return evidence
