# flood-router: Flood-Resilient Supply Chain Router

## 1. Overview
A deterministic weighted road network routing service that scores every road segment by historical flood exposure and recent rainfall anomaly, returning a flood-resilient route alongside a standard shortest-distance baseline route with the flood-zone kilometres of each.

This project is a **weighted network analysis** (not a machine learning model). Each road edge $e$ receives weight:

$$w(e) = L(e) \cdot (1 + \alpha F(e) + \beta R(e))$$

- $L(e) > 0$: segment length in kilometres (computed in local metric UTM projection).
- $F(e) \in [0, 1]$: fraction of segment length intersecting historical 2012 flood polygons.
- $R(e) \in [0, 1]$: normalized 7-day rainfall anomaly above the 10-year climatological median for the same calendar window, scaled by the (p95 - median) spread.
- $\alpha, \beta \ge 0$: configurable penalty multipliers (default $\alpha = 8.0$, $\beta = 0.5$ selected from the sensitivity sweep knee).

### Satellite Water Detection & Metric Interpretation
- **Coarse satellite water detection disclaimer**: Both the 2012 training flood polygons (MODIS 250m) and the 2022 held-out validation flood polygons (VIIRS 375m) represent **coarse satellite surface water detection, not road-level inundation** or structural passability.
- **Main metric**: The primary evaluation metric (`flood_km`) measures **avoidance of known historical flood zones** along routed paths, not real-time prediction of road flooding.

## 2. Data Sources and Provenance (`data/processed/manifest.json`)

| Dataset File | Rows | SHA-256 (prefix) | Source URL | Access Date | License |
|---|---:|---|---|---|---|
| `FL20221019NGA_SHP.zip` | 1,160 | `b701fdd819f2...` | https://data.humdata.org/dataset/7d978dcf-afa9-454e-8eaa-d9e54458f44e | 2026-11-08 | CC BY-IGO |
| `national-roads-esri-shapefile.zip` | 701,169 | `d109feddc53b...` | https://data.humdata.org/dataset/fa359dc7-c3e4-4ed9-9d51-0bf609c20e06 | 2026-11-08 | CC BY 4.0 |
| `nga-floods-2012-shp.zip` | 1 | `75d0907b5907...` | https://data.humdata.org/dataset/bd3b4ef6-0a96-4346-b229-2c410d9c6c6d | 2026-11-08 | CC BY-IGO |
| `nga_admin_boundaries.shp.zip` | 13,965 | `4600b159107b...` | https://data.humdata.org/dataset/cod-ab-nga | 2026-11-08 | CC BY-IGO |
| `power` | 30 | `6b6e128b90d9...` | https://power.larc.nasa.gov/docs/services/api/ | 2026-11-08 | NASA Open Data Policy |

- **Rainfall API**: Parameter `PRECTOTCORR` (`mm/day`), docs at https://power.larc.nasa.gov/docs/services/api/, cached per grid point in `data/raw/power/`.

## 3. Pilot Region Network Summary (`Kogi`)

- **Pilot Region (`region.name`)**: `Kogi` (configured in `config/default.yaml`)
- **Largest Connected Component**: `5,999` nodes, `7,724` edges, `6,769.41 km` total road length
- **2012 Training Flood Exposure ($F_{2012}(e)$)**: `115` / `7,724` edges (`1.49%`), `100.53 km` flood-zone road length (`1.49%`)
- **2022 Held-Out Flood Exposure ($F_{2022}(e)$)**: `345` / `7,724` edges (`4.47%`), `341.51 km` flood-zone road length (`5.04%`)
- **Default Rainfall Reference Date (`as_of`)**: `2022-10-08`

## 4. Seeded Evaluation Results (`results/results.json`)

Evaluated on `n_pairs = 200` origin-destination node pairs sampled uniformly at random from the largest connected component with straight-line distance $\ge 20.0\text{ km}$ (`seed = 42`). Every method is evaluated on the exact same node pairs.

### 4.1 Headline Comparison: All Pairs vs Affected Pairs (`baseline flood_km > 0`)

