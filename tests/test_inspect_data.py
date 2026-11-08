"""Unit tests for the raw data inspection script."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.inspect_data import inspect_raw_directory


def test_inspect_raw_directory_with_geojson(tmp_path: Path) -> None:
    """Inspection script extracts CRS, columns, row count, geometry types, and bounds."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    sample_geojson = raw_dir / "sample_roads.geojson"
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"road_id": "R1", "road_class": "primary"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[1.0, 2.0], [3.0, 4.0]],
                },
            }
        ],
    }
    sample_geojson.write_text(json.dumps(payload), encoding="utf-8")

    out_md = tmp_path / "docs" / "data_inspection.md"
    report = inspect_raw_directory(
        raw_dir=raw_dir,
        output_md=out_md,
        config_path="config/default.yaml",
    )
    assert out_md.exists()
    assert "sample_roads.geojson" in report
    assert "EPSG:4326" in report
    assert "**Row Count**: 1" in report
    assert "LineString" in report
    assert "[1.0, 2.0, 3.0, 4.0]" in report
    assert "road_class" in report
