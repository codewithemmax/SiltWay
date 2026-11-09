"""Pydantic request and response schemas for the FastAPI service."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

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


def _clean_as_of(value: str | None) -> str | None:
    """Validate an optional YYYY-MM-DD as_of string."""
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


def _clean_weight(value: float | None) -> float | None:
    """Validate an optional finite weight."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("Weight parameters must be finite numbers.")
    return float(value)


class RouteRequest(BaseModel):
    """Request payload for POST /route.

    Weights resolve in this order: explicit alpha/beta, then preset_id, then config weights.
    """

    model_config = ConfigDict(extra="forbid")

    origin: LatLon
    destination: LatLon
    as_of: str | None = None
    alpha: float | None = None
    beta: float | None = None
    preset_id: str | None = None

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, value: str | None) -> str | None:
        return _clean_as_of(value)

    @field_validator("alpha", "beta")
    @classmethod
    def _validate_finite_weight(cls, value: float | None) -> float | None:
        return _clean_weight(value)


class RouteSummaryResponse(BaseModel):
    """Per-route summary in POST /route response."""

    geometry: dict[str, Any]
    length_km: float
    flood_km: float
    weighted_cost: float | None = None
    n_edges: int | None = None
    flooded_segments: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class RecommendationResponse(BaseModel):
    """Dispatch status derived from the baseline versus resilient comparison."""

    status: Literal["clear", "recommended", "trade_off", "no_better_route"]
    baseline_length_km: float
    baseline_flood_km: float
    resilient_length_km: float
    resilient_flood_km: float
    avoided_flood_km: float
    extra_distance_km: float
    extra_distance_pct: float
    flood_tolerance_km: float
    max_acceptable_detour_pct: float


class RouteDeltaResponse(BaseModel):
    """Difference metrics between resilient and baseline routes."""

    flood_km_reduction_pct: float | None
    extra_distance_pct: float | None


class RouteParamsResponse(BaseModel):
    """Effective parameters used for a routing request."""

    alpha: float
    beta: float
    as_of: str
    preset_id: str | None = None


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
    recommendation: RecommendationResponse


class BatchRequest(BaseModel):
    """Request payload for POST /route/batch.

    rows are validated one by one so a malformed row fails alone. Each row is either
    {"origin": {"lat", "lon"}, "destination": {"lat", "lon"}} or flat
    {"origin_lat", "origin_lon", "dest_lat", "dest_lon"}.
    """

    model_config = ConfigDict(extra="forbid")

    rows: list[dict[str, Any]]
    as_of: str | None = None
    alpha: float | None = None
    beta: float | None = None
    preset_id: str | None = None

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, value: str | None) -> str | None:
        return _clean_as_of(value)

    @field_validator("alpha", "beta")
    @classmethod
    def _validate_finite_weight(cls, value: float | None) -> float | None:
        return _clean_weight(value)


class BatchRowResult(BaseModel):
    """Per-row batch result. Metric fields are null when status is error."""

    row: int
    status: Literal["ok", "error"]
    origin: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None
    baseline_length_km: float | None = None
    baseline_flood_km: float | None = None
    resilient_length_km: float | None = None
    resilient_flood_km: float | None = None
    avoided_flood_km: float | None = None
    extra_distance_km: float | None = None
    extra_distance_pct: float | None = None
    recommendation: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class BatchTotals(BaseModel):
    """Sums over succeeded rows. extra_distance_pct is computed from the summed lengths."""

    total_rows: int
    succeeded_rows: int
    failed_rows: int
    baseline_length_km: float
    baseline_flood_km: float
    resilient_length_km: float
    resilient_flood_km: float
    avoided_flood_km: float
    extra_distance_km: float
    extra_distance_pct: float | None
    status_counts: dict[str, int]


class BatchResponse(BaseModel):
    """Response payload for POST /route/batch."""

    rows: list[BatchRowResult]
    totals: BatchTotals
    params: RouteParamsResponse


class HealthResponse(BaseModel):
    """Response payload for GET /health."""

    status: str
    graph_nodes: int
    graph_edges: int


class PresetResponse(BaseModel):
    """Weight preset served to the UI."""

    id: str
    label: str
    alpha: float
    beta: float


class MetaResponse(BaseModel):
    """Response payload for GET /meta."""

    bbox: list[float]
    alpha: float
    beta: float
    as_of_default: str
    data_manifest: list[dict[str, Any]]
    region: str | None = None
    demo_scenarios: list[dict[str, Any]] | None = None
    demo_selection_rule: str | None = None
    presets: list[PresetResponse]
    default_preset_id: str
    max_acceptable_detour_pct: float
    flood_tolerance_km: float
    max_batch_pairs: int
    as_of_options: dict[str, Any] | None = None
    as_of_options_error: str | None = None


class ErrorDetail(BaseModel):
    """Structured API error detail."""

    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Standard error wrapper: {'error': {'code': '...', 'message': '...'}}."""

    error: ErrorDetail
