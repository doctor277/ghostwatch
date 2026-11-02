from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

INCLUDE_DIRECTORIES = ("app", "frontend", "tests", "e2e", "scripts", "docs")
INCLUDE_FILES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "REVIEW_EVIDENCE.md",
    "REVIEW_PACKET.md",
    "package-lock.json",
    "package.json",
    "playwright.config.js",
    "pyproject.toml",
    "requirements.txt",
    "start.ps1",
)
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "data",
    "node_modules",
    "playwright-report",
    "test-results",
}
EXCLUDED_SUFFIXES = {".db", ".db-shm", ".db-wal", ".env", ".log", ".sqlite", ".sqlite3"}
EXCLUDED_FILES = {
    "docs/browser-desktop.png",  # Live host telemetry; never distribute it.
    # Private design concepts with unresolved generating-account provenance.
    "docs/design-concept.png",
    "docs/ghostwatch-v2-concept.png",
    "REVIEW_EVIDENCE.md",  # Private, machine-specific source evidence.
    "REVIEW_PACKET.md",  # Superseded by sanitized public RC summaries.
}
EXCLUDED_PREFIXES = (
    "docs/audit-evidence/",  # Raw diagnostics may contain host/profile details.
    "docs/review/",  # Obsolete raw review command output.
)
APPROVED_BINARY_FILES = {
    "docs/browser-demo-network-graph.png",
}
APPROVED_EXTENSIONLESS_TEXT_FILES = {".gitignore", "LICENSE"}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".rst",
    ".toml",
    ".txt",
}
PROFILE_SCAN_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
}
PUBLIC_REDACTION_SUFFIXES = {".md", ".rst", ".txt"}
MAX_RELEASE_FILE_BYTES = 50 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 250 * 1024 * 1024
SENSITIVE_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    "bearer token": re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}", re.IGNORECASE),
}
WINDOWS_PROFILE = re.compile(
    r"(?i)(?:[a-z]:)?[\\/]+users(?:[\\/]+)[^\\/\r\n\"'`<>:]+"
)
UNIX_PROFILE = re.compile(r"(?i)(?:/users|/home)/[^/\r\n\"'`<>:]+")
REDACTED_HOME = "<USER_HOME>"

# Exact synthetic roots in exact regression-test sources. This is deliberately
# not a tests/ exemption: every other profile root in those files still fails.
_WINDOWS_FIXTURE_ROOT = "c:" + "/" + "users" + "/"
_UNIX_FIXTURE_ROOT = "/" + "home" + "/"
SYNTHETIC_PROFILE_FIXTURES = {
    "frontend/index.html": frozenset({_WINDOWS_FIXTURE_ROOT + "you"}),
    "tests/test_api.py": frozenset({_WINDOWS_FIXTURE_ROOT + "privateuser"}),
    "tests/test_v3_p0.py": frozenset({_WINDOWS_FIXTURE_ROOT + "alice"}),
    "tests/test_v3_rc4_hardening.py": frozenset(
        {
            _WINDOWS_FIXTURE_ROOT + "private-account",
            _UNIX_FIXTURE_ROOT + "private-account",
        }
    ),
    "tests/test_v3_rc5_release.py": frozenset(
        {
            _WINDOWS_FIXTURE_ROOT + "private-account",
            _UNIX_FIXTURE_ROOT + "private-account",
        }
    ),
    "tests/test_v3_windows_events.py": frozenset(
        {
            _WINDOWS_FIXTURE_ROOT + "privateuser",
            _WINDOWS_FIXTURE_ROOT + "private user",
        }
    ),
}


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class ReleaseFile:
    root: Path
    resolved_root: Path
    path: Path
    archive_name: str
    identity: FileIdentity


def _identity(value: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
    )


def _is_link_like(path: Path, metadata: os.stat_result | None = None) -> bool:
    details = metadata or path.lstat()
    if stat.S_ISLNK(details.st_mode) or path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_flag)


def _validate_archive_name(name: str) -> None:
    parts = name.split("/")
    parsed = PurePosixPath(name)
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or parsed.is_absolute()
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in parts)
        or parsed.as_posix() != name
    ):
        raise RuntimeError("Unsafe release archive name rejected")


