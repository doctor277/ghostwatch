from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.models import (
    FileBaselineEntry,
    FileIntegrityEvent,
    FileIntegritySettings,
    FileScanResult,
    MonitoredFolder,
)


class FileIntegrityCollector:
    """Bounded, read-only SHA-256 scanner for explicitly enrolled folders."""

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or (callable(is_junction) and is_junction())

    @staticmethod
    def validate_folder(path: str | Path) -> Path:
        selected = Path(path).expanduser()
        if not selected.is_absolute():
            raise ValueError("Monitored folder path must be absolute")
        if any(
            FileIntegrityCollector._is_link_like(component)
            for component in (selected, *selected.parents)
        ):
            raise ValueError(
                "Monitored folder root must not traverse a symbolic link or junction; "
                "enroll its resolved target explicitly"
            )
        try:
            resolved = selected.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Monitored folder does not exist or is inaccessible") from exc
        if not resolved.is_dir():
            raise ValueError("Monitored path must be a directory")
        return resolved

    def scan(
        self,
        folder: MonitoredFolder,
        previous: dict[str, FileBaselineEntry],
        settings: FileIntegritySettings,
        *,
        emit_events: bool = True,
    ) -> FileScanResult:
        try:
            root = self.validate_folder(folder.path)
        except ValueError as exc:
            return FileScanResult(status="unavailable", status_detail=str(exc))

        entries: list[FileBaselineEntry] = []
        events: list[FileIntegrityEvent] = []
        seen: set[str] = set()
        skipped = 0
        bounded = False
        enumeration_errors: list[OSError] = []
        timestamp = datetime.now(UTC)
        try:
            walker = os.walk(
                root,
                topdown=True,
                onerror=enumeration_errors.append,
                followlinks=False,
            )
            for directory, directories, filenames in walker:
                directory_path = Path(directory)
                directories[:] = sorted(
                    name
                    for name in directories
                    if not self._is_link_like(directory_path / name)
                )
                for filename in sorted(filenames):
                    if len(seen) >= settings.max_files_per_folder:
                        bounded = True
                        break
                    path = directory_path / filename
                    if self._is_link_like(path):
                        skipped += 1
                        continue
                    relative = path.relative_to(root).as_posix()
                    seen.add(relative)
                    try:
                        before = path.stat()
                        if before.st_size > settings.max_file_size_bytes:
                            raise OSError("file exceeds configured hashing limit")
                        digest = self._sha256(path)
                        after = path.stat()
                        if (
                            before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                        ):
                            raise OSError("file changed while hashing")
                    except OSError:
                        skipped += 1
                        if relative in previous:
                            entries.append(previous[relative])
                        continue
                    current = FileBaselineEntry(
                        relative_path=relative,
                        sha256=digest,
                        size=after.st_size,
                        modified_ns=after.st_mtime_ns,
                    )
                    entries.append(current)
                    prior = previous.get(relative)
                    if not emit_events:
                        continue
                    if prior is None:
                        events.append(
                            self._event(folder.id, timestamp, "created", current, None)
                        )
                    elif prior.sha256 != current.sha256:
                        events.append(
                            self._event(folder.id, timestamp, "modified", current, prior)
                        )
                if bounded:
                    break
        except OSError as exc:
            return FileScanResult(
                entries=list(previous.values()),
                status="unavailable",
                status_detail=f"Folder enumeration failed ({type(exc).__name__})",
                skipped_files=skipped,
            )

        incomplete = bounded or bool(enumeration_errors)
        if incomplete:
            entries.extend(
                prior for relative, prior in previous.items() if relative not in seen
            )
        if emit_events and not incomplete:
            for relative, prior in previous.items():
                if relative not in seen:
                    events.append(
                        self._event(folder.id, timestamp, "deleted", None, prior)
                    )
        status = "partial" if skipped or incomplete else "healthy"
        details: list[str] = []
        if skipped:
            details.append(f"{skipped} file(s) skipped")
        if bounded:
            details.append("configured file limit reached; deletions were not inferred")
        if enumeration_errors:
            details.append(
                f"{len(enumeration_errors)} directorie(s) inaccessible; deletions were not inferred"
            )
        return FileScanResult(
            entries=entries,
            events=events,
            status=status,
            status_detail="; ".join(details),
            scanned_files=len(entries),
            skipped_files=skipped,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _event(
        folder_id: str,
        timestamp: datetime,
        change_type: str,
        current: FileBaselineEntry | None,
        previous: FileBaselineEntry | None,
    ) -> FileIntegrityEvent:
        relative = (current or previous).relative_path  # type: ignore[union-attr]
        previous_hash = previous.sha256 if previous else None
        current_hash = current.sha256 if current else None
        identity = (
            f"{folder_id}:{change_type}:{relative}:{previous_hash}:{current_hash}:"
            f"{timestamp.isoformat()}"
        )
        return FileIntegrityEvent(
            id=str(uuid5(NAMESPACE_URL, identity)),
            folder_id=folder_id,
            timestamp=timestamp,
            change_type=change_type,  # type: ignore[arg-type]
            relative_path=relative,
            previous_sha256=previous_hash,
            current_sha256=current_hash,
            size=current.size if current else previous.size if previous else None,
        )
