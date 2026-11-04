from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from app.models import (
    FileBaselineEntry,
    FileScanResult,
    MonitoredFolder,
)
from app.storage.database import Database


def test_database_calls_close_their_connections(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        opened.append(real_connect(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    database.schema_version()
    database.backup(tmp_path / "backup.db")

    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_explicit_transaction_rolls_back_and_closes_on_error(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    folder = MonitoredFolder(
        id="synthetic-folder", path=str(tmp_path), created_at=datetime.now(UTC)
    )
    database.add_monitored_folder(
        folder,
        [FileBaselineEntry(relative_path="kept.txt", sha256="0" * 64, size=1, modified_ns=1)],
    )
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        opened.append(real_connect(*args, **kwargs))
        return opened[-1]

    def fail(*args):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(database, "_insert_file_baselines", fail)
    with pytest.raises(sqlite3.OperationalError):
        database.save_file_scan(folder.id, FileScanResult(status="healthy"), 100)

    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert list(database.load_file_baseline(folder.id)) == ["kept.txt"]


def test_database_connection_scope_rolls_back_and_closes_on_error(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with pytest.raises(RuntimeError):
        with database._connect() as connection:
            connection.execute(
                "INSERT INTO configuration (key, payload) VALUES ('synthetic', '{}')"
            )
            raise RuntimeError("synthetic")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with database._connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM configuration WHERE key = 'synthetic'"
        ).fetchone()
    assert row is None