| Method | alpha | beta | Validation Split | Pair Subset | n_pairs | Mean flood_km | Median flood_km | P90 flood_km | Total flood_km | Total flood_km Reduction | Mean Pair flood_km Reduction | Total Extra Distance | Latency Median / P95 (ms) | Unreachable |
|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| Baseline (Shortest) | 0.0 | 0.0 | 2012 In-Sample | All Pairs | 200 | 0.7635 | 0.3042 | 1.2681 | 152.70 | 0.00% | 0.00% | 0.00% | 8.96 / 24.23 | 0 |
| Baseline (Shortest) | 0.0 | 0.0 | 2012 In-Sample | Affected Pairs | 113 | 1.3513 | 1.2515 | 1.7350 | 152.70 | 0.00% | 0.00% | 0.00% | 8.96 / 24.23 | 0 |
| Baseline (Shortest) | 0.0 | 0.0 | 2022 Held-Out | All Pairs | 200 | 2.7976 | 2.6032 | 4.7388 | 559.52 | 0.00% | 0.00% | 0.00% | 8.96 / 24.23 | 0 |
| Baseline (Shortest) | 0.0 | 0.0 | 2022 Held-Out | Affected Pairs | 120 | 4.6627 | 2.6032 | 7.3500 | 559.52 | 0.00% | 0.00% | 0.00% | 8.96 / 24.23 | 0 |
| Alpha-Only (No Rain) | 8.0 | 0.0 | 2012 In-Sample | All Pairs | 200 | 0.7165 | 0.3042 | 1.0823 | 143.29 | 6.16% | 7.00% | 0.13% | 9.22 / 23.06 | 0 |
| Alpha-Only (No Rain) | 8.0 | 0.0 | 2012 In-Sample | Affected Pairs | 113 | 1.2681 | 1.0823 | 1.7012 | 143.29 | 6.16% | 7.00% | 0.18% | 9.22 / 23.06 | 0 |
| Alpha-Only (No Rain) | 8.0 | 0.0 | 2022 Held-Out | All Pairs | 200 | 2.8864 | 2.6112 | 4.7388 | 577.28 | -3.17% | -5.97% | 0.13% | 9.22 / 23.06 | 0 |
| Alpha-Only (No Rain) | 8.0 | 0.0 | 2022 Held-Out | Affected Pairs | 120 | 4.8107 | 2.9761 | 7.3500 | 577.28 | -3.17% | -5.97% | 0.18% | 9.22 / 23.06 | 0 |
| Resilient (Default) | 8.0 | 0.5 | 2012 In-Sample | All Pairs | 200 | 0.7165 | 0.3042 | 1.0823 | 143.29 | 6.16% | 7.00% | 0.16% | 8.96 / 22.57 | 0 |
| Resilient (Default) | 8.0 | 0.5 | 2012 In-Sample | Affected Pairs | 113 | 1.2681 | 1.0823 | 1.7012 | 143.29 | 6.16% | 7.00% | 0.18% | 8.96 / 22.57 | 0 |
| Resilient (Default) | 8.0 | 0.5 | 2022 Held-Out | All Pairs | 200 | 2.8864 | 2.6112 | 4.7388 | 577.28 | -3.17% | -5.97% | 0.16% | 8.96 / 22.57 | 0 |
| Resilient (Default) | 8.0 | 0.5 | 2022 Held-Out | Affected Pairs | 120 | 4.8107 | 2.9761 | 7.3500 | 577.28 | -3.17% | -5.97% | 0.18% | 8.96 / 22.57 | 0 |

### 4.2 Sensitivity Sweep (`alpha_grid x beta_grid`) and Knee Selection

> Default alpha=8.0, beta=0.5 is selected from the knee of the alpha_grid x beta_grid sweep ([0, 1, 2, 4, 8] x [0, 0.5, 1, 2]). Because flooded segments account for 1.49% of network length (~1.35 km per affected trip) while regional detours add several kilometres of dry road weighted by (1 + beta * R(e)), alpha <= 2.0 produces 0.00% flood reduction and alpha=4.0 only detours when beta=0.0. At alpha=8.0, beta=0.5, flood_km reduction on 2012 affected pairs jumps to 6.16% (7.00% mean per-pair reduction) for only 0.18% extra distance, whereas increasing beta to 2.0 suppresses detours by penalizing dry detour length.

