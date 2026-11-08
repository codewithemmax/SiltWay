"""Integration and error-handling tests for the FastAPI routing service."""

from __future__ import annotations

import copy
import time

import pytest
from fastapi.testclient import TestClient

from service.main import ServiceState, create_app


@pytest.fixture(scope="module")
def shared_state() -> ServiceState:
    """Load ServiceState once for the test module."""
    return ServiceState("config/default.yaml")


@pytest.fixture(scope="module")
def client(shared_state: ServiceState) -> TestClient:
    """Create a TestClient backed by shared_state."""
    app = create_app(state=shared_state)
    return TestClient(app)


def test_health_endpoint(client: TestClient) -> None:
    """GET /health returns status ok and positive node/edge counts."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["graph_nodes"] > 0
    assert body["graph_edges"] > 0


def test_meta_endpoint(client: TestClient) -> None:
    """GET /meta returns bbox, alpha, beta, as_of_default, and data_manifest."""
    resp = client.get("/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["bbox"]) == 4
    assert body["bbox"][0] < body["bbox"][2]
    assert body["bbox"][1] < body["bbox"][3]
    assert body["alpha"] >= 0.0
    assert body["beta"] >= 0.0
    assert len(body["as_of_default"]) == 10
    assert isinstance(body["data_manifest"], list)
    assert len(body["data_manifest"]) > 0


def test_flood_layer_endpoint(client: TestClient) -> None:
    """GET /flood-layer returns a valid GeoJSON FeatureCollection."""
    resp = client.get("/flood-layer")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert isinstance(body["features"], list)
    assert len(body["features"]) > 0
    first = body["features"][0]
    assert first["type"] == "Feature"
    assert first["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_post_route_success_and_latency(
    client: TestClient,
    shared_state: ServiceState,
) -> None:
    """POST /route returns valid baseline/resilient routes and meets <500 ms median latency."""
    assert len(shared_state.demo_scenarios) > 0
    scenario = shared_state.demo_scenarios[-1]

    payload = {
        "origin": scenario["origin"],
        "destination": scenario["destination"],
    }
    t0 = time.perf_counter()
    resp = client.post("/route", json=payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline"]["geometry"]["type"] == "LineString"
    assert body["resilient"]["geometry"]["type"] == "LineString"
    assert body["baseline"]["length_km"] > 0.0
    assert body["resilient"]["length_km"] >= body["baseline"]["length_km"] - 1e-9
    assert body["params"]["alpha"] == shared_state.config.weights.alpha
    assert body["params"]["beta"] == shared_state.config.weights.beta
    assert body["params"]["as_of"] == shared_state.config.rainfall.as_of_default
    assert body["snap"]["origin_m"] >= 0.0
    assert body["snap"]["destination_m"] >= 0.0
    assert elapsed_ms < 500.0


def test_post_route_zero_weights_reproduces_baseline(
    client: TestClient,
    shared_state: ServiceState,
) -> None:
    """POST /route with alpha=0, beta=0 returns identical length_km and flood_km as baseline."""
    scenario = shared_state.demo_scenarios[0]
    payload = {
        "origin": scenario["origin"],
        "destination": scenario["destination"],
        "alpha": 0.0,
        "beta": 0.0,
    }
    resp = client.post("/route", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline"]["length_km"] == pytest.approx(body["resilient"]["length_km"])
    assert body["baseline"]["flood_km"] == pytest.approx(body["resilient"]["flood_km"])
    assert body["delta"]["extra_distance_pct"] == pytest.approx(0.0)


def test_post_route_zero_baseline_flood_returns_null_reduction(
    client: TestClient,
    shared_state: ServiceState,
) -> None:
    """When baseline flood_km is 0, delta.flood_km_reduction_pct is null."""
    # Find an edge with flood_frac == 0 and route between its endpoints
    dry_edge = None
    for u, v, data in shared_state.graph.edges(data=True):
        if float(data.get("flood_frac", 0.0)) == 0.0 and float(data.get("length_m", 0.0)) > 100.0:
            dry_edge = (u, v)
            break
    assert dry_edge is not None
    u_node = shared_state.graph.nodes[dry_edge[0]]
    v_node = shared_state.graph.nodes[dry_edge[1]]

    resp = client.post(
        "/route",
        json={
            "origin": {"lat": float(u_node["lat"]), "lon": float(u_node["lon"])},
            "destination": {"lat": float(v_node["lat"]), "lon": float(v_node["lon"])},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline"]["flood_km"] == 0.0
    assert body["delta"]["flood_km_reduction_pct"] is None


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"origin": {"lat": 95.0, "lon": 6.7}, "destination": {"lat": 7.8, "lon": 6.7}},
        {"origin": {"lat": 7.8, "lon": -190.0}, "destination": {"lat": 7.8, "lon": 6.7}},
        {
            "origin": {"lat": 7.8, "lon": 6.7},
            "destination": {"lat": 7.8, "lon": 6.7},
            "alpha": -1.0,
        },
        {
            "origin": {"lat": 7.8, "lon": 6.7},
            "destination": {"lat": 7.8, "lon": 6.7},
            "alpha": 999.0,
        },
        {
            "origin": {"lat": 7.8, "lon": 6.7},
            "destination": {"lat": 7.8, "lon": 6.7},
            "beta": -0.5,
        },
        {
            "origin": {"lat": 7.8, "lon": 6.7},
            "destination": {"lat": 7.8, "lon": 6.7},
            "as_of": "not-a-valid-date",
        },
    ],
)
def test_post_route_invalid_input_returns_422(
    client: TestClient,
    bad_payload: dict,
) -> None:
    """Invalid coordinates, out-of-range alpha/beta, or bad as_of return 422 INVALID_INPUT."""
    resp = client.post("/route", json=bad_payload)
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "INVALID_INPUT"
    assert isinstance(body["error"]["message"], str)


def test_post_route_out_of_coverage_returns_400(client: TestClient) -> None:
    """Coordinates exceeding max_snap_distance_m return 400 OUT_OF_COVERAGE."""
    resp = client.post(
        "/route",
        json={
            "origin": {"lat": 0.0, "lon": 0.0},
            "destination": {"lat": 0.1, "lon": 0.1},
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "OUT_OF_COVERAGE"


def test_post_route_no_path_returns_404(shared_state: ServiceState) -> None:
    """Disconnected graph nodes within snap distance return 404 NO_PATH."""
    isolated_state = copy.copy(shared_state)
    G_copy = shared_state.graph.copy()
    G_copy.graph = dict(shared_state.graph.graph)
    G_copy.graph.pop("_spatial_index", None)

    # Add an isolated node slightly offset from an existing node so it is within snap tolerance
    first_nid = next(iter(G_copy.nodes))
    base_nd = G_copy.nodes[first_nid]
    new_nid = max(int(n) for n in G_copy.nodes) + 1000
    G_copy.add_node(
        new_nid,
        x=float(base_nd["x"]) + 50000.0,
        y=float(base_nd["y"]) + 50000.0,
        lon=float(base_nd["lon"]) + 0.45,
        lat=float(base_nd["lat"]) + 0.45,
    )
    isolated_state.graph = G_copy
    local_client = TestClient(create_app(state=isolated_state))

    resp = local_client.post(
        "/route",
        json={
            "origin": {"lat": float(base_nd["lat"]), "lon": float(base_nd["lon"])},
            "destination": {
                "lat": float(base_nd["lat"]) + 0.45,
                "lon": float(base_nd["lon"]) + 0.45,
            },
        },
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NO_PATH"
