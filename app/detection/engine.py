from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.detection.rules import RuleEvaluation, evaluate_rules
from app.models import (
    THRESHOLD_ALERT_RULES,
    Alert,
    DetectionSettings,
    NetworkSample,
    Settings,
    SystemSample,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class EpisodeRestoration:
    stored: Alert
    provisional: Alert | None
    recovery: Alert | None
    operator_changes: dict[str, bool]


class DetectionEngine:
    def __init__(
        self,
        clock=time.monotonic,
        cooldown_seconds: float = 60.0,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.clock = clock
        self.wall_clock = wall_clock
        self.cooldown_seconds = cooldown_seconds
        self._breach_started: dict[str, float] = {}
        self._last_alerted: dict[str, float] = {}
        # Identity and first observation survive restarts for an ongoing episode.
        self._active: dict[str, Alert] = {}
        # Until initial storage restoration completes, retain only the first
        # possible continuation and the first readable clear per rule (max eight).
        self._startup_first: dict[str, Alert] = {}
        self._startup_clear: dict[str, tuple[RuleEvaluation, datetime]] = {}
        self._startup_operator_changes: dict[str, dict[str, bool]] = {}
        self._restored = False
        self._previous_connections: int | None = None
        self._collection_failure_active = False
        self._collection_failure_first: datetime | None = None

    def restore_active(self, alerts: Iterable[Alert]) -> list[EpisodeRestoration]:
        """Bind an unconfirmed startup continuation to its stored episode.

        A readable clear is a boundary: episodes after it must remain independent.
        The service reconciles the returned deltas before any queued write runs.
        """
        if self._restored:
            return []
        restored: list[EpisodeRestoration] = []
        seen = set()
        for alert in alerts:
            if (
                alert.rule_id in THRESHOLD_ALERT_RULES
                and alert.condition_active is True
                and alert.rule_id not in seen
            ):
                seen.add(alert.rule_id)
                alert = alert.model_copy(update={
                    "first_occurrence": alert.first_occurrence or alert.timestamp,
                    "last_occurrence": alert.last_occurrence or alert.timestamp,
                })
                provisional = self._startup_first.get(alert.rule_id)
                clear = self._startup_clear.get(alert.rule_id)
                latest = alert.model_copy(update={
                    "last_occurrence": (
                        provisional.last_occurrence if provisional is not None
                        else alert.last_occurrence
                    )
                })
                recovery = self._recovery(*clear, latest) if clear is not None else None
                if clear is None:
                    self._active[alert.rule_id] = latest
                restored.append(EpisodeRestoration(
                    alert, provisional, recovery,
                    dict(self._startup_operator_changes.get(alert.rule_id, {})),
                ))
        self._restored = True
        self._startup_first.clear()
        self._startup_clear.clear()
        self._startup_operator_changes.clear()
        return restored

    def active_episode_id(self, rule_key: str) -> str | None:
        active = self._active.get(rule_key)
        return active.id if active is not None else None

    def note_operator_update(self, alert_id: str, changes: dict[str, bool]) -> None:
        # Explicit local intent must survive a saved logging opt-out discarding
        # persistence queues. Only the startup continuation can be rebound.
        for rule, provisional in self._startup_first.items():
            if provisional.id == alert_id:
                previous = self._startup_operator_changes.get(rule, {})
                self._startup_operator_changes[rule] = previous | changes
                break

    def evaluate(
        self,
        system: SystemSample,
        network: NetworkSample,
        settings: Settings,
        detection: DetectionSettings | None = None,
    ) -> list[Alert]:
        now = self.clock()
        alerts: list[Alert] = []
        configured = detection or DetectionSettings(cooldown_seconds=self.cooldown_seconds)
        rules = evaluate_rules(
            system, network, settings, self._previous_connections, configured
        )
        if "established_connections" not in network.unavailable_metrics:
            self._previous_connections = network.established_connections
        collection_rule = next(
            (rule for rule in rules if rule.key == "collection_failure"), None
        )
        observed_at = self.wall_clock()
        if collection_rule is not None and collection_rule.breached:
            if not self._collection_failure_active:
                self._collection_failure_active = True
                self._collection_failure_first = observed_at
            alerts.append(
                Alert(
                    id=str(uuid5(NAMESPACE_URL, "ghostwatch:collection-failure")),
                    timestamp=observed_at,
                    severity=collection_rule.severity,  # type: ignore[arg-type]
                    source=collection_rule.source,
                    metric=collection_rule.metric,
                    explanation=collection_rule.explanation,
                    rule_id=collection_rule.key,
                    evidence=collection_rule.evidence,
                    first_occurrence=self._collection_failure_first,
                    last_occurrence=observed_at,
                    occurrence_count=1,
                    condition_active=True,
                )
            )
        elif self._collection_failure_active:
            alerts.append(
                Alert(
                    id=str(uuid5(NAMESPACE_URL, "ghostwatch:collection-failure")),
                    timestamp=observed_at,
                    severity="INFO",
                    source="Collector",
                    metric="Telemetry collection recovered",
                    explanation="All monitored telemetry collectors reported a successful cycle.",
                    rule_id="collection_failure",
                    evidence={"unavailable_metrics": []},
                    first_occurrence=self._collection_failure_first,
                    occurrence_count=0,
                    condition_active=False,
                    last_recovery=observed_at,
                )
            )
            self._collection_failure_active = False
            self._collection_failure_first = None
        for rule in rules:
            if rule.key == "collection_failure":
                continue
            if not rule.breached:
                self._breach_started.pop(rule.key, None)
                if not self._restored and rule.available:
                    self._startup_clear.setdefault(rule.key, (rule, observed_at))
                # An unreadable metric has not been seen to clear.
                if rule.available and rule.key in self._active:
                    active = self._active.pop(rule.key)
                    alerts.append(self._recovery(rule, observed_at, active))
                continue
            started = self._breach_started.setdefault(rule.key, now)
            if now - started < rule.duration:
                continue
            last_alerted = self._last_alerted.get(rule.key)
            active = self._active.get(rule.key)
            if (
                active is not None
                and last_alerted is not None
                and now - last_alerted < configured.cooldown_seconds
            ):
                continue
            self._last_alerted[rule.key] = now
            first = active.first_occurrence if active is not None else observed_at
            observation = Alert(
                id=active.id if active is not None else str(uuid4()),
                timestamp=observed_at,
                severity=rule.severity,  # type: ignore[arg-type]
                source=rule.source,
                metric=rule.metric,
                explanation=rule.explanation,
                rule_id=rule.key,
                evidence=rule.evidence,
                first_occurrence=first,
                last_occurrence=observed_at,
                occurrence_count=1,
                condition_active=True,
            )
            self._active[rule.key] = observation
            if not self._restored and rule.key not in self._startup_clear:
                previous = self._startup_first.get(rule.key)
                self._startup_first[rule.key] = observation.model_copy(update={
                    "occurrence_count": (previous.occurrence_count if previous else 0) + 1
                })
            alerts.append(observation)
        return alerts

    @staticmethod
    def _recovery(rule: RuleEvaluation, observed_at: datetime, active: Alert) -> Alert:
        # Severity stays the rule's own, so history still shows what was breached.
        return Alert(
            id=active.id,
            timestamp=observed_at,
            severity=rule.severity,  # type: ignore[arg-type]
            source=rule.source,
            metric=rule.metric,
            explanation="Recovered: the latest sample is within the configured threshold.",
            rule_id=rule.key,
            evidence=rule.evidence,
            first_occurrence=active.first_occurrence,
            last_occurrence=active.last_occurrence,
            occurrence_count=0,
            condition_active=False,
            last_recovery=observed_at,
        )
