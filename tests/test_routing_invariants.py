"""Tests for the 5 required routing invariants and error handling."""

from __future__ import annotations

import random

import networkx as nx
import numpy as np
import pytest
from shapely.geometry import LineString

from floodrouter.config import load_config
from floodrouter.network import load_edges_parquet, load_graph_pickle
from floodrouter.rainfall import attach_rain_index_to_graph, get_or_compute_rain_index
from floodrouter.routing import (
    RoutingError,
    compare_routes,
    compute_route_delta,
    route_between_nodes,
    snap_point_to_graph,
)
from floodrouter.weights import apply_weights_to_graph, compute_edge_weight


def _load_prepared_pilot_graph() -> nx.Graph:
    """Load the prebuilt pilot graph and attach cached rain_idx."""
    cfg = load_config("config/default.yaml")
    graph = load_graph_pickle(cfg.paths.graph_path)
    edges_df = load_edges_parquet(cfg.paths.edges_path)
    crs_info = graph.graph.get("crs", {})
    rain_df = get_or_compute_rain_index(
        config=cfg,
        edges_df=edges_df,
        as_of=cfg.rainfall.as_of_default,
        lon0=crs_info.get("lon0"),
        lat0=crs_info.get("lat0"),
    )
    return attach_rain_index_to_graph(graph, rain_df)


def test_invariant_1_zero_alpha_beta_equals_baseline() -> None:
    """Invariant 1: alpha = beta = 0 gives identical path and length as baseline."""
    graph = _load_prepared_pilot_graph()
    rng = random.Random(42)
    nodes = list(graph.nodes())

    for _ in range(30):
        u, v = rng.sample(nodes, 2)
        res = route_between_nodes(graph, u, v, alpha=0.0, beta=0.0)
        assert res["resilient"]["nodes"] == res["baseline"]["nodes"]
        assert res["resilient"]["length_km"] == pytest.approx(
            res["baseline"]["length_km"], abs=1e-12
        )
        assert res["resilient"]["flood_km"] == pytest.approx(
            res["baseline"]["flood_km"], abs=1e-12
        )


def test_invariant_2_resilient_flood_km_le_baseline_when_beta_zero() -> None:
    """Invariant 2: Resilient flood_km <= baseline flood_km on random pairs when beta = 0."""
    graph = _load_prepared_pilot_graph()
    rng = random.Random(42)
    nodes = list(graph.nodes())

    for alpha in (1.0, 3.0, 8.0):
        for _ in range(30):
            u, v = rng.sample(nodes, 2)
            res = route_between_nodes(graph, u, v, alpha=alpha, beta=0.0)
            assert res["resilient"]["flood_km"] <= res["baseline"]["flood_km"] + 1e-9


def test_invariant_3_resilient_length_km_ge_baseline() -> None:
    """Invariant 3: Resilient length_km >= baseline length_km (baseline is length-optimal)."""
    graph = _load_prepared_pilot_graph()
    rng = random.Random(42)
    nodes = list(graph.nodes())

    for _ in range(40):
        u, v = rng.sample(nodes, 2)
        res = route_between_nodes(graph, u, v, alpha=3.0, beta=1.0)
        assert res["resilient"]["length_km"] + 1e-9 >= res["baseline"]["length_km"]
        assert res["resilient"]["weighted_cost"] <= res["baseline"]["weighted_cost"] + 1e-9


def test_invariant_4_all_edge_weights_finite_and_non_negative() -> None:
    """Invariant 4: All edge weights are finite and non-negative."""
    graph = _load_prepared_pilot_graph()
    apply_weights_to_graph(graph, alpha=3.0, beta=1.0)

    weights = [float(d["weight"]) for _, _, d in graph.edges(data=True)]
    lengths = [float(d["length_km"]) for _, _, d in graph.edges(data=True)]
    assert len(weights) == graph.number_of_edges()
    assert all(np.isfinite(w) and w >= 0.0 for w in weights)
    assert all(np.isfinite(lk) and lk > 0.0 for lk in lengths)

    with pytest.raises(ValueError, match="non-negative"):
        compute_edge_weight(1.0, 0.5, 0.5, alpha=-1.0, beta=0.0)


def test_invariant_5_routes_are_connected_paths_from_origin_to_destination() -> None:
    """Invariant 5: Routes are connected paths from origin node to destination node."""
    graph = _load_prepared_pilot_graph()
    rng = random.Random(42)
    nodes = list(graph.nodes())

    for _ in range(30):
        u, v = rng.sample(nodes, 2)
        res = route_between_nodes(graph, u, v, alpha=4.0, beta=1.0)
        for key in ("baseline", "resilient"):
            path_nodes = res[key]["nodes"]
            assert path_nodes[0] == u
            assert path_nodes[-1] == v
            assert res[key]["n_edges"] == len(path_nodes) - 1
            for idx in range(len(path_nodes) - 1):
                assert graph.has_edge(path_nodes[idx], path_nodes[idx + 1])
            geom = res[key]["geometry"]
            assert geom["type"] == "LineString"
            assert len(geom["coordinates"]) >= 2


def test_routing_error_codes_and_zero_denominator_delta() -> None:
    """OUT_OF_COVERAGE, NO_PATH, INVALID_INPUT, and null flood reduction on 0 denominator."""
    delta_zero = compute_route_delta(
        baseline_length_km=10.0,
        baseline_flood_km=0.0,
        resilient_length_km=10.0,
        resilient_flood_km=0.0,
    )
    assert delta_zero["flood_km_reduction_pct"] is None
    assert delta_zero["extra_distance_pct"] == pytest.approx(0.0)

    g = nx.Graph()
    g.graph["crs"] = {"lon0": 6.5, "lat0": 7.5}
    g.add_node(0, x=0.0, y=0.0, lon=6.5, lat=7.5)
    g.add_node(1, x=1000.0, y=0.0, lon=6.51, lat=7.5)
    g.add_node(2, x=2000.0, y=0.0, lon=6.52, lat=7.5)
    g.add_edge(
        0,
        1,
        edge_id=0,
        length_m=1000.0,
        flood_frac=0.5,
        rain_idx=0.2,
        geometry=LineString([(6.5, 7.5), (6.51, 7.5)]),
    )

    with pytest.raises(RoutingError) as exc_invalid:
        snap_point_to_graph(g, lat=95.0, lon=6.5, max_snap_distance_m=2000.0)
    assert exc_invalid.value.code == "INVALID_INPUT"

    with pytest.raises(RoutingError) as exc_cov:
        snap_point_to_graph(g, lat=8.5, lon=8.5, max_snap_distance_m=2000.0)
    assert exc_cov.value.code == "OUT_OF_COVERAGE"

    with pytest.raises(RoutingError) as exc_nopath:
        route_between_nodes(g, origin_node=0, destination_node=2, alpha=3.0, beta=1.0)
    assert exc_nopath.value.code == "NO_PATH"

    comp = compare_routes(
        graph=g,
        origin={"lat": 7.5, "lon": 6.5},
        destination={"lat": 7.5, "lon": 6.51},
        alpha=3.0,
        beta=1.0,
        max_snap_distance_m=2000.0,
        as_of="2022-10-08",
    )
    assert comp["baseline"]["length_km"] == pytest.approx(1.0)
    assert comp["resilient"]["length_km"] == pytest.approx(1.0)
