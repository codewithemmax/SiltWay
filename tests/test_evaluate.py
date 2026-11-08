"""Tests for floodrouter.evaluate (seeded OD sampling, summaries, and reproducibility)."""

from __future__ import annotations

import json
import math
from pathlib import Path

from floodrouter.evaluate import (
    evaluate_method_on_pairs,
    run_full_evaluation,
    sample_od_pairs,
    summarize_method,
)
from floodrouter.ingest import _open_shapefile_bytes
from floodrouter.network import load_graph_pickle


def test_open_shapefile_bytes_normalizes_backslashes() -> None:
    """_open_shapefile_bytes resolves nested zip member paths even with Windows backslashes."""
    spec_str = (
        r"data\raw\FL20221019NGA_SHP.zip!"
        r"FL20221019NGA_SHP\VIIRS_20221001_20221025_MaximumFloodWaterExtent_Nigeria.shp"
    )
    shp_b, shx_b, dbf_b = _open_shapefile_bytes(Path(spec_str))
    assert len(shp_b) > 100
    assert len(shx_b) > 100
    assert len(dbf_b) > 100


def test_sample_od_pairs_deterministic_and_respects_min_distance() -> None:
    """sample_od_pairs returns identical pairs for the same seed and enforces min_trip_km."""
    G = load_graph_pickle("data/processed/graph.pkl")
    pairs_1 = sample_od_pairs(G, n_pairs=25, min_trip_km=20.0, seed=42)
    pairs_2 = sample_od_pairs(G, n_pairs=25, min_trip_km=20.0, seed=42)
    pairs_diff = sample_od_pairs(G, n_pairs=25, min_trip_km=20.0, seed=99)

    assert len(pairs_1) == 25
    assert pairs_1 == pairs_2
    assert pairs_1 != pairs_diff

    for u, v in pairs_1:
        assert u != v
        dx = float(G.nodes[u]["x"]) - float(G.nodes[v]["x"])
        dy = float(G.nodes[u]["y"]) - float(G.nodes[v]["y"])
        dist_km = math.hypot(dx, dy) / 1000.0
        assert dist_km >= 20.0


def test_evaluate_and_summarize_method_invariants() -> None:
    """Resilient method reduces or preserves 2012 flood_km relative to baseline on sampled pairs."""
    G = load_graph_pickle("data/processed/graph.pkl")
    for _, _, edata in G.edges(data=True):
        edata.setdefault("rain_idx", 0.0)
        edata.setdefault("flood_2022_frac", float(edata.get("flood_frac", 0.0)))

    pairs = sample_od_pairs(G, n_pairs=20, min_trip_km=20.0, seed=42)
    base_recs = evaluate_method_on_pairs(G, pairs, alpha=0.0, beta=0.0)
    res_recs = evaluate_method_on_pairs(G, pairs, alpha=3.0, beta=0.0)

    summary = summarize_method(base_recs, res_recs, alpha=3.0, beta=0.0)
    all_2012 = summary["in_sample_2012"]["all_pairs"]
    assert all_2012["n_pairs"] == 20
    assert all_2012["flood_km_total"] <= all_2012["baseline_flood_km_total"] + 1e-9
    assert all_2012["total_extra_distance_pct"] >= -1e-9


def test_results_json_and_reproducibility() -> None:
    """Re-running run_full_evaluation reproduces identical numbers in results/results.json."""
    results_path = Path("results/results.json")
    demo_path = Path("config/demo_scenarios.json")

    if not results_path.exists():
        run_full_evaluation()

    saved_1 = json.loads(results_path.read_text(encoding="utf-8"))
    res_2 = run_full_evaluation(run_sweep=False, write_outputs=False)
    assert results_path.exists()
    assert demo_path.exists()
    assert len(saved_1["sensitivity_sweep"]) == 20

    # Check deterministic metrics match identically across runs
    for method_key in ("baseline", "alpha_only", "resilient"):
        for split_key in ("in_sample_2012", "held_out_2022"):
            for sub_key in ("all_pairs", "affected_pairs"):
                m1 = saved_1["methods"][method_key][split_key][sub_key]
                m2 = res_2["methods"][method_key][split_key][sub_key]
                assert m1["n_pairs"] == m2["n_pairs"]
                assert math.isclose(
                    m1["flood_km_total"],
                    m2["flood_km_total"],
                    rel_tol=0,
                    abs_tol=1e-12,
                )
                assert math.isclose(
                    m1["length_km_total"],
                    m2["length_km_total"],
                    rel_tol=0,
                    abs_tol=1e-12,
                )
                assert m1["total_flood_km_reduction_pct"] == m2["total_flood_km_reduction_pct"]
                assert math.isclose(
                    m1["total_extra_distance_pct"],
                    m2["total_extra_distance_pct"],
                    rel_tol=0,
                    abs_tol=1e-12,
                )

    assert "include_unknown_true" in saved_1["connector_road_comparison"]
    assert "include_unknown_false" in saved_1["connector_road_comparison"]
    assert len(saved_1["demo_scenarios"]) == 10
