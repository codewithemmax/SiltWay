"""Unit and integration tests for historical flood exposure calculation F(e)."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from floodrouter.flood import (
    attach_flood_exposure_to_graph,
    build_flood_from_config,
    edge_flood_fraction,
    plot_flood_sanity_map,
    validate_flood_exposure,
)


def test_edge_flood_fraction_exact_and_dissolved() -> None:
    """F(e) is 1.0 inside, 0.0 outside, 0.5 half-covered, and dissolves overlapping polygons."""
    e_full = LineString([(6.001, 7.001), (6.009, 7.001)])
    e_half = LineString([(6.005, 7.005), (6.015, 7.005)])
    e_none = LineString([(6.050, 7.050), (6.060, 7.050)])

    edges_df = pd.DataFrame(
        {
            "edge_id": [0, 1, 2],
            "u": [0, 1, 2],
            "v": [1, 2, 3],
            "length_m": [1000.0, 1000.0, 1000.0],
            "flood_frac": [0.0, 0.0, 0.0],
            "geometry": [e_full, e_half, e_none],
        }
    )

    poly_1 = box(6.000, 7.000, 6.010, 7.010)
    poly_2_overlap = box(6.000, 7.000, 6.008, 7.008)

    fracs = edge_flood_fraction(edges_df, [poly_1, poly_2_overlap])
    assert len(fracs) == 3
    assert np.all((fracs >= 0.0) & (fracs <= 1.0))
    assert fracs[0] == pytest.approx(1.0, abs=1e-4)
    assert fracs[1] == pytest.approx(0.5, abs=1e-3)
    assert fracs[2] == pytest.approx(0.0, abs=1e-6)


def test_validate_flood_exposure_rejects_invalid_values() -> None:
    """Validation rejects flood_frac outside [0, 1]."""
    bad_df = pd.DataFrame(
        {
            "edge_id": [0],
            "u": [0],
            "v": [1],
            "length_m": [500.0],
            "flood_frac": [1.2],
            "geometry": [LineString([(6.0, 7.0), (6.01, 7.0)])],
        }
    )
    with pytest.raises(ValueError, match="outside"):
        validate_flood_exposure(bad_df)


def test_attach_flood_and_plot_sanity_map(tmp_path: Path) -> None:
    """Graph attributes update cleanly and sanity map PNG is written."""
    geom = LineString([(6.001, 7.001), (6.009, 7.001)])
    edges_df = pd.DataFrame(
        {
            "edge_id": [0],
            "u": [0],
            "v": [1],
            "length_m": [900.0],
            "flood_frac": [0.0],
            "geometry": [geom],
        }
    )
    graph = nx.Graph()
    graph.add_edge(0, 1, edge_id=0, length_m=900.0, flood_frac=0.0, geometry=geom)

    poly = box(6.000, 7.000, 6.010, 7.010)
    boundary = box(5.990, 6.990, 6.020, 7.020)
    fracs = np.array([0.75], dtype=np.float64)

    graph, updated_df = attach_flood_exposure_to_graph(graph, edges_df, fracs, [poly])
    assert graph.edges[0, 1]["flood_frac"] == pytest.approx(0.75)
    assert updated_df["flood_frac"].iloc[0] == pytest.approx(0.75)

    out_png = tmp_path / "sanity.png"
    plot_flood_sanity_map(updated_df, [poly], boundary, out_png)
    assert out_png.exists()
    assert out_png.stat().st_size > 1000


def test_real_flood_build_from_config() -> None:
    """Running build_flood_from_config computes 2012 F(e) on the pilot graph and saves map."""
    stats = build_flood_from_config("config/default.yaml")
    assert stats["n_edges"] > 100
    assert 0 < stats["n_flooded_edges"] < stats["n_edges"]
    assert 0.0 < stats["flood_edge_share"] < 1.0
    assert stats["total_flood_km"] > 0.0
    assert 0.0 <= stats["min_flood_frac"] <= stats["max_flood_frac"] <= 1.0
    assert Path(stats["sanity_map_path"]).exists()
