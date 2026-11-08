"""Data manifest writer and reader for dataset provenance and checksums."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ManifestEntry(BaseModel):
    """Single dataset provenance record in data/processed/manifest.json."""

    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(..., min_length=1)
    row_count: int = Field(..., ge=0)
    checksum: str = Field(..., min_length=1)
    source_url: str = Field(..., min_length=1)
    access_date: str = Field(..., min_length=1)
    license: str = Field(..., min_length=1)
    dataset_key: str | None = Field(default=None)
    version: str | None = Field(default=None)

    @field_validator("file_name", "checksum", "source_url", "access_date", "license")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Manifest string fields cannot be blank.")
        return stripped


def compute_sha256(path: str | Path) -> str:
    """Compute SHA-256 hex digest for a file or directory of files."""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Cannot compute checksum for missing path: {target}")

    hasher = hashlib.sha256()
    if target.is_file():
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    files = sorted(p for p in target.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"Cannot compute checksum for empty directory: {target}")

    for file_path in files:
        rel_path = file_path.relative_to(target).as_posix().encode("utf-8")
        hasher.update(rel_path)
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def create_manifest_entry(
    file_path: str | Path,
    row_count: int,
    source_url: str,
    license: str,
    access_date: str | None = None,
    dataset_key: str | None = None,
    version: str | None = None,
) -> ManifestEntry:
    """Create a validated ManifestEntry by computing the SHA-256 checksum of file_path."""
    target = Path(file_path)
    checksum = compute_sha256(target)
    resolved_date = access_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ManifestEntry(
        file_name=target.name,
        row_count=row_count,
        checksum=checksum,
        source_url=source_url,
        access_date=resolved_date,
        license=license,
        dataset_key=dataset_key,
        version=version,
    )


def write_manifest(entries: list[ManifestEntry], manifest_path: str | Path) -> Path:
    """Write validated manifest entries to manifest.json."""
    out_path = Path(manifest_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "datasets": [entry.model_dump(mode="json") for entry in entries],
    }
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return out_path


def read_manifest(manifest_path: str | Path) -> list[ManifestEntry]:
    """Read and validate manifest entries from manifest.json."""
    in_path = Path(manifest_path)
    if not in_path.exists():
        return []
    with in_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_entries = payload.get("datasets", []) if isinstance(payload, dict) else payload
    return [ManifestEntry.model_validate(item) for item in raw_entries]


def upsert_manifest_entry(
    entry: ManifestEntry,
    manifest_path: str | Path,
) -> list[ManifestEntry]:
    """Insert or update a ManifestEntry in manifest.json and return all entries."""
    existing = read_manifest(manifest_path)
    updated: list[ManifestEntry] = []
    replaced = False
    for item in existing:
        same_key = entry.dataset_key is not None and item.dataset_key == entry.dataset_key
        same_file = item.file_name == entry.file_name
        if same_key or same_file:
            if not replaced:
                updated.append(entry)
                replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(entry)
    write_manifest(updated, manifest_path)
    return updated
