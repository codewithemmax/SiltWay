"""FastAPI application entrypoint for flood-resilient routing."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd
import shapely
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from shapely.geometry import mapping

from floodrouter.config import AppConfig, load_config
from floodrouter.flood import load_flood_polygons
from floodrouter.ingest import load_region_boundary
from floodrouter.manifest import read_manifest
from floodrouter.network import load_edges_parquet, load_graph_pickle
from floodrouter.rainfall import (
    attach_rain_index_to_graph,
    edges_bbox,
    get_or_compute_rain_index,
    select_as_of_options,
)
from floodrouter.roads_layer import build_roads_layer, classes_for_zoom, load_or_build_roads_layer
from floodrouter.routing import RoutingError, compare_routes, compute_route_recommendation
from service.batch import BatchRowError, batch_to_csv, parse_batch_row, summarize_batch
from service.schemas import (
    BatchRequest,
    BatchResponse,
    BatchRowResult,
    HealthResponse,
    MetaResponse,
    PresetResponse,
    RouteCompareResponse,
    RouteParamsResponse,
    RouteRequest,
)

STATUS_BY_ERROR_CODE: dict[str, int] = {
    "INVALID_INPUT": 422,
    "UNKNOWN_PRESET": 422,
    "BATCH_TOO_LARGE": 422,
    "OUT_OF_COVERAGE": 400,
    "NO_PATH": 404,
    "RAINFALL_UNAVAILABLE": 503,
}


def _build_flood_feature_collection(
    graph: nx.Graph,
    config: AppConfig,
    repo_root: Path,
) -> dict[str, Any]:
    """Extract and simplify the 2012 flood polygons into a GeoJSON FeatureCollection."""
    geom = graph.graph.get("flood_2012_geom")
    if geom is None or geom.is_empty:
        boundary = load_region_boundary(
            boundary_path=repo_root / config.region.boundary_file,
            region_name=config.region.name,
            name_column=config.region.name_column,
        )
        polys = load_flood_polygons(
            flood_path=repo_root / config.datasets.flood_2012.path,
            region_boundary=boundary,
        )
        geom = shapely.unary_union(polys) if polys else shapely.GeometryCollection()

    tolerance = float(config.service.flood_simplify_tolerance_deg)
    simplified = (
        shapely.simplify(geom, tolerance=tolerance, preserve_topology=True)
        if not geom.is_empty and tolerance > 0.0
        else geom
    )

    features: list[dict[str, Any]] = []
    if simplified is not None and not simplified.is_empty:
        for idx, part in enumerate(shapely.get_parts(simplified)):
            if part is None or part.is_empty:
                continue
            features.append(
                {
                    "type": "Feature",
                    "id": idx,
                    "properties": {
                        "layer": "flood_2012",
                        "region": config.region.name,
                    },
                    "geometry": mapping(part),
                }
            )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def _compute_graph_bbox(graph: nx.Graph) -> list[float]:
    """Compute [min_lon, min_lat, max_lon, max_lat] from loaded graph nodes."""
    stored_bbox = graph.graph.get("bbox")
    if isinstance(stored_bbox, (list, tuple)) and len(stored_bbox) == 4:
        return [float(v) for v in stored_bbox]

    lons = [float(d["lon"]) for _, d in graph.nodes(data=True)]
    lats = [float(d["lat"]) for _, d in graph.nodes(data=True)]
    if not lons or not lats:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(lons), min(lats), max(lons), max(lats)]


class ServiceState:
    """In-memory state holding the road graph, config, flood layer, and rainfall cache."""

    def __init__(
        self,
        config_path: str | Path = "config/default.yaml",
        repo_root: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        cfg_file = Path(config_path)
        if not cfg_file.is_absolute():
            cfg_file = self.repo_root / cfg_file
        self.config: AppConfig = load_config(cfg_file)

        self.edges_df: pd.DataFrame = load_edges_parquet(
            self.repo_root / self.config.paths.edges_path
        )
        self.graph: nx.Graph = load_graph_pickle(self.repo_root / self.config.paths.graph_path)
        self.bbox: list[float] = _compute_graph_bbox(self.graph)

        manifest_file = self.repo_root / self.config.paths.manifest_path
        if manifest_file.exists():
            entries = read_manifest(manifest_file)
            self.manifest_data: list[dict[str, Any]] = [e.model_dump() for e in entries]
        else:
            self.manifest_data = []

        demo_file = self.repo_root / "config" / "demo_scenarios.json"
        self.demo_selection_rule: str | None = None
        if demo_file.exists():
            raw_demo = json.loads(demo_file.read_text(encoding="utf-8"))
            self.demo_scenarios: list[dict[str, Any]] = list(raw_demo.get("scenarios", []))
            self.demo_selection_rule = raw_demo.get("selection_rule")
        else:
            self.demo_scenarios = []

        self.flood_layer_geojson: dict[str, Any] = _build_flood_feature_collection(
            graph=self.graph,
            config=self.config,
            repo_root=self.repo_root,
        )

        self.rain_cache: dict[str, pd.DataFrame] = {}
        self.active_as_of: str | None = None
        if self.config.rainfall.as_of_default:
            self.ensure_rain_index(self.config.rainfall.as_of_default)

        self._as_of_options: dict[str, Any] | None = None
        self._as_of_options_error: str | None = None
        self._roads_df: pd.DataFrame | None = None

    def get_as_of_options(self) -> tuple[dict[str, Any] | None, str | None]:
        """Compute the wet and dry as_of options once by the configured rule."""
        if self._as_of_options is None and self._as_of_options_error is None:
            try:
                self._as_of_options = select_as_of_options(self.config, edges_bbox(self.edges_df))
            except Exception as exc:  # reported in /meta instead of failing the service
                self._as_of_options_error = str(exc)
        return self._as_of_options, self._as_of_options_error

    def get_roads_df(self) -> pd.DataFrame:
        """Load the display road table (road_class, geometry) on first use."""
        if self._roads_df is None:
            self._roads_df = load_or_build_roads_layer(self.config, self.repo_root)
        return self._roads_df

    def resolve_weights(
        self,
        preset_id: str | None,
        alpha: float | None,
        beta: float | None,
    ) -> tuple[float, float, str | None]:
        """Resolve alpha and beta from explicit values, a preset id, or config weights."""
        cfg = self.config
        base_alpha = float(cfg.weights.alpha)
        base_beta = float(cfg.weights.beta)
        used_preset: str | None = cfg.ui.default_preset_id
        if preset_id is not None:
            preset = next((p for p in cfg.presets if p.id == preset_id), None)
            if preset is None:
                raise RoutingError("UNKNOWN_PRESET", f"Unknown preset_id '{preset_id}'.")
            base_alpha, base_beta, used_preset = float(preset.alpha), float(preset.beta), preset.id
        res_alpha = float(alpha) if alpha is not None else base_alpha
        res_beta = float(beta) if beta is not None else base_beta
        if alpha is not None or beta is not None:
            match = next(
                (p for p in cfg.presets if p.alpha == res_alpha and p.beta == res_beta), None
            )
            used_preset = match.id if match is not None else None

        if res_alpha < cfg.weights.alpha_min or res_alpha > cfg.weights.alpha_max:
            raise RoutingError(
                "INVALID_INPUT",
                f"alpha={res_alpha} is outside allowed range "
                f"[{cfg.weights.alpha_min}, {cfg.weights.alpha_max}].",
            )
        if res_beta < cfg.weights.beta_min or res_beta > cfg.weights.beta_max:
            raise RoutingError(
                "INVALID_INPUT",
                f"beta={res_beta} is outside allowed range "
                f"[{cfg.weights.beta_min}, {cfg.weights.beta_max}].",
            )
        return res_alpha, res_beta, used_preset

    def route_with_recommendation(
        self,
        origin: dict[str, float],
        destination: dict[str, float],
        alpha: float,
        beta: float,
        as_of: str,
    ) -> dict[str, Any]:
        """Run compare_routes and attach the recommendation block."""
        comparison = compare_routes(
            graph=self.graph,
            origin=origin,
            destination=destination,
            alpha=alpha,
            beta=beta,
            max_snap_distance_m=float(self.config.service.max_snap_distance_m),
            as_of=as_of,
        )
        comparison["recommendation"] = compute_route_recommendation(
            baseline_length_km=float(comparison["baseline"]["length_km"]),
            baseline_flood_km=float(comparison["baseline"]["flood_km"]),
            resilient_length_km=float(comparison["resilient"]["length_km"]),
            resilient_flood_km=float(comparison["resilient"]["flood_km"]),
            flood_tolerance_km=float(self.config.ui.flood_tolerance_km),
            max_acceptable_detour_pct=float(self.config.ui.max_acceptable_detour_pct),
        )
        return comparison

    def ensure_rain_index(self, as_of: str) -> None:
        """Load or compute rain_idx for as_of and attach to self.graph if not already active."""
        if self.active_as_of == as_of:
            return
        if as_of not in self.rain_cache:
            crs_info = self.graph.graph.get("crs", {})
            rain_df = get_or_compute_rain_index(
                config=self.config,
                edges_df=self.edges_df,
                as_of=as_of,
                lon0=crs_info.get("lon0"),
                lat0=crs_info.get("lat0"),
            )
            self.rain_cache[as_of] = rain_df

        attach_rain_index_to_graph(self.graph, self.rain_cache[as_of])
        self.graph.graph.pop("_weights_params", None)
        self.active_as_of = as_of


def create_app(
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    state: ServiceState | None = None,
) -> FastAPI:
    """Create and configure the FastAPI routing application."""
    resolved_cfg = config_path or os.environ.get("FLOODROUTER_CONFIG", "config/default.yaml")

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        if getattr(app_instance.state, "service_state", None) is None:
            app_instance.state.service_state = ServiceState(
                config_path=resolved_cfg,
                repo_root=repo_root,
            )
        yield

    application = FastAPI(
        title="Flood-Resilient Supply Chain Router API",
        lifespan=lifespan,
    )
    if state is not None:
        application.state.service_state = state

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_state(request: Request) -> ServiceState:
        svc = getattr(request.app.state, "service_state", None)
        if svc is None:
            svc = ServiceState(config_path=resolved_cfg, repo_root=repo_root)
            request.app.state.service_state = svc
        return svc

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        messages: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", []) if part != "body")
            msg = str(err.get("msg", "Invalid input"))
            messages.append(f"{loc}: {msg}" if loc else msg)
        summary_msg = "; ".join(messages) if messages else "Invalid request payload."
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "message": summary_msg,
                }
            },
        )

    @application.exception_handler(RoutingError)
    async def handle_routing_error(
        _request: Request,
        exc: RoutingError,
    ) -> JSONResponse:
        status_code = STATUS_BY_ERROR_CODE.get(exc.code, 400)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                }
            },
        )

    @application.exception_handler(ValueError)
    async def handle_value_error(
        _request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "message": str(exc),
                }
            },
        )

    @application.get("/health", response_model=HealthResponse)
    async def get_health(request: Request) -> HealthResponse:
        svc = get_state(request)
        return HealthResponse(
            status="ok",
            graph_nodes=int(svc.graph.number_of_nodes()),
            graph_edges=int(svc.graph.number_of_edges()),
        )

    @application.get("/meta", response_model=MetaResponse)
    async def get_meta(request: Request) -> MetaResponse:
        svc = get_state(request)
        cfg = svc.config
        as_of_options, as_of_error = svc.get_as_of_options()
        return MetaResponse(
            bbox=svc.bbox,
            alpha=float(cfg.weights.alpha),
            beta=float(cfg.weights.beta),
            as_of_default=str(cfg.rainfall.as_of_default or ""),
            data_manifest=svc.manifest_data,
            region=cfg.region.name,
            demo_scenarios=svc.demo_scenarios,
            demo_selection_rule=svc.demo_selection_rule,
            presets=[PresetResponse(**p.model_dump()) for p in cfg.presets],
            default_preset_id=cfg.ui.default_preset_id,
            max_acceptable_detour_pct=float(cfg.ui.max_acceptable_detour_pct),
            flood_tolerance_km=float(cfg.ui.flood_tolerance_km),
            max_batch_pairs=int(cfg.service.max_batch_pairs),
            as_of_options=as_of_options,
            as_of_options_error=as_of_error,
        )

    @application.get("/flood-layer")
    async def get_flood_layer(request: Request) -> dict[str, Any]:
        svc = get_state(request)
        return svc.flood_layer_geojson

    @application.get("/evidence-data")
    async def get_evidence_data(request: Request) -> dict[str, Any]:
        """Serve evaluation evidence directly from the configured results directory."""
        svc = get_state(request)
        results_path = svc.repo_root / svc.config.paths.results_json
        if not results_path.exists():
            return {"available": False, "message": "Run make reproduce"}
        try:
            payload = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RoutingError(
                "EVIDENCE_UNAVAILABLE",
                "Evaluation results could not be read.",
            ) from exc
        results_dir = svc.repo_root / svc.config.paths.results_dir
        figures = sorted(
            path.name
            for path in results_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}
        ) if results_dir.exists() else []
        return {"available": True, "results": payload, "figures": figures}

    @application.get("/evidence-figure/{filename}")
    async def get_evidence_figure(filename: str, request: Request) -> FileResponse:
        """Serve a figure discovered in the configured results directory."""
        svc = get_state(request)
        results_dir = (svc.repo_root / svc.config.paths.results_dir).resolve()
        figure_path = (results_dir / filename).resolve()
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".svg"}
        if (
            figure_path.parent != results_dir
            or figure_path.suffix.lower() not in allowed_suffixes
            or not figure_path.is_file()
        ):
            raise RoutingError("EVIDENCE_UNAVAILABLE", "Requested evidence figure was not found.")
        return FileResponse(figure_path)

    @application.get("/roads-layer")
    async def get_roads_layer(
        request: Request,
        min_lon: float | None = Query(default=None),
        min_lat: float | None = Query(default=None),
        max_lon: float | None = Query(default=None),
        max_lat: float | None = Query(default=None),
        zoom: float | None = Query(default=None, ge=0),
    ) -> dict[str, Any]:
        svc = get_state(request)
        ui = svc.config.ui
        given = [min_lon, min_lat, max_lon, max_lat]
        if all(v is None for v in given):
            bbox = list(svc.bbox)
        elif any(v is None for v in given):
            raise RoutingError(
                "INVALID_INPUT", "Provide all of min_lon, min_lat, max_lon, max_lat or none."
            )
        else:
            bbox = [float(v) for v in given]  # type: ignore[arg-type]
        min_tier_zoom = min(t.min_zoom for t in ui.road_classes_by_zoom)
        eff_zoom = float(zoom) if zoom is not None else float(min_tier_zoom)
        classes = classes_for_zoom(ui.road_classes_by_zoom, eff_zoom)
        layer = build_roads_layer(
            roads=svc.get_roads_df(),
            bbox=bbox,
            classes=classes,
            simplify_tolerance_deg=float(ui.roads_simplify_tolerance_deg),
            max_features=int(ui.roads_max_features),
        )
        layer["zoom"] = eff_zoom
        layer["bbox"] = bbox
        return layer

    @application.get("/scenarios")
    async def get_scenarios(request: Request) -> dict[str, Any]:
        svc = get_state(request)
        return {"scenarios": svc.demo_scenarios}

    def _activate_as_of(svc: ServiceState, as_of: str) -> None:
        try:
            svc.ensure_rain_index(as_of)
        except ValueError:
            raise
        except Exception as exc:
            raise RoutingError(
                "RAINFALL_UNAVAILABLE",
                f"Rainfall data for as_of={as_of} could not be loaded: {exc}",
            ) from exc

    @application.post("/route", response_model=RouteCompareResponse)
    async def post_route(
        payload: RouteRequest,
        request: Request,
    ) -> RouteCompareResponse:
        svc = get_state(request)
        cfg = svc.config
        alpha, beta, preset_id = svc.resolve_weights(payload.preset_id, payload.alpha, payload.beta)
        as_of = payload.as_of if payload.as_of is not None else str(cfg.rainfall.as_of_default)
        _activate_as_of(svc, as_of)

        comparison = svc.route_with_recommendation(
            origin={"lat": payload.origin.lat, "lon": payload.origin.lon},
            destination={"lat": payload.destination.lat, "lon": payload.destination.lon},
            alpha=alpha,
            beta=beta,
            as_of=as_of,
        )
        comparison["params"]["preset_id"] = preset_id
        return RouteCompareResponse(**comparison)

    def _run_batch(svc: ServiceState, payload: BatchRequest) -> BatchResponse:
        cfg = svc.config
        max_pairs = int(cfg.service.max_batch_pairs)
        if len(payload.rows) == 0:
            raise RoutingError("INVALID_INPUT", "Batch must contain at least one row.")
        if len(payload.rows) > max_pairs:
            raise RoutingError(
                "BATCH_TOO_LARGE",
                f"Batch has {len(payload.rows)} rows; the maximum is {max_pairs}.",
            )
        alpha, beta, preset_id = svc.resolve_weights(payload.preset_id, payload.alpha, payload.beta)
        as_of = payload.as_of if payload.as_of is not None else str(cfg.rainfall.as_of_default)
        _activate_as_of(svc, as_of)

        results: list[BatchRowResult] = []
        for idx, raw in enumerate(payload.rows):
            try:
                origin, dest = parse_batch_row(raw)
            except BatchRowError as exc:
                results.append(
                    BatchRowResult(
                        row=idx, status="error", error_code=exc.code, error_message=exc.message
                    )
                )
                continue
            o = {"lat": origin.lat, "lon": origin.lon}
            d = {"lat": dest.lat, "lon": dest.lon}
            try:
                comp = svc.route_with_recommendation(o, d, alpha, beta, as_of)
            except Exception as exc:  # isolate every row failure
                code = exc.code if isinstance(exc, RoutingError) else "ROW_FAILED"
                results.append(
                    BatchRowResult(
                        row=idx,
                        status="error",
                        origin=o,
                        destination=d,
                        error_code=code,
                        error_message=str(exc),
                    )
                )
                continue
            rec = comp["recommendation"]
            results.append(
                BatchRowResult(
                    row=idx,
                    status="ok",
                    origin=o,
                    destination=d,
                    baseline_length_km=rec["baseline_length_km"],
                    baseline_flood_km=rec["baseline_flood_km"],
                    resilient_length_km=rec["resilient_length_km"],
                    resilient_flood_km=rec["resilient_flood_km"],
                    avoided_flood_km=rec["avoided_flood_km"],
                    extra_distance_km=rec["extra_distance_km"],
                    extra_distance_pct=rec["extra_distance_pct"],
                    recommendation=rec["status"],
                )
            )
        return BatchResponse(
            rows=results,
            totals=summarize_batch(results),
            params=RouteParamsResponse(alpha=alpha, beta=beta, as_of=as_of, preset_id=preset_id),
        )

    @application.post("/route/batch", response_model=BatchResponse)
    async def post_route_batch(payload: BatchRequest, request: Request) -> BatchResponse:
        return _run_batch(get_state(request), payload)

    @application.post("/route/batch/csv")
    async def post_route_batch_csv(payload: BatchRequest, request: Request) -> Response:
        result = _run_batch(get_state(request), payload)
        return Response(
            content=batch_to_csv(result.rows, result.totals),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="route_batch.csv"'},
        )

    return application


app = create_app()
