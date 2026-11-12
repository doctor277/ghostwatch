from __future__ import annotations

from app.service import TelemetryService
from app.storage.database import Database


async def test_clean_start_does_not_announce_a_storage_recovery(tmp_path):
    service = TelemetryService(Database(tmp_path / "clean.db"))

    assert await service.initialize_storage()

    assert service.storage.status == "healthy"
    assert "storage_recovery" not in [event.event_type for event in service.live_events]


async def test_recovery_after_a_real_failure_is_still_announced(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "flaky.db"), storage_retry_seconds=0)
    original = service.database.initialize

    def fail_once():
        monkeypatch.setattr(service.database, "initialize", original)
        raise OSError("synthetic outage")

    monkeypatch.setattr(service.database, "initialize", fail_once)
    assert not await service.initialize_storage()
    assert service.storage.status == "degraded"

    await service._maybe_recover_storage()

    assert service.storage.status == "healthy"
    assert service.live_events[0].event_type == "storage_recovery"
