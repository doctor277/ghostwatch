from __future__ import annotations

import ipaddress
import json
import tracemalloc
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from app.models import Alert
from app.text_utils import MAX_EVIDENCE_BYTES, MAX_STRUCTURE_NODES, safe_evidence

TRUNCATED = "[TRUNCATED]"
UNSERIALIZABLE = "[UNSERIALIZABLE]"
OVER_LIMIT = {TRUNCATED: f"Evidence exceeded {MAX_EVIDENCE_BYTES} bytes"}
# Far above any legitimate walk, and small enough that a broken guard fails quickly.
ITERATION_GUARD = 5_000


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


class GuardedValues:
    """Stands in for an endless iterator: counts reads and refuses to run forever."""

    def __init__(self):
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.consumed += 1
        if self.consumed > ITERATION_GUARD:
            raise RuntimeError("iteration guard reached")
        return 1


class CountingMapping(Mapping):
    def __init__(self, keys):
        self.keys_list = keys
        self.inspected = 0

    def __getitem__(self, key):
        return 0

    def __len__(self):
        return len(self.keys_list)

    def __iter__(self):
        for key in self.keys_list:
            self.inspected += 1
            yield key


class CountingList(list):
    visits = 0

    def __iter__(self):
        type(self).visits += 1
        return super().__iter__()


def peak_allocation(work):
    tracemalloc.start()
    try:
        work()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_dictionary_key_inspection_counts_against_the_budget():
    # A late non-plain key made each visit scan every key for one node of budget.
    shared = CountingMapping([f"key-{index}" for index in range(500)] + [(1,)])
    safe_evidence({"value": [shared] * 1000})
    assert shared.inspected <= MAX_STRUCTURE_NODES


def test_shared_containers_are_visited_within_the_budget():
    CountingList.visits = 0
    shared = CountingList()
    for _ in range(31):
        shared = CountingList([shared, shared])
    safe_evidence({"value": shared})
    assert CountingList.visits <= MAX_STRUCTURE_NODES


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param({(1,): "value"}, id="non-plain-key"),
        pytest.param({f"key-{index}": 0 for index in range(MAX_STRUCTURE_NODES * 2)}, id="wide"),
    ],
)
def test_top_level_mapping_is_always_kept_as_a_mapping(evidence):
    assert isinstance(alert(evidence=evidence).evidence, dict)


@dataclass
class Holder:
    value: object


class ModelHolder(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    value: object


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(Holder, id="dataclass"),
        pytest.param(lambda value: ModelHolder.model_construct(value=value), id="model"),
        pytest.param(lambda value: deque([value]), id="deque"),
        pytest.param(lambda value: {value}, id="set"),
        pytest.param(lambda value: Holder(Holder(value)), id="nested-dataclass"),
    ],
)
def test_iterators_inside_containers_are_marked_and_not_consumed(wrap):
    guarded = GuardedValues()
    item = alert(evidence={"value": wrap(guarded)})
    assert guarded.consumed == 0
    assert UNSERIALIZABLE in json.dumps(item.evidence)


def test_arbitrary_iterables_are_marked_and_not_iterated():
    class Iterable:
        def __init__(self):
            self.values = GuardedValues()

        def __iter__(self):
            return self.values

    source = Iterable()
    assert safe_evidence({"value": source}) == {"value": UNSERIALIZABLE}
    assert source.values.consumed == 0


def test_self_referencing_records_are_marked():
    record = Holder(None)
    record.value = record
    assert safe_evidence({"value": record}) == {"value": {"value": UNSERIALIZABLE}}


def test_failing_container_does_not_hide_other_values():
    class Failing(list):
        def __iter__(self):
            raise RuntimeError("synthetic failure")

    evidence = safe_evidence({"bad": Failing([1]), "good": 1})
    assert evidence == {"bad": UNSERIALIZABLE, "good": 1}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x" * 10_000, id="text"),
        pytest.param(b"x" * 10_000, id="bytes"),
        pytest.param(Path("x" * 10_000), id="path"),
    ],
)
def test_shared_text_cannot_multiply_memory(value):
    # This allocated about 100 MB before the budget counted text.
    peak = peak_allocation(lambda: safe_evidence({"value": [value] * 9_000}))
    assert peak < 1024 * 1024
    assert safe_evidence({"value": [value] * 9_000}) == OVER_LIMIT


def test_oversized_text_is_not_escaped_in_full():
    # Each lone surrogate escapes to six bytes, so escaping first needed about 6 MB.
    text = "\ud800" * 1_000_000
    peak = peak_allocation(lambda: alert(explanation=text))
    assert peak < 1024 * 1024


def test_truncation_is_unchanged_by_the_early_cut():
    text = "\ud800" * 1000
    assert alert(explanation=text).explanation == (
        (r"\ud800" * 1000)[: 2000 - len(f" {TRUNCATED}")] + f" {TRUNCATED}"
    )


@pytest.mark.parametrize("character", ["x", "é", "€", "😀"])
def test_evidence_at_the_byte_limit_is_unchanged(character):
    overhead = len(json.dumps({"value": ""}))
    count = (MAX_EVIDENCE_BYTES - overhead) // len(character.encode("utf-8"))
    evidence = {"value": character * count}
    assert safe_evidence(evidence) == evidence
    padded = {"value": character * (count + 1)}
    assert safe_evidence(padded) == OVER_LIMIT


class Level(Enum):
    HIGH = 3


class Named(BaseModel):
    name: str
    when: datetime


@dataclass
class Record:
    name: str
    when: datetime
    tags: tuple[str, ...]


MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(MOMENT, id="datetime"),
        pytest.param(date(2026, 1, 2), id="date"),
        pytest.param(time(3, 4, 5), id="time"),
        pytest.param(timedelta(seconds=90), id="timedelta"),
        pytest.param(Decimal("1.50"), id="decimal"),
        pytest.param(UUID(int=7), id="uuid"),
        pytest.param(Path("/synthetic/file"), id="path"),
        pytest.param(Level.HIGH, id="enum"),
        pytest.param(b"synthetic", id="bytes"),
        pytest.param((1, "two", None), id="tuple"),
        pytest.param(Record("synthetic", MOMENT, ("a", "b")), id="dataclass"),
        pytest.param(Named(name="synthetic", when=MOMENT), id="model"),
        pytest.param({"nested": [True, 1.5, None]}, id="nested"),
    ],
)
def test_supported_values_keep_their_json_form(value):
    assert safe_evidence({"value": value}) == {"value": to_jsonable_python(value)}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b"\xff", id="undecodable-bytes"),
        pytest.param(ipaddress.ip_address("127.0.0.1"), id="ip"),
        pytest.param(deque([1, 2]), id="deque"),
    ],
)
def test_unsupported_values_stay_marked(value):
    assert safe_evidence({"value": value}) == {"value": UNSERIALIZABLE}


def test_sets_have_a_deterministic_order():
    values = {f"item-{index}" for index in range(100)}
    assert safe_evidence({"value": values}) == {"value": sorted(values)}


def test_mixed_sets_are_ordered_without_comparing_types():
    evidence = safe_evidence({"value": {1, "a", None}})
    assert evidence == safe_evidence({"value": {None, "a", 1}})
    assert len(evidence["value"]) == 3


def test_evidence_is_stable_when_sanitized_again():
    item = alert(evidence={"value": {"b", "a"}, "at": MOMENT, "self": (1, [2])})
    assert safe_evidence(item.evidence) == item.evidence
