"""Invariant tests for the dispatch backend: presets, recommendation, roads layer, batch."""

from __future__ import annotations

import csv
import io
import itertools

import pandas as pd
import pytest
import shapely
from fastapi.testclient import TestClient

from floodrouter.config import RoadZoomTierConfig, load_config
from floodrouter.rainfall import candidate_as_of_dates, select_wet_dry_dates
from floodrouter.roads_layer import build_roads_layer, classes_for_zoom
from floodrouter.routing import compute_route_recommendation
from service.batch import summarize_batch
from service.main import ServiceState, create_app
from service.schemas import BatchRowResult

SUM_FIELDS = (
    "baseline_length_km",
    "baseline_flood_km",
    "resilient_length_km",
    "resilient_flood_km",
    "avoided_flood_km",
    "extra_distance_km",
)


@pytest.fixture(scope="module")
def shared_state() -> ServiceState:
    return ServiceState("config/default.yaml")


@pytest.fixture(scope="module")
def client(shared_state: ServiceState) -> TestClient:
    return TestClient(create_app(state=shared_state))


# Recommendation status over a generated grid of inputs


def _expected_status(b_len, b_flood, r_len, r_flood, tol, max_pct) -> str:
    if b_flood <= 0.0:
        return "clear"
    if r_flood < b_flood - tol:
        pct = (r_len - b_len) / b_len * 100.0 if b_len > 0 else 0.0
        return "recommended" if pct <= max_pct else "trade_off"
    return "no_better_route"


def test_recommendation_status_grid_matches_rule() -> None:
    lengths = [1.0, 10.0, 80.0]
    extra_fracs = [0.0, 0.01, 0.05, 0.2, 1.0]
    floods = [0.0, 0.01, 0.5, 3.0]
    flood_drop_fracs = [0.0, 0.1, 0.5, 1.0]
    tolerances = [0.0, 0.05, 1.0]
    max_pcts = [0.0, 5.0, 50.0]
    seen: set[str] = set()
    for b_len, ef, b_fl, fd, tol, mp in itertools.product(
        lengths, extra_fracs, floods, flood_drop_fracs, tolerances, max_pcts
    ):
        r_len = b_len * (1 + ef)
        r_fl = b_fl * (1 - fd)
        rec = compute_route_recommendation(b_len, b_fl, r_len, r_fl, tol, mp)
        assert rec["status"] == _expected_status(b_len, b_fl, r_len, r_fl, tol, mp)
        assert rec["avoided_flood_km"] == pytest.approx(b_fl - r_fl)
        assert rec["extra_distance_km"] == pytest.approx(r_len - b_len)
        assert rec["flood_tolerance_km"] == tol
        assert rec["max_acceptable_detour_pct"] == mp
        seen.add(rec["status"])
    assert seen == {"clear", "recommended", "trade_off", "no_better_route"}