| alpha | beta | 2012 Affected Total flood_km Reduction | 2012 Affected Mean Pair Reduction | 2012 Affected Extra Distance | 2022 Held-Out Affected Total flood_km Reduction | 2022 Held-Out Affected Extra Distance |
|---:|---:|---:|---:|---:|---:|---:|
| 0.0 | 0.0 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.0 | 0.5 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.0 | 1.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 0.0 | 2.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 1.0 | 0.0 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 1.0 | 0.5 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 1.0 | 1.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 1.0 | 2.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 2.0 | 0.0 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 2.0 | 0.5 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 2.0 | 1.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 2.0 | 2.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 4.0 | 0.0 | 6.09% | 6.26% | 0.18% | -3.00% | 0.17% |
| 4.0 | 0.5 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 4.0 | 1.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 4.0 | 2.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |
| 8.0 | 0.0 | 6.16% | 7.00% | 0.18% | -3.17% | 0.18% |
| 8.0 | 0.5 | 6.16% | 7.00% | 0.18% | -3.17% | 0.18% |
| 8.0 | 1.0 | 6.09% | 6.26% | 0.18% | -2.15% | 0.18% |
| 8.0 | 2.0 | 0.00% | 0.00% | 0.00% | 0.85% | 0.01% |

### 4.3 Connector Road Class Flag (`network.include_unknown_roads`) Comparison

| Setting (`include_unknown_roads`) | Raw Road Rows | Graph Nodes | Graph Edges | Total Road km | 2012 Flood km | 2022 Flood km | 2012 Affected flood_km Reduction | 2022 Held-Out Affected flood_km Reduction |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `True` | 3,349 | 5,999 | 7,724 | 6,769.41 | 100.53 | 341.51 | 6.16% | -3.17% |
| `False` | 3,349 | 5,999 | 7,724 | 6,769.41 | 100.53 | 341.51 | 6.16% | -3.17% |

## 5. Known Weaknesses and Limitations

1. **In-sample evaluation overlap**: In-sample evaluation measures route `flood_km` against the same 2012 flood polygons used to construct $F(e)$, so non-negative flood reduction is guaranteed by design when $\beta = 0$. We mitigate this by implementing a strict temporal held-out split where $F(e)$ is built exclusively from 2012 MODIS flood extents and tested exclusively against 2022 VIIRS maximum flood water extents.
2. **Coarse rainfall resolution**: NASA POWER precipitation (`PRECTOTCORR`) is sampled on a `0.5 deg` latitude/longitude lattice (~55 km grid spacing), which is coarse relative to individual road segments. Furthermore, when regional rainfall anomaly $R(e)$ is uniformly high, adding $\beta R(e)$ to dry detour edges increases detour cost unless $\alpha$ is sufficiently large. We report the $\beta = 0$ (`alpha_only`) ablation alongside the full model to isolate this effect.
3. **Historical flood polygons miss new flood paths**: As shown in the 2022 held-out temporal validation, the October 2022 flood extent in the pilot region intersected `341.51 km` of road (`5.04%`), over three times the 2012 extent (`100.53 km`, `1.49%`). Detours that avoided 2012 flood zones encountered segments that flooded in 2022, resulting in a `-3.17%` change in 2022 `flood_km` at the default weights. In addition, primary river crossings with a single bridge act as graph cut-edges where no topological detour exists.

## 6. Setup, Service, Frontend, and One-Command Reproduction

### Reproduce All Artifacts and Results (`make reproduce`)
```bash
python scripts/reproduce.py
# or via Make:
make reproduce
```

### Run Quality & Hard-Coding Checks
```bash
python -m ruff check .
python -m pytest
python scripts/check_no_hardcoding.py
```

### Run FastAPI Routing Service (`service/`)
```bash
python -m uvicorn service.main:app --host 127.0.0.1 --port 8000
```

### Run Next.js Comparison Map Frontend (`web/`)
```bash
cd web
bun run build
bun run dev
```
