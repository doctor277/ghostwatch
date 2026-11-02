from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import package_release as release


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    "suffix",
    [".bin", ".BIN", ".exe", ".pfx", ".p12", ".dat", ".zip", ".mystery"],
)
def test_unknown_and_dangerous_extensions_fail_closed_without_partial_archive(
    tmp_path, suffix
):
    root = _source_root(tmp_path)
    (root / "docs" / f"unapproved-dump{suffix}").write_bytes(b"unapproved payload")
    destination = tmp_path / "release.zip"

    with pytest.raises(RuntimeError, match="unknown file type"):
        release.build(root, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".ghostwatch-release-*.tmp"))


def test_unapproved_extensionless_file_is_rejected(tmp_path):
    root = _source_root(tmp_path)
    (root / "docs" / "UNRECOGNIZED").write_text("plain-looking bytes", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unknown file type"):
        release.build(root, tmp_path / "release.zip")


@pytest.mark.parametrize("archive_name", sorted(release.APPROVED_BINARY_FILES))
def test_exact_approved_binary_assets_preserve_bytes(tmp_path, archive_name):
    root = tmp_path / "source"
    source = root / Path(archive_name)
    source.parent.mkdir(parents=True)
    original = b"\x89GHOSTWATCH\x00\xffapproved binary"
    source.write_bytes(original)
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read(archive_name) == original


def test_binary_allowlist_requires_complete_relative_path(tmp_path):
    root = _source_root(tmp_path)
    nested = root / "docs" / "nested"
    nested.mkdir()
    (nested / "design-concept.png").write_bytes(b"lookalike")

    with pytest.raises(RuntimeError, match="Unapproved binary release asset"):
        release.build(root, tmp_path / "release.zip")


@pytest.mark.parametrize("suffix", sorted(release.TEXT_SUFFIXES))
def test_known_text_source_types_are_allowed_and_byte_exact(tmp_path, suffix):
    root = _source_root(tmp_path)
    archive_name = f"docs/safe-source{suffix}"
    source = root / Path(archive_name)
    original = b"safe source bytes\r\n"
    source.write_bytes(original)
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read(archive_name) == original


def test_required_extensionless_gitignore_is_allowed_and_byte_exact(tmp_path):
    root = _source_root(tmp_path)
    source = root / ".gitignore"
    original = b"cache/\r\n"
    source.write_bytes(original)
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read(".gitignore") == original


def test_root_mit_license_is_allowed_and_byte_exact(tmp_path):
    root = _source_root(tmp_path)
    source = root / "LICENSE"
    original = b"MIT License\r\n\r\nCopyright (c) 2026 Example\r\n"
    source.write_bytes(original)
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read("LICENSE") == original


def test_unverified_concepts_are_excluded_and_demo_capture_is_retained(tmp_path):
    root = _source_root(tmp_path)
    (root / "docs" / "browser-demo-network-graph.png").write_bytes(b"demo capture")
    (root / "docs" / "design-concept.png").write_bytes(b"private concept")
    (root / "docs" / "ghostwatch-v2-concept.png").write_bytes(b"private concept")
    destination = tmp_path / "release.zip"

    release.build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.read("docs/browser-demo-network-graph.png") == b"demo capture"
        assert "docs/design-concept.png" not in archive.namelist()
        assert "docs/ghostwatch-v2-concept.png" not in archive.namelist()


def test_symlinked_unknown_binary_is_rejected_before_type_admission(
    tmp_path, monkeypatch
):
    root = _source_root(tmp_path)
    external = tmp_path / "external.bin"
    external.write_bytes(b"external binary")
    link = root / "docs" / "linked.bin"
    try:
        link.symlink_to(external)
    except OSError as exc:
        assert getattr(exc, "winerror", None) in {5, 1314}
        link.write_bytes(b"deterministic stand-in")
        original = release._is_link_like
        monkeypatch.setattr(
            release,
            "_is_link_like",
            lambda path, metadata=None: path == link or original(path, metadata),
        )

    with pytest.raises(RuntimeError, match="Linked release source rejected"):
        release.build(root, tmp_path / "release.zip")


def test_archive_verification_failure_leaves_no_partial_artifact(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    (root / "docs" / "safe.txt").write_text("safe", encoding="utf-8")
    destination = tmp_path / "release.zip"

    def reject_archive(*_args, **_kwargs):
        raise RuntimeError("synthetic verification failure")

    monkeypatch.setattr(release, "verify_archive", reject_archive)
    with pytest.raises(RuntimeError, match="synthetic verification failure"):
        release.build(root, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".ghostwatch-release-*.tmp"))


def test_archive_rescan_rejects_unknown_binary_member(tmp_path):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("docs/unapproved-dump.bin", b"unapproved payload")

    with pytest.raises(RuntimeError, match="unknown file type"):
        release.verify_archive(path)

