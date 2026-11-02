from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import fmean, pstdev

from app.models import BaselineMetric, NetworkSample, SystemSample


class BaselineEngine:
    """Rolling baseline whose bounded observations can survive a service restart."""

    METRIC_NAMES = {
        "cpu_percent": "CPU utilization",
        "memory_percent": "Memory utilization",
        "bandwidth_mbps": "Combined network bandwidth",
        "active_connections": "Active connections",
        "process_count": "Process count",
    }

    def __init__(self, max_samples: int = 720, min_samples: int = 12) -> None:
        self.max_samples = max_samples
        self.min_samples = min_samples
        self._values: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.max_samples)
        )

    @staticmethod
    def _percentile_95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]

    @staticmethod
    def _level(ratio: float) -> str:
        absolute = abs(ratio)
        if absolute >= 1.0:
            return "HIGH"
        if absolute >= 0.5:
            return "MODERATE"
        return "LOW"

    def observe(self, system: SystemSample, network: NetworkSample) -> list[BaselineMetric]:
        values = {
            "cpu_percent": system.cpu_percent,
            "memory_percent": system.memory_percent,
            "bandwidth_mbps": network.upload_mbps + network.download_mbps,
            "active_connections": float(network.active_connections),
            "process_count": float(system.process_count),
        }
        unavailable = set(system.unavailable_metrics) | set(network.unavailable_metrics)
        unavailable_aliases = {
            "bandwidth_mbps": {
                "upload_mbps",
                "download_mbps",
                "bytes_sent_per_second",
                "bytes_recv_per_second",
            }
        }
        results: list[BaselineMetric] = []
        for key, current in values.items():
            if key in unavailable or unavailable_aliases.get(key, set()) & unavailable:
                continue
            history = list(self._values[key])
            if len(history) >= self.min_samples:
                mean = fmean(history)
                deviation = (current - mean) / max(abs(mean), 1.0)
                standard_deviation = pstdev(history)
                level = self._level(deviation)
                results.append(
                    BaselineMetric(
                        metric=key,
                        current=round(current, 3),
                        mean=round(mean, 3),
                        standard_deviation=round(standard_deviation, 3),
                        percentile_95=round(self._percentile_95(history), 3),
                        deviation_ratio=round(deviation, 3),
                        level=level,  # type: ignore[arg-type]
                        sample_count=len(history),
                        explanation=(
                            f"{self.METRIC_NAMES[key]} is {abs(deviation) * 100:.0f}% "
                            f"{'above' if deviation >= 0 else 'below'} its rolling average. "
                            "This is an observed deviation, not proof of compromise."
                        ),
                    )
                )
            self._values[key].append(float(current))
        return results

    def export_state(self) -> dict[str, list[float]]:
        return {
            key: list(self._values[key])
            for key in self.METRIC_NAMES
            if self._values[key]
        }

    def restore_state(self, payload: dict[str, list[float]]) -> None:
        restored: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.max_samples)
        )
        for key in self.METRIC_NAMES:
            values = payload.get(key, [])
            if not isinstance(values, list):
                continue
            safe_values = [
                float(value)
                for value in values[-self.max_samples :]
                if isinstance(value, (int, float)) and math.isfinite(value)
            ]
            restored[key].extend(safe_values)
        self._values = restored
