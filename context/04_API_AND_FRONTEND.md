# 04 API AND FRONTEND SPEC

Build rule: no hard-coded edge cases (see 00_PROJECT.md section 3).

## FastAPI service (`service/`)
Load `graph.pkl` and config once at startup. Rain index per `as_of` computed on first request and cached.

### Endpoints
`GET /health` returns `{status, graph_nodes, graph_edges}`.

`GET /meta` returns `{bbox, alpha, beta, as_of_default, data_manifest}`. The frontend reads its map bounds from here. Nothing about the region is hard-coded in the frontend.

`GET /flood-layer` returns simplified flood polygons as GeoJSON for map display (simplification tolerance from config).

`POST /route`
Request:
```json
{"origin": {"lat": 0.0, "lon": 0.0}, "destination": {"lat": 0.0, "lon": 0.0},
 "as_of": "YYYY-MM-DD (optional)", "alpha": "optional", "beta": "optional"}
```
Response:
```json
{"baseline": {"geometry": "GeoJSON LineString", "length_km": 0, "flood_km": 0},
 "resilient": {"geometry": "GeoJSON LineString", "length_km": 0, "flood_km": 0},
 "delta": {"flood_km_reduction_pct": 0, "extra_distance_pct": 0},
 "params": {"alpha": 0, "beta": 0, "as_of": "YYYY-MM-DD"},
 "snap": {"origin_m": 0, "destination_m": 0}}
```
Errors use `{ "error": {"code": "...", "message": "..."} }` with codes `INVALID_INPUT` (422), `OUT_OF_COVERAGE` (400), `NO_PATH` (404). Validate with Pydantic: lat in -90 to 90, lon in -180 to 180, alpha and beta within ranges from config.

### Notes
- When baseline `flood_km` is 0, `flood_km_reduction_pct` is `null`, not 0 and not an error. This is a general rule for zero denominators.
- Latency target: median under 500 ms per request on the pilot graph. Measure it, do not assume it.

## Node gateway (optional, cut first)
Express and TypeScript. Forwards `/api/*` to FastAPI, validates JSON, enables CORS, reads the FastAPI URL from env. No business logic.

## Next.js and Mapbox frontend (`web/`)
Env: `NEXT_PUBLIC_MAPBOX_TOKEN`, `NEXT_PUBLIC_API_URL`.
Requirements:
- On load, fetch `/meta` and fit the map to `bbox`. Draw the flood layer from `/flood-layer`.
- Click twice to set origin and destination, or pick a scenario from `config/demo_scenarios.json` served as data.
- Draw baseline in red and resilient in teal. Show a legend.
- Toggle "Flood-Resilient mode" switches which route is emphasized. Both stay visible at lower opacity.
- Metrics panel shows `length_km` and `flood_km` for both routes plus the `delta` values, with the `as_of` date and the alpha and beta used.
- Loading and error states render the API error message. No silent failures.
- A rainfall date picker that sends `as_of`.
- Accessible: sufficient contrast, route status also given as text, not color alone.
