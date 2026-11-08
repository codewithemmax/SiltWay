"""Unit tests for dataset manifest creation, writing, updating, and reading."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from floodrouter.manifest import (
    ManifestEntry,
    compute_sha256,
    create_manifest_entry,
    read_manifest,
    upsert_manifest_entry,
    write_manifest,
)


def test_compute_sha256_file_and_directory(tmp_path: Path) -> None:
    """Checksum calculation matches standard hashlib SHA-256 and is deterministic."""
    sample_file = tmp_path / "dataset.geojson"
    payload = b'{"type": "FeatureCollection", "features": []}\n'
    sample_file.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()
    assert compute_sha256(sample_file) == expected

    sub_dir = tmp_path / "shp_dir"
    sub_dir.mkdir()
    (sub_dir / "part_a.bin").write_bytes(b"abc")
    (sub_dir / "part_b.bin").write_bytes(b"def")
    dir_hash_1 = compute_sha256(sub_dir)
    dir_hash_2 = compute_sha256(sub_dir)
    assert dir_hash_1 == dir_hash_2
    assert len(dir_hash_1) == 64


def test_create_and_write_manifest_roundtrip(tmp_path: Path) -> None:
    """Manifest writer stores file_name, row_count, checksum, source_url, access_date, license."""
    raw_file = tmp_path / "roads.gpkg"
    raw_file.write_bytes(b"binary-geopackage-placeholder")
    manifest_file = tmp_path / "processed" / "manifest.json"

    entry = create_manifest_entry(
        file_path=raw_file,
        row_count=450,
        source_url="https://example.org/roads",
        license="CC-BY-4.0",
        access_date="2026-11-08",
        dataset_key="roads",
        version="1.0",
    )

    write_manifest([entry], manifest_file)
    assert manifest_file.exists()

    loaded = read_manifest(manifest_file)
    assert len(loaded) == 1
    record = loaded[0]
    assert record.file_name == "roads.gpkg"
    assert record.row_count == 450
    assert record.checksum == compute_sha256(raw_file)
    assert record.source_url == "https://example.org/roads"
    assert record.access_date == "2026-11-08"
    assert record.license == "CC-BY-4.0"
    assert record.dataset_key == "roads"
    assert record.version == "1.0"


def test_upsert_manifest_entry_replaces_existing_by_key(tmp_path: Path) -> None:
    """Upserting an entry with the same dataset_key updates the existing record."""
    file_v1 = tmp_path / "flood.shp"
    file_v1.write_bytes(b"v1")
    manifest_file = tmp_path / "manifest.json"

    entry_1 = create_manifest_entry(
        file_path=file_v1,
        row_count=10,
        source_url="https://example.org/flood",
        license="ODbL",
        access_date="2026-11-08",
        dataset_key="flood_2012",
    )
    upsert_manifest_entry(entry_1, manifest_file)

    file_v1.write_bytes(b"v2-updated")
    entry_2 = create_manifest_entry(
        file_path=file_v1,
        row_count=25,
        source_url="https://example.org/flood",
        license="ODbL",
        access_date="2026-11-08",
        dataset_key="flood_2012",
    )
    updated = upsert_manifest_entry(entry_2, manifest_file)
    assert len(updated) == 1
    assert updated[0].row_count == 25
    assert updated[0].checksum == compute_sha256(file_v1)


def test_manifest_entry_rejects_invalid_fields() -> None:
    """Negative row counts or blank required fields fail validation."""
    with pytest.raises(ValidationError):
        ManifestEntry(
            file_name="roads.shp",
            row_count=-1,
            checksum="abc",
            source_url="https://example.org",
            access_date="2026-11-08",
            license="CC-BY-4.0",
        )

    with pytest.raises(ValidationError):
        ManifestEntry(
            file_name="   ",
            row_count=10,
            checksum="abc",
            source_url="https://example.org",
            access_date="2026-11-08",
            license="CC-BY-4.0",
        )
