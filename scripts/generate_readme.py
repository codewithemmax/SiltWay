"""Generate README.md strictly from results/results.json, manifest.json, and config/default.yaml."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from floodrouter.config import load_config
from floodrouter.manifest import read_manifest


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "null"
    return f"{val:.2f}%"


def build_readme_markdown(
    results: dict[str, Any],
    manifest_rows: list[dict[str, Any]],
    cfg_dict: dict[str, Any],
) -> str:
    """Construct the complete README.md markdown from structured evaluation outputs."""
    cfg = results["config"]
    net = results["network_summary"]
    f12 = net["flood_2012"]
    f22 = net["flood_2022_held_out"]
    methods = results["methods"]

    lines: list[str] = [
        f"# {results['project_name']}: Flood-Resilient Supply Chain Router",
        "",
        "## 1. Overview",
        (
            "A deterministic weighted road network routing service that scores every road "
            "segment by historical flood exposure and recent rainfall anomaly, returning a "
            "flood-resilient route alongside a standard shortest-distance baseline route "
            "with the flood-zone kilometres of each."
        ),
        "",
        "This project is a **weighted network analysis** (not a machine learning model). "
        "Each road edge $e$ receives weight:",
        "",
        "$$w(e) = L(e) \\cdot (1 + \\alpha F(e) + \\beta R(e))$$",
        "",
        "- $L(e) > 0$: segment length in kilometres (computed in local metric UTM projection).",
        (
            "- $F(e) \\in [0, 1]$: fraction of segment length intersecting "
            "historical 2012 flood polygons."
        ),
        (
            f"- $R(e) \\in [0, 1]$: normalized {cfg_dict['rainfall']['window_days']}-day "
            f"rainfall anomaly above the {cfg_dict['rainfall']['climatology_years']}-year "
            "climatological median for the same calendar window, scaled by the "
            f"(p{int(cfg_dict['rainfall']['scale_percentile'])} - median) spread."
        ),
        (
            f"- $\\alpha, \\beta \\ge 0$: configurable penalty multipliers "
            f"(default $\\alpha = {cfg['alpha']}$, $\\beta = {cfg['beta']}$ selected from "
            "the sensitivity sweep knee)."
        ),
        "",
        "### Satellite Water Detection & Metric Interpretation",
        (
            "- **Coarse satellite water detection disclaimer**: Both the 2012 training flood "
            "polygons (MODIS 250m) and the 2022 held-out validation flood polygons (VIIRS 375m) "
            "represent **coarse satellite surface water detection, not road-level inundation** "
            "or structural passability."
        ),
        (
            "- **Main metric**: The primary evaluation metric (`flood_km`) measures **avoidance "
            "of known historical flood zones** along routed paths, not real-time prediction of "
            "road flooding."
        ),
        "",
        "## 2. Data Sources and Provenance (`data/processed/manifest.json`)",
        "",
        "| Dataset File | Rows | SHA-256 (prefix) | Source URL | Access Date | License |",
        "|---|---:|---|---|---|---|",
    ]

    for entry in manifest_rows:
        sha_short = str(entry["checksum"])[:12]
        lines.append(
            f"| `{entry['file_name']}` | {int(entry['row_count']):,} | `{sha_short}...` | "
            f"{entry['source_url']} | {entry['access_date']} | {entry['license']} |"
        )

    lines.extend(
        [
            "",
            (
                f"- **Rainfall API**: Parameter `{cfg_dict['datasets']['rainfall']['parameter']}` "
                f"(`{cfg_dict['datasets']['rainfall']['units']}`), docs at "
                f"{cfg_dict['datasets']['rainfall']['docs_url']}, cached per grid point in "
                f"`{cfg_dict['datasets']['rainfall']['raw_cache_dir']}/`."
            ),
            "",
            f"## 3. Pilot Region Network Summary (`{cfg['region']}`)",
            "",
            (
                f"- **Pilot Region (`region.name`)**: `{cfg['region']}` "
                "(configured in `config/default.yaml`)"
            ),
            (
                f"- **Largest Connected Component**: `{net['nodes']:,}` nodes, "
                f"`{net['edges']:,}` edges, `{f12['total_road_km']:,.2f} km` total road length"
            ),
            (
                f"- **2012 Training Flood Exposure ($F_{{2012}}(e)$)**: "
                f"`{f12['n_flooded_edges']:,}` / `{f12['n_edges']:,}` edges "
                f"(`{f12['flood_edge_pct']:.2f}%`), `{f12['total_flood_km']:.2f} km` "
                f"flood-zone road length (`{f12['flood_km_pct']:.2f}%`)"
            ),
            (
                f"- **2022 Held-Out Flood Exposure ($F_{{2022}}(e)$)**: "
                f"`{f22['n_flooded_edges']:,}` / `{f22['n_edges']:,}` edges "
                f"(`{f22['flood_edge_pct']:.2f}%`), `{f22['total_flood_km']:.2f} km` "
                f"flood-zone road length (`{f22['flood_km_pct']:.2f}%`)"
            ),
            f"- **Default Rainfall Reference Date (`as_of`)**: `{cfg['as_of']}`",
            "",
            "## 4. Seeded Evaluation Results (`results/results.json`)",
            "",
            (
                f"Evaluated on `n_pairs = {cfg['n_pairs']}` origin-destination node pairs "
                f"sampled uniformly at random from the largest connected component with "
                f"straight-line distance $\\ge {cfg['min_trip_km']:.1f}\\text{{ km}}$ "
                f"(`seed = {cfg['seed']}`). "
                "Every method is evaluated on the exact same node pairs."
            ),
            "",
            "### 4.1 Headline Comparison: All Pairs vs Affected Pairs (`baseline flood_km > 0`)",
            "",
            (
                "| Method | alpha | beta | Validation Split | Pair Subset | n_pairs | "
                "Mean flood_km | Median flood_km | P90 flood_km | Total flood_km | "
                "Total flood_km Reduction | Mean Pair flood_km Reduction | "
                "Total Extra Distance | Latency Median / P95 (ms) | Unreachable |"
            ),
            "|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
        ]
    )

    for m_key, m_label in [
        ("baseline", "Baseline (Shortest)"),
        ("alpha_only", "Alpha-Only (No Rain)"),
        ("resilient", "Resilient (Default)"),
    ]:
        m_obj = methods[m_key]
        lat_str = f"{m_obj['latency_ms']['median']:.2f} / {m_obj['latency_ms']['p95']:.2f}"
        for split_key, split_label in [
            ("in_sample_2012", "2012 In-Sample"),
            ("held_out_2022", "2022 Held-Out"),
        ]:
            for sub_key, sub_label in [
                ("all_pairs", "All Pairs"),
                ("affected_pairs", "Affected Pairs"),
            ]:
                sub = m_obj[split_key][sub_key]
                lines.append(
                    f"| {m_label} | {m_obj['alpha']:.1f} | {m_obj['beta']:.1f} | "
                    f"{split_label} | {sub_label} | {sub['n_pairs']} | "
                    f"{sub['flood_km_mean']:.4f} | {sub['flood_km_median']:.4f} | "
                    f"{sub['flood_km_p90']:.4f} | {sub['flood_km_total']:.2f} | "
                    f"{_fmt_pct(sub['total_flood_km_reduction_pct'])} | "
                    f"{_fmt_pct(sub['mean_pair_flood_km_reduction_pct'])} | "
                    f"{sub['total_extra_distance_pct']:.2f}% | {lat_str} | "
                    f"{m_obj['unreachable_pairs']} |"
                )

    lines.extend(
        [
            "",
            "### 4.2 Sensitivity Sweep (`alpha_grid x beta_grid`) and Knee Selection",
            "",
            f"> {results['knee_selection_note']}",
            "",
            (
                "| alpha | beta | 2012 Affected Total flood_km Reduction | "
                "2012 Affected Mean Pair Reduction | 2012 Affected Extra Distance | "
                "2022 Held-Out Affected Total flood_km Reduction | "
                "2022 Held-Out Affected Extra Distance |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for item in results["sensitivity_sweep"]:
        s12 = item["in_sample_2012"]["affected_pairs"]
        s22 = item["held_out_2022"]["affected_pairs"]
        lines.append(
            f"| {item['alpha']:.1f} | {item['beta']:.1f} | "
            f"{_fmt_pct(s12['total_flood_km_reduction_pct'])} | "
            f"{_fmt_pct(s12['mean_pair_flood_km_reduction_pct'])} | "
            f"{s12['total_extra_distance_pct']:.2f}% | "
            f"{_fmt_pct(s22['total_flood_km_reduction_pct'])} | "
            f"{s22['total_extra_distance_pct']:.2f}% |"
        )

    lines.extend(
        [
            "",
            "### 4.3 Connector Road Class Flag (`network.include_unknown_roads`) Comparison",
            "",
            (
                "| Setting (`include_unknown_roads`) | Raw Road Rows | Graph Nodes | "
                "Graph Edges | Total Road km | 2012 Flood km | 2022 Flood km | "
                "2012 Affected flood_km Reduction | 2022 Held-Out Affected flood_km Reduction |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for key in ("include_unknown_true", "include_unknown_false"):
        c_obj = results["connector_road_comparison"][key]
        r12 = c_obj["resilient_vs_baseline"]["in_sample_2012"]["affected_pairs"]
        r22 = c_obj["resilient_vs_baseline"]["held_out_2022"]["affected_pairs"]
        lines.append(
            f"| `{c_obj['include_unknown_roads']}` | "
            f"{c_obj.get('raw_road_rows_in_region', net['edges']):,} | "
            f"{c_obj['graph_nodes']:,} | {c_obj['graph_edges']:,} | "
            f"{c_obj['total_length_km']:,.2f} | "
            f"{c_obj['flood_2012_summary']['total_flood_km']:.2f} | "
            f"{c_obj['flood_2022_summary']['total_flood_km']:.2f} | "
            f"{_fmt_pct(r12['total_flood_km_reduction_pct'])} | "
            f"{_fmt_pct(r22['total_flood_km_reduction_pct'])} |"
        )

    res_22_red = _fmt_pct(
        methods["resilient"]["held_out_2022"]["affected_pairs"]["total_flood_km_reduction_pct"]
    )
    lines.extend(
        [
            "",
            "## 5. Known Weaknesses and Limitations",
            "",
            (
                "1. **In-sample evaluation overlap**: In-sample evaluation measures route "
                "`flood_km` against the same 2012 flood polygons used to construct $F(e)$, "
                "so non-negative flood reduction is guaranteed by design when $\\beta = 0$. "
                "We mitigate this by implementing a strict temporal held-out split where "
                "$F(e)$ is built exclusively from 2012 MODIS flood extents and tested "
                "exclusively against 2022 VIIRS maximum flood water extents."
            ),
            (
                "2. **Coarse rainfall resolution**: NASA POWER precipitation (`PRECTOTCORR`) "
                f"is sampled on a `{cfg_dict['rainfall']['grid_step_deg']} deg` latitude/longitude "
                "lattice (~55 km grid spacing), which is coarse relative to individual road "
                "segments. Furthermore, when regional rainfall anomaly $R(e)$ is uniformly high, "
                "adding $\\beta R(e)$ to dry detour edges increases detour cost unless $\\alpha$ "
                "is sufficiently large. We report the $\\beta = 0$ (`alpha_only`) ablation "
                "alongside the full model to isolate this effect."
            ),
            (
                "3. **Historical flood polygons miss new flood paths**: As shown in the "
                "2022 held-out temporal validation, the October 2022 flood extent in the pilot "
                f"region intersected `{f22['total_flood_km']:.2f} km` of road "
                f"(`{f22['flood_km_pct']:.2f}%`), over three times the 2012 extent "
                f"(`{f12['total_flood_km']:.2f} km`, `{f12['flood_km_pct']:.2f}%`). "
                "Detours that avoided 2012 flood zones encountered segments that flooded in 2022, "
                f"resulting in a `{res_22_red}` "
                "change in 2022 `flood_km` at the default weights. In addition, primary river "
                "crossings with a single bridge act as graph cut-edges where no topological "
                "detour exists."
            ),
            "",
            "## 6. Setup, Service, Frontend, and One-Command Reproduction",
            "",
            "### Reproduce All Artifacts and Results (`make reproduce`)",
            "```bash",
            "python scripts/reproduce.py",
            "# or via Make:",
            "make reproduce",
            "```",
            "",
            "### Run Quality & Hard-Coding Checks",
            "```bash",
            "python -m ruff check .",
            "python -m pytest",
            "python scripts/check_no_hardcoding.py",
            "```",
            "",
            "### Run FastAPI Routing Service (`service/`)",
            "```bash",
            "python -m uvicorn service.main:app --host 127.0.0.1 --port 8000",
            "```",
            "",
            "### Run Next.js Comparison Map Frontend (`web/`)",
            "```bash",
            "cd web",
            "bun run build",
            "bun run dev",
            "```",
        ]
    )

    return "\n".join(lines) + "\n"


def generate_readme(
    repo_root: str | Path | None = None,
    config_path: str | Path = "config/default.yaml",
) -> Path:
    """Read results/results.json and manifest.json and write README.md."""
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    cfg_file = Path(config_path)
    if not cfg_file.is_absolute():
        cfg_file = root / cfg_file
    cfg = load_config(cfg_file)

    results_path = root / cfg.paths.results_json
    results = json.loads(results_path.read_text(encoding="utf-8"))

    manifest_path = root / cfg.paths.manifest_path
    manifest_entries = [m.model_dump() for m in read_manifest(manifest_path)]

    md_content = build_readme_markdown(
        results=results,
        manifest_rows=manifest_entries,
        cfg_dict=cfg.model_dump(),
    )
    readme_path = root / "README.md"
    readme_path.write_text(md_content, encoding="utf-8")
    return readme_path


if __name__ == "__main__":
    out_path = generate_readme()
    print(f"Generated {out_path}")