def _archive_name_key(name: str) -> str:
    _validate_archive_name(name)
    return unicodedata.normalize("NFC", name).casefold()


def _assert_unique_archive_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        key = _archive_name_key(name)
        if key in seen:
            raise RuntimeError("Duplicate release archive name rejected")
        seen.add(key)


def _is_approved_text_file(archive_name: str) -> bool:
    return (
        Path(archive_name).suffix.lower() in TEXT_SUFFIXES
        or archive_name in APPROVED_EXTENSIONLESS_TEXT_FILES
    )


def _release_type_finding(archive_name: str) -> str | None:
    if _is_approved_text_file(archive_name) or archive_name in APPROVED_BINARY_FILES:
        return None
    return "unapproved binary release asset or unknown file type"


def _repository_roots(root: Path) -> tuple[Path, Path]:
    lexical = root.absolute()
    try:
        metadata = lexical.lstat()
        if _is_link_like(lexical, metadata):
            raise RuntimeError("Release repository root cannot be a link or junction")
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Release repository root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Release repository root is not a directory")
    return lexical, resolved


def _entry_for_path(
    root: Path, resolved_root: Path, path: Path, *, allow_directory: bool = False
) -> ReleaseFile:
    try:
        relative_path = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Release source path escapes the repository root") from exc
    archive_name = relative_path.as_posix()
    _validate_archive_name(archive_name)

    current = root
    try:
        root_metadata = root.lstat()
        if _is_link_like(root, root_metadata):
            raise RuntimeError("Release repository root cannot be a link or junction")
        for part in relative_path.parts:
            current /= part
            metadata = current.lstat()
            if _is_link_like(current, metadata):
                raise RuntimeError(f"Linked release source rejected: {archive_name}")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"Release source is unavailable: {archive_name}") from exc
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(f"Release source escapes repository: {archive_name}") from exc
    accepted_type = stat.S_ISREG(metadata.st_mode) or (
        allow_directory and stat.S_ISDIR(metadata.st_mode)
    )
    if not accepted_type:
        raise RuntimeError(f"Non-regular release source rejected: {archive_name}")
    if metadata.st_size > MAX_RELEASE_FILE_BYTES:
        raise RuntimeError(f"Release source exceeds size limit: {archive_name}")
    return ReleaseFile(
        root=root,
        resolved_root=resolved_root,
        path=path,
        archive_name=archive_name,
        identity=_identity(metadata),
    )


