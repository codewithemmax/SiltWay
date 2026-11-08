# 02 DATA

Build rule: no hard-coded edge cases (see 00_PROJECT.md section 3).

Verify every source below at download time. Record URL, access date, license and version in `data/processed/manifest.json`. Do not assume field names. Inspect columns first and map them in config.

## Sources
| Dataset | Use | Expected form | Check at download |
|---|---|---|---|
| GRID3 Nigeria roads | road network | line geometries, shapefile or GeoPackage | license, road class field, CRS, geometry validity |
| HDX Nigeria flood data | historical flood extent | polygons (possibly per event or year) | has date or event field? license, CRS |
| NASA POWER daily precipitation | rainfall anomaly | point API per lat/lon, daily JSON | confirm precipitation parameter name and units in POWER docs |
| Region boundary | clip area | polygon from any official or HDX boundary file | CRS, name field |

If the flood dataset has no event or date field, held-out temporal validation is impossible. Say so in the README and use the spatial-split fallback in 03.

## Processing steps (each is a function with a test)
1. `ingest.load_roads(path, region)`: read, reproject to storage CRS, clip to region boundary, drop null or empty geometries, explode multi-part lines.
2. `network.build_graph(roads, snap_tolerance_m)`: reproject to projected CRS, node the lines at intersections (shapely 2 `node` or equivalent), snap endpoints within tolerance, create nodes at endpoints, edges carry `edge_id, length_m, geometry`. Keep the largest connected component if configured. Log the number of components dropped.
3. `flood.edge_flood_fraction(edges, flood_polys)`: reproject polygons, dissolve, intersect edge lines with polygons, `F(e) = intersect_length / edge_length`, clipped to 0 to 1. Use a spatial index. Edges with no intersection get 0.
4. `rainfall.fetch_grid(bbox, step, date_range)`: sample points on a regular lattice over the region bbox, request POWER daily precipitation, cache raw JSON per point in `data/raw/power/`. Respect rate limits, retry with backoff.
5. `rainfall.rain_index(grid, as_of, window_days, climatology_years)`: for each cell, `recent = sum over window ending at as_of`, `clim = distribution of the same-window sums in prior years`, `anomaly = (recent - clim_median) / max(clim_p95 - clim_median, eps)`, clipped to 0 to 1. Assign each edge the value of the nearest grid point to its midpoint.

## Output tables
`data/processed/edges.parquet` columns: `edge_id, u, v, length_m, flood_frac, geometry`.
`data/processed/rain_<as_of>.parquet` columns: `edge_id, rain_idx`.
`data/processed/graph.pkl` NetworkX graph with edge attributes above.

## Validation checks (generic, thresholds from config)
- Share of edges with valid non-empty geometry
- Number and size of connected components before and after filtering
- `0 <= flood_frac <= 1` and `0 <= rain_idx <= 1` for all edges
- Share of edges with `flood_frac > 0` printed, plus total flood-zone km. If it is 0 percent or near 100 percent, stop and inspect the CRS handling before continuing.
