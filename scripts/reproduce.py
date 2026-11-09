"""One-command pipeline reproduction script rebuilding network, evaluation, and README."""

from __future__ import annotations

import argparse
from pathlib import Path

from floodrouter.evaluate import run_full_evaluation
from floodrouter.flood import build_flood_from_config
from floodrouter.network import build_network_from_config
from floodrouter.rainfall import build_rainfall_from_config
from scripts.generate_readme import generate_readme


def reproduce_pipeline(
    config_path: str | Path = "config/default.yaml",
    rebuild_graph: bool = True,
) -> None:
    """Execute the full end-to-end flood-router pipeline from config."""
    cfg_str = str(config_path)
    if rebuild_graph:
        print("Step 1/5: Rebuilding road network graph from config...")
        net_stats = build_network_from_config(cfg_str)
        print(
            f"  Graph built: {net_stats['largest_component_nodes']} nodes, "
            f"{net_stats['largest_component_edges']} edges."
        )

        print("Step 2/5: Computing 2012 flood exposure F(e) and sanity map...")
        flood_stats = build_flood_from_config(cfg_str)
        print(
            f"  2012 flood exposure: {flood_stats['n_flooded_edges']}/{flood_stats['n_edges']} "
            f"edges ({flood_stats['flood_edge_pct']:.2f}%), {flood_stats['total_flood_km']:.2f} km."
        )

        print("Step 3/5: Computing/verifying NASA POWER rainfall anomaly R(e)...")
        rain_stats = build_rainfall_from_config(cfg_str)
        print(
            f"  Rain index ready: min={rain_stats['min_rain_idx']:.4f}, "
            f"median={rain_stats['median_rain_idx']:.4f}, max={rain_stats['max_rain_idx']:.4f}."
        )

    print("Step 4/5: Running seeded evaluation, sensitivity sweep, and 2022 held-out check...")
    results = run_full_evaluation(cfg_str)
    res_12 = results["methods"]["resilient"]["in_sample_2012"]["affected_pairs"]
    res_22 = results["methods"]["resilient"]["held_out_2022"]["affected_pairs"]
    print(
        f"  Evaluation saved to results/results.json: "
        f"2012 affected reduction = {res_12['total_flood_km_reduction_pct']:.2f}%, "
        f"2022 held-out affected reduction = {res_22['total_flood_km_reduction_pct']:.2f}%."
    )

    print("Step 5/5: Generating README.md strictly from results/results.json and manifest.json...")
    readme_path = generate_readme(config_path=cfg_str)
    print(f"  Generated {readme_path}.")


def main() -> int:
    """CLI entrypoint for pipeline reproduction."""
    parser = argparse.ArgumentParser(description="Reproduce flood-router artifacts and results.")
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to configuration YAML file.",
    )
    parser.add_argument(
        "--eval-only",
        action="store_true",
        help="Skip graph/flood/rainfall rebuild and re-run evaluation + README generation only.",
    )
    args = parser.parse_args()
    reproduce_pipeline(config_path=args.config, rebuild_graph=not args.eval_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
