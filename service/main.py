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
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from shapely.geometry import mapping

from floodrouter.config import AppConfig, load_config
from floodrouter.flood import load_flood_polygons
from floodrouter.ingest import load_region_boundary
from floodrouter.manifest import read_manifest
from floodrouter.network import load_edges_parquet, load_graph_pickle
from floodrouter.rainfall import attach_rain_index_to_graph, get_or_compute_rain_index
from floodrouter.routing import RoutingError, compare_routes
from service.schemas import (
    HealthResponse,
    MetaResponse,
    RouteCompareResponse,
    RouteRequest,
)

STATUS_BY_ERROR_CODE: dict[str, int] = {
    "INVALID_INPUT": 422,
    "OUT_OF_COVERAGE": 400,
    "NO_PATH": 404,
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
        if demo_file.exists():
            raw_demo = json.loads(demo_file.read_text(encoding="utf-8"))
            self.demo_scenarios: list[dict[str, Any]] = list(raw_demo.get("scenarios", []))
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
        return MetaResponse(
            bbox=svc.bbox,
            alpha=float(svc.config.weights.alpha),
            beta=float(svc.config.weights.beta),
            as_of_default=str(svc.config.rainfall.as_of_default or ""),
            data_manifest=svc.manifest_data,
            region=svc.config.region.name,
            demo_scenarios=svc.demo_scenarios,
        )

    @application.get("/flood-layer")
    async def get_flood_layer(request: Request) -> dict[str, Any]:
        svc = get_state(request)
        return svc.flood_layer_geojson

    @application.get("/scenarios")
    async def get_scenarios(request: Request) -> dict[str, Any]:
        svc = get_state(request)
        return {"scenarios": svc.demo_scenarios}

    @application.post("/route", response_model=RouteCompareResponse)
    async def post_route(
        payload: RouteRequest,
        request: Request,
    ) -> RouteCompareResponse:
        svc = get_state(request)
        cfg = svc.config

        alpha = float(payload.alpha) if payload.alpha is not None else float(cfg.weights.alpha)
        beta = float(payload.beta) if payload.beta is not None else float(cfg.weights.beta)
        as_of = payload.as_of if payload.as_of is not None else str(cfg.rainfall.as_of_default)

        if alpha < cfg.weights.alpha_min or alpha > cfg.weights.alpha_max:
            raise RoutingError(
                "INVALID_INPUT",
                f"alpha={alpha} is outside allowed range "
                f"[{cfg.weights.alpha_min}, {cfg.weights.alpha_max}].",
            )
        if beta < cfg.weights.beta_min or beta > cfg.weights.beta_max:
            raise RoutingError(
                "INVALID_INPUT",
                f"beta={beta} is outside allowed range "
                f"[{cfg.weights.beta_min}, {cfg.weights.beta_max}].",
            )

        svc.ensure_rain_index(as_of)

        comparison = compare_routes(
            graph=svc.graph,
            origin={"lat": payload.origin.lat, "lon": payload.origin.lon},
            destination={"lat": payload.destination.lat, "lon": payload.destination.lon},
            alpha=alpha,
            beta=beta,
            max_snap_distance_m=float(cfg.service.max_snap_distance_m),
            as_of=as_of,
        )
        return RouteCompareResponse(**comparison)

    return application


app = create_app()
