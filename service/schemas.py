"""Pydantic request and response schemas for the FastAPI service."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LatLon(BaseModel):
    """Geographic coordinate in WGS84 (EPSG:4326)."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

    @field_validator("lat", "lon")
    @classmethod
    def _validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate values must be finite numbers.")
        return float(value)


class RouteRequest(BaseModel):
    """Request payload for POST /route."""

    model_config = ConfigDict(extra="forbid")

    origin: LatLon
    destination: LatLon
    as_of: str | None = None
    alpha: float | None = None
    beta: float | None = None

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) != 10:
            raise ValueError("as_of must be formatted as YYYY-MM-DD.")
        try:
            date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("as_of must be a valid calendar date in YYYY-MM-DD format.") from exc
        return cleaned

    @field_validator("alpha", "beta")
    @classmethod
    def _validate_finite_weight(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("Weight parameters must be finite numbers.")
        return float(value)


class RouteSummaryResponse(BaseModel):
    """Per-route summary in POST /route response."""

    geometry: dict[str, Any]
    length_km: float
    flood_km: float
    weighted_cost: float | None = None
    n_edges: int | None = None


class RouteDeltaResponse(BaseModel):
    """Difference metrics between resilient and baseline routes."""

    flood_km_reduction_pct: float | None
    extra_distance_pct: float | None


class RouteParamsResponse(BaseModel):
    """Effective parameters used for a routing request."""

    alpha: float
    beta: float
    as_of: str


class RouteSnapResponse(BaseModel):
    """Projected endpoint snapping distances in meters."""

    origin_m: float
    destination_m: float


class RouteCompareResponse(BaseModel):
    """Full response payload for POST /route."""

    baseline: RouteSummaryResponse
    resilient: RouteSummaryResponse
    delta: RouteDeltaResponse
    params: RouteParamsResponse
    snap: RouteSnapResponse


class HealthResponse(BaseModel):
    """Response payload for GET /health."""

    status: str
    graph_nodes: int
    graph_edges: int


class MetaResponse(BaseModel):
    """Response payload for GET /meta."""

    bbox: list[float]
    alpha: float
    beta: float
    as_of_default: str
    data_manifest: list[dict[str, Any]]
    region: str | None = None
    demo_scenarios: list[dict[str, Any]] | None = None


class ErrorDetail(BaseModel):
    """Structured API error detail."""

    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Standard error wrapper: {'error': {'code': '...', 'message': '...'}}."""

    error: ErrorDetail
