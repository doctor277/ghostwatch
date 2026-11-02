from __future__ import annotations

import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import package_release as release


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    return root


def _mark_link_when_unavailable(
    link: Path, monkeypatch: pytest.MonkeyPatch, *, directory: bool = False
) -> None:
    if directory:
        link.mkdir()
    else:
        link.write_bytes(b"synthetic stand-in")
    original = release._is_link_like
    monkeypatch.setattr(
        release,
        "_is_link_like",
        lambda path, metadata=None: path == link or original(path, metadata),
    )


def _create_file_symlink_or_mark(
    target: Path, link: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        assert getattr(exc, "winerror", None) in {5, 1314}
        _mark_link_when_unavailable(link, monkeypatch)


def _create_directory_symlink_or_mark(
    target: Path, link: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        assert getattr(exc, "winerror", None) in {5, 1314}
        _mark_link_when_unavailable(link, monkeypatch, directory=True)


def _windows_profile(*, escaped: bool = False, mixed: bool = False) -> str:
    first = "\\\\" if escaped else "/" if mixed else "\\"
    second = "\\" if mixed else first
    return "C:" + first + "Users" + second + "private-account" + second + "secret.txt"


def _unix_profile() -> str:
    return "/" + "home" + "/" + "private-account" + "/secret.txt"


def test_external_text_file_symlink_is_rejected_without_partial_archive(
    tmp_path, monkeypatch
):
    root = _source_root(tmp_path)
    external = tmp_path / "external.py"
    external.write_text("outside = True\n", encoding="utf-8")
    link = root / "docs" / "linked.txt"
    _create_file_symlink_or_mark(external, link, monkeypatch)
    destination = tmp_path / "release.zip"

    with pytest.raises(RuntimeError, match="Linked release source rejected"):
        release.build(root, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".ghostwatch-release-*.tmp"))


def test_external_allowlisted_binary_symlink_is_still_rejected(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    external = tmp_path / "external.png"
    external.write_bytes(b"external binary")
    link = root / "docs" / "browser-demo-network-graph.png"
    _create_file_symlink_or_mark(external, link, monkeypatch)

    with pytest.raises(RuntimeError, match="Linked release source rejected"):
        release.build(root, tmp_path / "release.zip")


def test_symlinked_included_parent_directory_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    external = tmp_path / "external-docs"
    external.mkdir()
    (external / "outside.txt").write_text("outside", encoding="utf-8")
    linked_docs = root / "docs"
    _create_directory_symlink_or_mark(external, linked_docs, monkeypatch)

    with pytest.raises(RuntimeError, match="Linked release source rejected"):
        release.build(root, tmp_path / "release.zip")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_in_included_tree_is_rejected(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "outside.txt").write_text("outside", encoding="utf-8")
    junction = root / "docs" / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0
    if not created:
        _mark_link_when_unavailable(junction, monkeypatch, directory=True)
    try:
        with pytest.raises(RuntimeError, match="Linked release source rejected"):
            release.build(root, tmp_path / "release.zip")
    finally:
        if created and junction.exists():
            os.rmdir(junction)


def test_inventory_revalidates_file_identity_before_reading(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    source = root / "docs" / "stable.txt"
    source.write_text("original", encoding="utf-8")
    original_inventory = release._selected_inventory

    def replace_after_inventory(selected_root):
        inventory = original_inventory(selected_root)
        source.unlink()
        source.write_text("replacement with a different identity", encoding="utf-8")
        return inventory

    monkeypatch.setattr(release, "_selected_inventory", replace_after_inventory)
    destination = tmp_path / "release.zip"

    with pytest.raises(RuntimeError, match="identity changed"):
        release.build(root, destination)
    assert not destination.exists()


def test_inventory_revalidates_parent_components_before_reading(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    (root / "docs" / "stable.txt").write_text("original", encoding="utf-8")
    original_inventory = release._selected_inventory
    original_link_check = release._is_link_like
    reading = False

    def inventory_then_mark_parent(selected_root):
        nonlocal reading
        inventory = original_inventory(selected_root)
        reading = True
        return inventory

    def link_check(path, metadata=None):
        if reading and path == root / "docs":
            return True
        return original_link_check(path, metadata)

    monkeypatch.setattr(release, "_selected_inventory", inventory_then_mark_parent)
    monkeypatch.setattr(release, "_is_link_like", link_check)

    with pytest.raises(RuntimeError, match="Linked release source rejected"):
        release.build(root, tmp_path / "release.zip")


@pytest.mark.parametrize(
    "suffix",
    [".py", ".js", ".json", ".html", ".css", ".ps1", ".toml", ".md", ".txt"],
)
@pytest.mark.parametrize("profile", [_windows_profile(), _unix_profile()])
def test_every_source_like_format_rejects_profile_paths_without_echoing_match(
    suffix, profile
):
    with pytest.raises(RuntimeError, match="user profile path") as caught:
        release.scan_sensitive_payloads({"source/sample" + suffix: profile.encode()})

    assert "private-account" not in str(caught.value)
    assert "secret.txt" not in str(caught.value)


def test_json_escaped_windows_profile_and_mixed_separators_are_detected():
    escaped = ("{\"path\": \"" + _windows_profile(escaped=True) + "\"}").encode()
    mixed = _windows_profile(mixed=True).encode()

    with pytest.raises(RuntimeError, match="user profile path"):
        release.scan_sensitive_payloads({"source/settings.json": escaped})
    with pytest.raises(RuntimeError, match="user profile path"):
        release.scan_sensitive_payloads({"source/mixed.js": mixed})


def test_synthetic_fixture_exception_is_exact_and_not_a_tests_wildcard():
    allowed = _windows_profile().replace("private-account", "alice").encode()
    release.scan_sensitive_payloads({"tests/test_v3_p0.py": allowed})

    unsafe = _windows_profile().replace("private-account", "real-account").encode()
    with pytest.raises(RuntimeError, match="user profile path"):
        release.scan_sensitive_payloads({"tests/test_v3_p0.py": unsafe})


def test_sensitive_archive_name_is_not_echoed_in_scanner_diagnostic(monkeypatch):
    monkeypatch.setenv("USERNAME", "CaseyRC5")
    monkeypatch.setenv("USER", "")

    with pytest.raises(RuntimeError, match="local username in archive path") as caught:
        release.scan_sensitive_payloads({"docs/CaseyRC5.txt": b"safe"})

    assert "CaseyRC5" not in str(caught.value)


def test_executable_source_bytes_are_preserved_and_unicode_name_is_supported(tmp_path):
    root = _source_root(tmp_path)
    source = root / "docs" / "rN{LATIN SMALL LETTER E WITH ACUTE}sumN{LATIN SMALL LETTER E WITH ACUTE}.py"
    original = b"value = 'safe'\r\n"
    source.write_bytes(original)
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read("docs/rN{LATIN SMALL LETTER E WITH ACUTE}sumN{LATIN SMALL LETTER E WITH ACUTE}.py") == original


def test_oversized_release_input_is_rejected_without_partial_output(
    tmp_path, monkeypatch
):
    root = _source_root(tmp_path)
    (root / "docs" / "large.txt").write_bytes(b"x" * 33)
    monkeypatch.setattr(release, "MAX_RELEASE_FILE_BYTES", 32)
    destination = tmp_path / "release.zip"

    with pytest.raises(RuntimeError, match="exceeds size limit"):
        release.build(root, destination)
    assert not destination.exists()


def test_existing_destination_is_not_replaced_and_temporary_is_cleaned(tmp_path):
    root = _source_root(tmp_path)
    (root / "docs" / "safe.txt").write_text("safe", encoding="utf-8")
    destination = tmp_path / "release.zip"
    destination.write_bytes(b"existing archive placeholder")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        release.build(root, destination)

    assert destination.read_bytes() == b"existing archive placeholder"
    assert not list(tmp_path.glob(".ghostwatch-release-*.tmp"))


def test_archive_verifier_rejects_duplicate_and_traversal_names(tmp_path):
    duplicate = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("docs/same.txt", b"one")
            archive.writestr("docs/same.txt", b"two")
    with pytest.raises(RuntimeError, match="Duplicate release archive name"):
        release.verify_archive(duplicate)

    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.py", b"safe = False")
    with pytest.raises(RuntimeError, match="Unsafe release archive name"):
        release.verify_archive(traversal)


def test_archive_verifier_rejects_corruption_and_symlink_entries(tmp_path):
    corrupted = tmp_path / "corrupted.zip"
    corrupted.write_bytes(b"not a zip")
    with pytest.raises(RuntimeError, match="invalid or corrupted"):
        release.verify_archive(corrupted)

    linked = tmp_path / "linked.zip"
    info = zipfile.ZipInfo("docs/link.txt")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(info, "outside.txt")
    with pytest.raises(RuntimeError, match="Symbolic link entry"):
        release.verify_archive(linked)


def test_direct_source_path_escape_is_rejected(tmp_path):
    root = _source_root(tmp_path)
    external = tmp_path / "outside.py"
    external.write_text("outside = True", encoding="utf-8")
    lexical, resolved = release._repository_roots(root)

    with pytest.raises(RuntimeError, match="escapes the repository root"):
        release._entry_for_path(lexical, resolved, external)

