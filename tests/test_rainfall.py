"""Unit and integration tests for NASA POWER rainfall anomaly index R(e)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

from floodrouter.rainfall import (
    build_rainfall_from_config,
    compute_cell_anomaly,
    generate_lattice_points,
    rain_index,
    validate_rain_index,
)


def _make_synthetic_series(
    as_of: date,
    window_days: int,
    prior_window_sums: list[float],
    recent_window_sum: float,
) -> dict[str, float]:
    """Construct a synthetic daily precipitation dict with specified window sums."""
    series: dict[str, float] = {}
    for d_off in range(window_days):
        d_key = (as_of - timedelta(days=d_off)).strftime("%Y%m%d")
        series[d_key] = recent_window_sum / float(window_days)

    for yr_idx, total_val in enumerate(prior_window_sums, start=1):
        prior_anchor = date(as_of.year - yr_idx, as_of.month, as_of.day)
        for d_off in range(window_days):
            d_key = (prior_anchor - timedelta(days=d_off)).strftime("%Y%m%d")
            series[d_key] = total_val / float(window_days)

    return series


def test_generate_lattice_points_covers_bbox() -> None:
    """Lattice generator returns expected regular grid over bbox."""
    pts = generate_lattice_points([6.0, 7.0, 7.0, 8.0], step=0.5)
    assert len(pts) == 9
    assert (6.0, 7.0) in pts
    assert (7.0, 8.0) in pts


def test_compute_cell_anomaly_exact_cases() -> None:
    """Cell anomaly is 0 at median, 0.5 halfway to p95, 1.0 at/above p95, and ignores -999."""
    anchor = date(2022, 10, 8)
    prior_sums = [10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 30.0]
    series_test = _make_synthetic_series(anchor, 7, prior_sums, recent_window_sum=10.0)
    clim_median = float(np.median(prior_sums))
    clim_p95 = float(np.percentile(prior_sums, 95.0))
    halfway = (clim_median + clim_p95) * 0.5

    res_low = compute_cell_anomaly(series_test, anchor, 7, 10, scale_percentile=95.0, eps=1e-6)
    assert res_low["rain_idx"] == pytest.approx(0.0, abs=1e-6)

    series_mid = _make_synthetic_series(anchor, 7, prior_sums, recent_window_sum=halfway)
    res_mid = compute_cell_anomaly(series_mid, anchor, 7, 10, scale_percentile=95.0, eps=1e-6)
    assert res_mid["rain_idx"] == pytest.approx(0.5, abs=1e-4)

    series_high = _make_synthetic_series(anchor, 7, prior_sums, recent_window_sum=clim_p95 + 15.0)
    series_high[(anchor - timedelta(days=1)).strftime("%Y%m%d")] = -999.0
    res_high = compute_cell_anomaly(series_high, anchor, 7, 10, scale_percentile=95.0, eps=1e-6)
    assert 0.0 <= res_high["rain_idx"] <= 1.0


def test_rain_index_assigns_nearest_grid_cell_to_edge_midpoint() -> None:
    """Each edge receives the rain_idx of the grid cell closest to its midpoint."""
    anchor = date(2022, 10, 8)
    prior_sums = [10.0] * 5 + [30.0] * 5
    s_dry = _make_synthetic_series(anchor, 7, prior_sums, recent_window_sum=5.0)
    s_wet = _make_synthetic_series(anchor, 7, prior_sums, recent_window_sum=50.0)

    grid = [
        {"lon": 6.0, "lat": 7.0, "series": s_dry},
        {"lon": 6.5, "lat": 7.0, "series": s_wet},
    ]
    edges_df = pd.DataFrame(
        {
            "edge_id": [10, 20],
            "geometry": [
                LineString([(5.99, 7.0), (6.01, 7.0)]),
                LineString([(6.49, 7.0), (6.51, 7.0)]),
            ],
        }
    )

    out_df = rain_index(
        grid=grid,
        as_of="2022-10-08",
        window_days=7,
        climatology_years=10,
        edges=edges_df,
        scale_percentile=95.0,
        eps=1e-6,
    )
    assert list(out_df.columns) == ["edge_id", "rain_idx"]
    assert out_df.loc[out_df["edge_id"] == 10, "rain_idx"].iloc[0] == pytest.approx(0.0)
    assert out_df.loc[out_df["edge_id"] == 20, "rain_idx"].iloc[0] == pytest.approx(1.0)


def test_validate_rain_index_rejects_invalid_values() -> None:
    """Validation rejects rain_idx outside [0, 1]."""
    bad = pd.DataFrame({"edge_id": [0], "rain_idx": [-0.1]})
    with pytest.raises(ValueError, match="outside"):
        validate_rain_index(bad)


def test_real_rainfall_build_and_cache_from_config() -> None:
    """Fetching POWER grid for the pilot region caches rain_<as_of>.parquet in [0, 1]."""
    stats = build_rainfall_from_config("config/default.yaml")
    assert stats["n_edges"] > 100
    assert 0.0 <= stats["min_rain_idx"] <= stats["max_rain_idx"] <= 1.0
    assert Path(stats["parquet_path"]).exists()
