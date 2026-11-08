# 01 ARCHITECTURE

Build rule: no hard-coded edge cases (see 00_PROJECT.md section 3).

## Components
1. `floodrouter/` Python package: ingest, network build, flood exposure, rainfall index, weighting, routing, evaluation.
2. `service/` FastAPI app that loads the prebuilt graph once at startup and serves routes.
3. `gateway/` Node.js, Express, TypeScript proxy with request validation and CORS. Optional. It is the first thing cut if time runs short.
4. `web/` Next.js app with Mapbox GL showing both routes and a metrics panel.
5. `notebooks/evaluation.ipynb` produces every number used in the deck.

## Data flow
raw GRID3 roads -> clean and node lines -> graph (NetworkX) -> edge table
raw HDX flood polygons -> per-edge flood fraction F(e)
NASA POWER precipitation -> per-edge rainfall index R(e) for a given as_of date
F(e), R(e), length L(e) -> weight w(e) -> shortest path (baseline by L, resilient by w) -> response

Precompute everything that does not depend on as_of (graph, F(e)) offline and cache to disk. Only R(e) and w(e) depend on as_of and are recomputed per as_of date, then cached by date key.

## Repo layout
```
flood-router/
  config/default.yaml
  config/demo_scenarios.json
  config/forbidden_literals.txt
  data/raw/            (gitignored)
  data/processed/      (gitignored)
  floodrouter/
    config.py  ingest.py  network.py  flood.py  rainfall.py
    weights.py  routing.py  evaluate.py
  service/main.py  service/schemas.py
  gateway/src/index.ts
  web/
  scripts/check_no_hardcoding.py
  tests/
  notebooks/evaluation.ipynb
  README.md
```

## Config (single source of truth)
`config/default.yaml` keys (values are examples, tune from data):
```
region: {name: <set by user>, boundary_file: data/raw/<file>}
crs: {storage: "EPSG:4326", projected: "auto_utm"}
network: {snap_tolerance_m: 5, keep_largest_component: true}
weights: {alpha: 3.0, beta: 1.0}
rainfall: {window_days: 7, climatology_years: 10, grid_step_deg: 0.5, scale_percentile: 95}
evaluation: {n_pairs: 200, min_trip_km: 20, seed: 42, alpha_grid: [0,1,2,4,8], beta_grid: [0,0.5,1,2]}
service: {max_snap_distance_m: 2000}
```
`projected: auto_utm` means call `GeoDataFrame.estimate_utm_crs()` on the data. Never write a UTM zone number in code.

## Conventions
- Python 3.11, type hints, ruff, pytest. TypeScript strict mode.
- All geometry math (length, intersection) happens in the projected metric CRS. Only API input and output use EPSG:4326.
- Every function takes config or parameters explicitly. No module-level constants for tunables.
- Log data versions (file name, row count, checksum) to `data/processed/manifest.json` for reproducibility.