def test_recommendation_zero_baseline_length_does_not_divide() -> None:
    rec = compute_route_recommendation(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert rec["status"] == "clear"
    assert rec["extra_distance_pct"] == 0.0


# Presets and meta


def test_meta_serves_presets_exactly_as_configured(
    client: TestClient, shared_state: ServiceState
) -> None:
    body = client.get("/meta").json()
    cfg = shared_state.config
    assert body["presets"] == [p.model_dump() for p in cfg.presets]
    assert body["default_preset_id"] == cfg.ui.default_preset_id
    assert body["max_acceptable_detour_pct"] == cfg.ui.max_acceptable_detour_pct
    assert body["flood_tolerance_km"] == cfg.ui.flood_tolerance_km
    assert body["max_batch_pairs"] == cfg.service.max_batch_pairs
    assert any(p["alpha"] == 0.0 and p["beta"] == 0.0 for p in body["presets"])
    default = next(p for p in body["presets"] if p["id"] == body["default_preset_id"])
    assert (default["alpha"], default["beta"]) == (cfg.weights.alpha, cfg.weights.beta)


def test_meta_as_of_options_follow_rule(client: TestClient, shared_state: ServiceState) -> None:
    body = client.get("/meta").json()
    opts = body["as_of_options"]
    assert body["as_of_options_error"] is None
    rule = shared_state.config.ui.as_of_selection
    assert opts["rule"] == rule.description
    cands = candidate_as_of_dates(rule.start_date, rule.end_date, rule.step_days)
    assert opts["wet"]["as_of"] in cands
    assert opts["dry"]["as_of"] in cands
    key = "mean_recent_mm" if opts["metric"] == "recent_mm" else "mean_rain_idx"
    assert opts["wet"][key] >= opts["dry"][key]


def test_preset_list_is_rendered_from_config_overrides() -> None:
    cfg = load_config(
        "config/default.yaml",
        overrides={
            "presets": [
                {"id": "a", "label": "A", "alpha": 0.0, "beta": 0.0},
                {"id": "b", "label": "B", "alpha": 8.0, "beta": 0.5},
            ],
            "ui": {"default_preset_id": "b"},
        },
    )
    assert [p.id for p in cfg.presets] == ["a", "b"]


def test_default_preset_must_match_weights() -> None:
    with pytest.raises(ValueError):
        load_config(
            "config/default.yaml",
            overrides={
                "presets": [{"id": "x", "label": "X", "alpha": 1.0, "beta": 0.0}],
                "ui": {"default_preset_id": "x"},
            },
        )


def test_select_wet_dry_dates_is_extreme_and_ties_go_earliest() -> None:
    days = {f"2020{m:02d}{d:02d}": 0.0 for m in range(1, 13) for d in range(1, 29)}
    wet_series = dict(days)
    for d in range(10, 17):
        wet_series[f"202003{d:02d}"] = 10.0
    grid = [{"lon": 0.0, "lat": 0.0, "series": wet_series}]
    cands = ["2020-02-16", "2020-03-16", "2020-04-16"]
    out = select_wet_dry_dates(grid, cands, 7, 1, 100.0, 1.0, metric="recent_mm")
    assert out["wet"]["as_of"] == "2020-03-16"
    assert out["dry"]["as_of"] == "2020-02-16"


# Roads layer


def _synthetic_roads(n: int) -> pd.DataFrame:
    geoms = [
        shapely.LineString([(i * 0.01, 0.0), (i * 0.01, 0.005 + i * 0.0001)]) for i in range(n)
    ]
    classes = ["Major" if i % 2 == 0 else "Minor" for i in range(n)]
    return pd.DataFrame({"road_class": classes, "geometry": geoms})


def test_roads_layer_cap_sets_truncated_flag() -> None:
    roads = _synthetic_roads(40)
    bbox = [-1.0, -1.0, 1.0, 1.0]
    capped = build_roads_layer(roads, bbox, ["Major", "Minor"], 0.0, 10)
    assert capped["truncated"] is True
    assert capped["returned_features"] == 10
    assert capped["total_matching"] == 40
    assert all(f["properties"]["road_class"] == "Major" for f in capped["features"])

    uncapped = build_roads_layer(roads, bbox, ["Major", "Minor"], 0.0, 40)
    assert uncapped["truncated"] is False
    assert uncapped["returned_features"] == 40


def test_roads_layer_filters_class_and_bbox() -> None:
    roads = _synthetic_roads(40)
    out = build_roads_layer(roads, [-1.0, -1.0, 0.1, 1.0], ["Minor"], 0.0, 100)
    assert out["total_matching"] > 0
    for f in out["features"]:
        assert f["properties"]["road_class"] == "Minor"
        assert shapely.geometry.shape(f["geometry"]).bounds[0] <= 0.1


def test_classes_for_zoom_picks_highest_eligible_tier() -> None:
    tiers = [
        RoadZoomTierConfig(min_zoom=0, classes=["a"]),
        RoadZoomTierConfig(min_zoom=5, classes=["a", "b"]),
    ]
    assert classes_for_zoom(tiers, 0) == ["a"]
    assert classes_for_zoom(tiers, 4) == ["a"]
    assert classes_for_zoom(tiers, 5) == ["a", "b"]
    assert classes_for_zoom(tiers, 20) == ["a", "b"]


def test_roads_layer_endpoint_respects_configured_cap(
    client: TestClient, shared_state: ServiceState
) -> None:
    cap = shared_state.config.ui.roads_max_features
    top_zoom = max(t.min_zoom for t in shared_state.config.ui.road_classes_by_zoom)
    body = client.get("/roads-layer", params={"zoom": top_zoom}).json()
    assert body["type"] == "FeatureCollection"
    assert body["returned_features"] <= cap
    assert body["truncated"] == (body["total_matching"] > cap)


def test_roads_layer_endpoint_rejects_partial_bbox(client: TestClient) -> None:
    resp = client.get("/roads-layer", params={"min_lon": 0})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


# Route recommendation and flooded segments on the live endpoint


def test_route_includes_recommendation_and_flooded_segments(
    client: TestClient, shared_state: ServiceState
) -> None:
    sc = shared_state.demo_scenarios[0]
    body = client.post(
        "/route", json={"origin": sc["origin"], "destination": sc["destination"]}
    ).json()
    rec = body["recommendation"]
    assert rec["status"] in {"clear", "recommended", "trade_off", "no_better_route"}
    assert rec["baseline_flood_km"] == pytest.approx(body["baseline"]["flood_km"])
    for key in ("baseline", "resilient"):
        segs = body[key]["flooded_segments"]["features"]
        total = sum(f["properties"]["flood_km"] for f in segs)
        assert total == pytest.approx(body[key]["flood_km"])
        assert body[key]["steps"]
        assert body[key]["steps"][0]["instruction"].startswith("Start")
        assert all(step["road_label"] == "Mapped road segment" for step in body[key]["steps"])


def test_route_unknown_preset_returns_error(client: TestClient, shared_state: ServiceState) -> None:
    sc = shared_state.demo_scenarios[0]
    resp = client.post(
        "/route",
        json={"origin": sc["origin"], "destination": sc["destination"], "preset_id": "nope"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNKNOWN_PRESET"


# Batch


def _valid_rows(state: ServiceState, n: int) -> list[dict]:
    rows = []
    for sc in state.demo_scenarios[:n]:
        rows.append({"origin": sc["origin"], "destination": sc["destination"]})
    return rows


def test_batch_row_isolation_and_totals(client: TestClient, shared_state: ServiceState) -> None:
    good = _valid_rows(shared_state, 3)
    bad_coords = {"origin": {"lat": 999, "lon": 0}, "destination": {"lat": 0, "lon": 0}}
    out_of_coverage = {"origin_lat": 0.0, "origin_lon": 0.0, "dest_lat": 0.1, "dest_lon": 0.1}
    rows = [good[0], bad_coords, good[1], out_of_coverage, good[2]]
    body = client.post("/route/batch", json={"rows": rows}).json()

    statuses = [r["status"] for r in body["rows"]]
    assert statuses == ["ok", "error", "ok", "error", "ok"]
    assert body["rows"][1]["error_code"] == "INVALID_INPUT"
    assert body["rows"][3]["error_code"] == "OUT_OF_COVERAGE"

    totals = body["totals"]
    ok_rows = [r for r in body["rows"] if r["status"] == "ok"]
    assert totals["total_rows"] == 5
    assert totals["succeeded_rows"] == 3
    assert totals["failed_rows"] == 2
    for f in SUM_FIELDS:
        assert totals[f] == pytest.approx(sum(r[f] for r in ok_rows))
    assert sum(totals["status_counts"].values()) == 3

    # Each good row matches the single-route endpoint.
    single = client.post("/route", json=good[0]).json()
    assert body["rows"][0]["baseline_flood_km"] == pytest.approx(single["baseline"]["flood_km"])
    assert body["rows"][0]["resilient_length_km"] == pytest.approx(single["resilient"]["length_km"])


def test_batch_too_large_rejected(client: TestClient, shared_state: ServiceState) -> None:
    n = shared_state.config.service.max_batch_pairs + 1
    row = _valid_rows(shared_state, 1)[0]
    resp = client.post("/route/batch", json={"rows": [row] * n})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"


def test_batch_csv_export_has_rows_and_total(
    client: TestClient, shared_state: ServiceState
) -> None:
    rows = _valid_rows(shared_state, 2) + [{"origin": "bad"}]
    resp = client.post("/route/batch/csv", json={"rows": rows})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    parsed = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(parsed) == 4
    assert parsed[-1]["row"] == "TOTAL"
    ok = [r for r in parsed[:-1] if r["status"] == "ok"]
    assert float(parsed[-1]["baseline_length_km"]) == pytest.approx(
        sum(float(r["baseline_length_km"]) for r in ok)
    )


def test_summarize_batch_total_equals_sum_of_rows() -> None:
    rows = []
    for i in range(7):
        if i % 3 == 0:
            rows.append(BatchRowResult(row=i, status="error", error_code="X"))
            continue
        vals = {f: float(i + k) for k, f in enumerate(SUM_FIELDS)}
        rows.append(BatchRowResult(row=i, status="ok", recommendation="clear", **vals))
    totals = summarize_batch(rows)
    ok = [r for r in rows if r.status == "ok"]
    for f in SUM_FIELDS:
        assert getattr(totals, f) == pytest.approx(sum(getattr(r, f) for r in ok))
    assert totals.failed_rows == 3
