"""Unit tests for road ingestion, topology noding, endpoint snapping, and graph build."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import shapely
from shapely.geometry import LineString, Point

from floodrouter.config import load_config
from floodrouter.ingest import (
    estimate_utm_zone,
    load_region_boundary,
    load_roads,
    project_to_metric,
    project_to_storage,
)
from floodrouter.network import (
    build_graph,
    load_edges_parquet,
    load_graph_pickle,
    save_network_artifacts,
    validate_network,
)


def test_projection_roundtrip_preserves_coordinates() -> None:
    """Forward and inverse local metric projection recovers storage coordinates."""
    line = LineString([(5.0, 7.0), (5.2, 7.3)])
    utm = estimate_utm_zone(5.1, 7.15)
    assert utm["zone"] >= 1
    metric_line = project_to_metric(line, 5.1, 7.15)
    assert metric_line.length > 10000.0
    recovered = project_to_storage(metric_line, 5.1, 7.15)
    assert shapely.equals_exact(line, recovered, tolerance=1e-9)


def test_build_graph_nodes_intersections_and_snaps_endpoints() -> None:
    """Crossing lines are split at their intersection and close endpoints snap together."""
    cross_1 = LineString([(6.000, 7.001), (6.002, 7.001)])
    cross_2 = LineString([(6.001, 7.000), (6.001, 7.002)])
    near_extension = LineString([(6.00201, 7.001), (6.003, 7.001)])
    disconnected_island = LineString([(6.100, 7.100), (6.101, 7.100)])

    roads_df = pd.DataFrame(
        {
            "road_id": ["r1", "r2", "r3", "r4"],
            "road_class": ["primary", "primary", "secondary", "tertiary"],
            "geometry": [cross_1, cross_2, near_extension, disconnected_island],
        }
    )

    graph, edges_df = build_graph(
        roads=roads_df,
        snap_tolerance_m=5.0,
        keep_largest_component=True,
    )

    stats = graph.graph["stats"]
    assert stats["n_components_before"] == 2
    assert stats["n_components_dropped"] == 1
    assert stats["n_components_after"] == 1
    assert graph.number_of_nodes() == 6
    assert graph.number_of_edges() == 5
    assert len(edges_df) == 5
    assert set(edges_df.columns) == {"edge_id", "u", "v", "length_m", "flood_frac", "geometry"}


def test_build_graph_preserves_parallel_edges_via_midpoint_split() -> None:
    """Two roads connecting the same endpoints are both kept in simple nx.Graph."""
    direct_line = LineString([(6.000, 7.000), (6.010, 7.000)])
    detour_line = LineString([(6.000, 7.000), (6.005, 7.005), (6.010, 7.000)])
    roads_df = pd.DataFrame(
        {
            "road_id": ["direct", "detour"],
            "road_class": ["primary", "secondary"],
            "geometry": [direct_line, detour_line],
        }
    )

    graph, edges_df = build_graph(
        roads=roads_df,
        snap_tolerance_m=5.0,
        keep_largest_component=True,
    )
    assert graph.number_of_nodes() == 3
    assert graph.number_of_edges() == 3
    assert len(edges_df) == 3


def test_save_and_load_network_artifacts(tmp_path: Path) -> None:
    """Edges Parquet (WKB) and graph pickle roundtrip cleanly."""
    line_a = LineString([(6.000, 7.000), (6.005, 7.000)])
    line_b = LineString([(6.005, 7.000), (6.005, 7.005)])
    roads_df = pd.DataFrame(
        {
            "road_id": ["a", "b"],
            "road_class": ["primary", "primary"],
            "geometry": [line_a, line_b],
        }
    )
    graph, edges_df = build_graph(roads_df, snap_tolerance_m=5.0)
    edges_path = tmp_path / "edges.parquet"
    graph_path = tmp_path / "graph.pkl"

    save_network_artifacts(graph, edges_df, edges_path, graph_path)
    loaded_edges = load_edges_parquet(edges_path)
    loaded_graph = load_graph_pickle(graph_path)

    assert len(loaded_edges) == len(edges_df)
    assert loaded_edges["geometry"].iloc[0].geom_type == "LineString"
    assert loaded_graph.number_of_nodes() == graph.number_of_nodes()
    assert loaded_graph.number_of_edges() == graph.number_of_edges()


def test_build_graph_rejects_invalid_geometry_share() -> None:
    """Validation fails when share of valid LineString geometries is below threshold."""
    roads_df = pd.DataFrame(
        {
            "road_id": ["good", "bad"],
            "road_class": ["primary", "primary"],
            "geometry": [LineString([(6.0, 7.0), (6.1, 7.0)]), Point(6.0, 7.0)],
        }
    )
    with pytest.raises(ValueError, match="Valid geometry share"):
        build_graph(roads_df, snap_tolerance_m=5.0, min_valid_geom_share=0.99)


def test_real_config_pipeline_builds_and_validates() -> None:
    """End-to-end ingest and network build from config/default.yaml passes validation."""
    cfg = load_config("config/default.yaml")
    boundary = load_region_boundary(
        boundary_path=cfg.region.boundary_file,
        region_name=cfg.region.name,
        name_column=cfg.region.name_column or "adm1_name",
    )
    assert not boundary.is_empty
    roads_df = load_roads(
        path=cfg.datasets.roads.path,
        region=boundary,
        road_class_column=cfg.datasets.roads.road_class_column,
        unknown_class_value=cfg.datasets.roads.unknown_class_value,
        include_unknown_roads=cfg.network.include_unknown_roads,
    )
    assert len(roads_df) > 100
    assert all(g.geom_type == "LineString" for g in roads_df["geometry"])

    graph, edges_df = build_graph(
        roads=roads_df,
        snap_tolerance_m=cfg.network.snap_tolerance_m,
        keep_largest_component=cfg.network.keep_largest_component,
        min_valid_geom_share=cfg.network.min_valid_geom_share,
    )
    stats = validate_network(graph, edges_df, cfg)
    assert stats["edge_valid_geom_share"] == 1.0
    assert stats["n_components_after"] == 1
    assert stats["n_nodes_after"] > 100
    assert stats["n_edges_after"] > 100
