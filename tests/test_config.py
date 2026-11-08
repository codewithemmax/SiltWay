"""Unit tests for configuration loader and Pydantic schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from floodrouter.config import load_config


def test_default_config_fails_when_region_missing() -> None:
    """Loading config with region.name set to None must fail clearly."""
    with pytest.raises(ValidationError) as exc_info:
        load_config("config/default.yaml", overrides={"region": {"name": None}})
    error_text = str(exc_info.value)
    assert "region" in error_text
    assert "name" in error_text


def test_default_config_fails_when_region_blank() -> None:
    """Blank or whitespace-only region.name must fail validation."""
    with pytest.raises(ValidationError) as exc_info:
        load_config("config/default.yaml", overrides={"region": {"name": "   "}})
    assert "region.name" in str(exc_info.value)


def test_load_config_succeeds_with_valid_region_override() -> None:
    """Providing a non-empty region.name allows config/default.yaml to validate."""
    cfg = load_config(
        "config/default.yaml",
        overrides={"region": {"name": "SampleAdminRegion"}},
    )
    assert cfg.project_name == "flood-router"
    assert cfg.region.name == "SampleAdminRegion"
    assert cfg.crs.storage == "EPSG:4326"
    assert cfg.crs.projected == "auto_utm"
    assert cfg.network.include_unknown_roads is True
    assert cfg.datasets.rainfall.parameter == "PRECTOTCORR"


def test_load_config_rejects_unknown_fields(tmp_path: Path) -> None:
    """Extra/unrecognized configuration keys must be rejected by schema validation."""
    with pytest.raises(ValidationError):
        load_config(
            "config/default.yaml",
            overrides={
                "region": {"name": "SampleAdminRegion"},
                "unexpected_top_level_key": 123,
            },
        )


def test_load_config_rejects_negative_weights() -> None:
    """Negative alpha or beta weights must fail validation."""
    with pytest.raises(ValidationError):
        load_config(
            "config/default.yaml",
            overrides={
                "region": {"name": "SampleAdminRegion"},
                "weights": {"alpha": -1.0},
            },
        )


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    """Missing configuration file path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.yaml")