def _selected_inventory(root: Path) -> list[ReleaseFile]:
    root, resolved_root = _repository_roots(root)
    candidates: list[Path] = []

    for directory in INCLUDE_DIRECTORIES:
        base = root / directory
        if not os.path.lexists(base):
            continue
        base_entry = _entry_for_path(root, resolved_root, base, allow_directory=True)
        if not stat.S_ISDIR(base.lstat().st_mode):
            raise RuntimeError(f"Release source directory is invalid: {base_entry.archive_name}")

        def fail_walk(
            _error: OSError, source_directory: str = directory
        ) -> None:
            raise RuntimeError(
                f"Unable to enumerate release source: {source_directory}"
            )

        for current_text, directories, filenames in os.walk(
            base, topdown=True, followlinks=False, onerror=fail_walk
        ):
            current = Path(current_text)
            retained_directories: list[str] = []
            for name in sorted(directories):
                child = current / name
                relative = child.relative_to(root)
                if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
                    continue
                _entry_for_path(root, resolved_root, child, allow_directory=True)
                retained_directories.append(name)
            directories[:] = retained_directories
            candidates.extend(current / name for name in sorted(filenames))

    for name in INCLUDE_FILES:
        path = root / name
        if os.path.lexists(path):
            candidates.append(path)

    selected: list[ReleaseFile] = []
    for path in candidates:
        relative = path.relative_to(root)
        archive_name = relative.as_posix()
        if archive_name in EXCLUDED_FILES:
            continue
        if archive_name.startswith(EXCLUDED_PREFIXES):
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        lower_name = path.name.lower()
        if lower_name == ".env" or lower_name.startswith(".env."):
            continue
        if any(lower_name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        entry = _entry_for_path(root, resolved_root, path)
        if finding := _release_type_finding(archive_name):
            raise RuntimeError(f"{finding.capitalize()}: {archive_name}")
        selected.append(entry)

    selected.sort(key=lambda item: item.archive_name)
    _assert_unique_archive_names([item.archive_name for item in selected])
    if sum(item.identity.size for item in selected) > MAX_RELEASE_TOTAL_BYTES:
        raise RuntimeError("Release source inventory exceeds total size limit")
    return selected


def selected_files(root: Path) -> list[Path]:
    """Return the validated release paths for inventory-oriented callers."""
    return [entry.path for entry in _selected_inventory(root)]


def _private_usernames() -> set[str]:
    values = {
        os.environ.get("USERNAME", ""),
        os.environ.get("USER", ""),
        Path.home().name,
    }
    generic = {"", "default", "public", "root", "user", "you"}
    return {value for value in values if value.casefold() not in generic}


def _username_pattern(username: str) -> str:
    return rf"(?<![A-Za-z0-9]){re.escape(username)}(?![A-Za-z0-9])"


def _username_bytes_pattern(username: str) -> bytes:
    return rb"(?<![A-Za-z0-9])" + re.escape(username.encode()) + rb"(?![A-Za-z0-9])"


def _normalized_profile(value: str) -> str:
    return re.sub(r"[\\/]+", "/", value).casefold()


def _has_unapproved_profile(relative: str, text: str) -> bool:
    allowed = SYNTHETIC_PROFILE_FIXTURES.get(relative, frozenset())
    matches = (*WINDOWS_PROFILE.finditer(text), *UNIX_PROFILE.finditer(text))
    return any(_normalized_profile(match.group(0)) not in allowed for match in matches)


def sanitize_text(text: str) -> str:
    """Redact profile roots and the current account name in public text."""
    sanitized = WINDOWS_PROFILE.sub(REDACTED_HOME, text)
    sanitized = UNIX_PROFILE.sub(REDACTED_HOME, sanitized)
    for username in sorted(_private_usernames(), key=len, reverse=True):
        sanitized = re.sub(
            _username_pattern(username),
            "<redacted-user>",
            sanitized,
            flags=re.IGNORECASE,
        )
    return sanitized


def _read_release_file(entry: ReleaseFile) -> bytes:
    try:
        revalidated = _entry_for_path(
            entry.root, entry.resolved_root, entry.path
        )
        before = entry.path.lstat()
        if revalidated.identity != entry.identity or _identity(before) != entry.identity:
            raise RuntimeError(f"Release source identity changed: {entry.archive_name}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(entry.path, flags)
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) != entry.identity:
                raise RuntimeError(f"Release source identity changed: {entry.archive_name}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_RELEASE_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RELEASE_FILE_BYTES:
                    raise RuntimeError(
                        f"Release source exceeds size limit: {entry.archive_name}"
                    )
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = entry.path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Unable to read validated release source: {entry.archive_name}"
        ) from exc
    if _identity(after_open) != entry.identity or _identity(after_path) != entry.identity:
        raise RuntimeError(f"Release source changed while reading: {entry.archive_name}")
    return b"".join(chunks)


def _release_payload(entry: ReleaseFile) -> bytes:
    data = _read_release_file(entry)
    if entry.archive_name in APPROVED_BINARY_FILES:
        return data
    if not _is_approved_text_file(entry.archive_name):
        raise RuntimeError(
            "Release source is an unapproved binary asset or unknown file type"
        )
    suffix = Path(entry.archive_name).suffix.lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Release text file is not UTF-8: {entry.archive_name}"
        ) from exc
    if suffix in PUBLIC_REDACTION_SUFFIXES:
        return sanitize_text(text).encode("utf-8")
    return data


def release_payload(path: Path) -> bytes:
    """Read one file securely, rooted at its parent, for compatibility callers."""
    root = path.parent.absolute()
    _, resolved_root = _repository_roots(root)
    return _release_payload(_entry_for_path(root, resolved_root, path.absolute()))


