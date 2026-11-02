from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from app.detection.rules import evaluate_rules
from app.models import Alert, DetectionSettings, NetworkSample, Settings, SystemSample


class DetectionEngine:
    def __init__(self, clock=time.monotonic, cooldown_seconds: float = 60.0) -> None:
        self.clock = clock
        self.cooldown_seconds = cooldown_seconds
        self._breach_started: dict[str, float] = {}
        self._last_alerted: dict[str, float] = {}
        self._occurrences: dict[str, int] = {}
        self._previous_connections: int | None = None
        self._collection_failure_active = False
        self._collection_failure_first: datetime | None = None

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
        observed_at = datetime.now(UTC)
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
                continue
            started = self._breach_started.setdefault(rule.key, now)
            if now - started < rule.duration:
                continue
            last_alerted = self._last_alerted.get(rule.key)
            if (
                last_alerted is not None
                and now - last_alerted < configured.cooldown_seconds
            ):
                continue
            self._last_alerted[rule.key] = now
            occurrence = self._occurrences.get(rule.key, 0) + 1
            self._occurrences[rule.key] = occurrence
            alert_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"ghostwatch:{rule.key}:{started:.6f}:{occurrence}",
                )
            )
            alerts.append(
                Alert(
                    id=alert_id,
                    timestamp=datetime.now(UTC),
                    severity=rule.severity,  # type: ignore[arg-type]
                    source=rule.source,
                    metric=rule.metric,
                    explanation=rule.explanation,
                    rule_id=rule.key,
                    evidence=rule.evidence,
                )
            )
        return alerts