def scan_sensitive_payloads(payloads: dict[str, bytes]) -> None:
    findings: list[str] = []
    usernames = _private_usernames()
    total = 0
    for member_number, (relative, data) in enumerate(payloads.items(), start=1):
        diagnostic = f"member #{member_number}"
        _validate_archive_name(relative)
        type_finding = _release_type_finding(relative)
        total += len(data)
        if len(data) > MAX_RELEASE_FILE_BYTES:
            findings.append(f"{diagnostic}: release file size limit")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{diagnostic}: {label}")
        for username in usernames:
            if re.search(
                _username_bytes_pattern(username),
                data,
                flags=re.IGNORECASE,
            ):
                findings.append(f"{diagnostic}: local username")
            if re.search(_username_pattern(username), relative, flags=re.IGNORECASE):
                findings.append(f"{diagnostic}: local username in archive path")
        if type_finding:
            findings.append(f"{diagnostic}: {type_finding}")
        suffix = Path(relative).suffix.lower()
        if suffix not in PROFILE_SCAN_SUFFIXES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{diagnostic}: source-like text is not UTF-8")
            continue
        if _has_unapproved_profile(relative, text):
            findings.append(f"{diagnostic}: user profile path")
    if total > MAX_RELEASE_TOTAL_BYTES:
        findings.append("release inventory: total size limit")
    if findings:
        raise RuntimeError("Sensitive-content scan failed:\n" + "\n".join(findings))


def _payloads_from_inventory(inventory: list[ReleaseFile]) -> dict[str, bytes]:
    _assert_unique_archive_names([entry.archive_name for entry in inventory])
    payloads: dict[str, bytes] = {}
    for entry in inventory:
        payloads[entry.archive_name] = _release_payload(entry)
    if sum(len(data) for data in payloads.values()) > MAX_RELEASE_TOTAL_BYTES:
        raise RuntimeError("Release payloads exceed total size limit")
    return payloads


def sanitized_payloads(root: Path, files: list[Path]) -> dict[str, bytes]:
    lexical_root, resolved_root = _repository_roots(root)
    inventory = [
        _entry_for_path(lexical_root, resolved_root, path.absolute()) for path in files
    ]
    return _payloads_from_inventory(inventory)


def scan_sensitive(root: Path, files: list[Path]) -> None:
    scan_sensitive_payloads(sanitized_payloads(root, files))


def verify_archive(path: Path, expected_payloads: dict[str, bytes] | None = None) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            _assert_unique_archive_names(names)
            total = 0
            for info in infos:
                if info.is_dir():
                    raise RuntimeError("Unexpected directory entry in release archive")
                unix_mode = info.external_attr >> 16
                if unix_mode and stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                    raise RuntimeError("Symbolic link entry in release archive")
                if info.file_size > MAX_RELEASE_FILE_BYTES:
                    raise RuntimeError("Release archive member exceeds size limit")
                total += info.file_size
            if total > MAX_RELEASE_TOTAL_BYTES:
                raise RuntimeError("Release archive exceeds total size limit")
            if archive.testzip() is not None:
                raise RuntimeError("Release archive integrity check failed")
            payloads = {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RuntimeError("Release archive is invalid or corrupted") from exc
    scan_sensitive_payloads(payloads)
    if expected_payloads is not None and payloads != expected_payloads:
        raise RuntimeError("Release archive payload verification failed")


def _install_validated_archive(temporary: Path, destination: Path) -> None:
    try:
        if os.name == "nt":
            # Windows rename is atomic on one volume and refuses replacement.
            os.rename(temporary, destination)
        else:
            # POSIX rename replaces existing files, so use a no-clobber link.
            os.link(temporary, destination)
    except FileExistsError as exc:
        raise FileExistsError(
            f"Refusing to overwrite existing archive: {destination}"
        ) from exc
    except PermissionError as exc:
        if destination.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing archive: {destination}"
            ) from exc
        raise


def build(root: Path, destination: Path) -> tuple[int, str]:
    inventory = _selected_inventory(root)
    payloads = _payloads_from_inventory(inventory)
    scan_sensitive_payloads(payloads)
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing archive: {destination}")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".ghostwatch-release-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative, data in payloads.items():
                archive.writestr(relative, data)
        verify_archive(temporary, payloads)
        _install_validated_archive(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return len(inventory), digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a sanitized Ghostwatch source archive")
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    count, digest = build(root, arguments.destination.resolve())
    print(f"files={count}")
    print(f"archive={arguments.destination.resolve()}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
