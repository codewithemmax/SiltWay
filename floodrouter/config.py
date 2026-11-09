"""Configuration loader and Pydantic schema validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RegionConfig(BaseModel):
    """Pilot region configuration. Region name is required with no default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Required administrative region name")
    boundary_file: str = Field(..., min_length=1)
    name_column: str | None = Field(default=None)

    @field_validator("name", mode="before")
    @classmethod
    def validate_region_name(cls, value: Any) -> str:
        if value is None:
            raise ValueError(
                "region.name is required and has no default; set it in config or overrides."
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError("region.name must be a non-empty string.")
        return value.strip()


class RoadsDatasetConfig(BaseModel):
    """Road dataset configuration."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    fallback_url: str | None = Field(default=None)
    license: str = Field(default="CC BY 4.0", min_length=1)
    road_class_column: str | None = Field(default=None)
    unknown_class_value: str = Field(..., min_length=1)


class DatasetSourceConfig(BaseModel):
    """Vector dataset path and source URL configuration."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    license: str = Field(default="CC BY-IGO", min_length=1)


class RainfallDatasetConfig(BaseModel):
    """NASA POWER precipitation dataset configuration."""

    model_config = ConfigDict(extra="forbid")

    parameter: str = Field(..., min_length=1)
    units: str = Field(..., min_length=1)
    docs_url: str = Field(..., min_length=1)
    api_base_url: str = Field(..., min_length=1)
    raw_cache_dir: str = Field(..., min_length=1)


class DatasetsConfig(BaseModel):
    """All external dataset configurations."""

    model_config = ConfigDict(extra="forbid")

    roads: RoadsDatasetConfig
    flood_2012: DatasetSourceConfig
    flood_2022: DatasetSourceConfig
    boundaries: DatasetSourceConfig
    rainfall: RainfallDatasetConfig


class PathsConfig(BaseModel):
    """File system paths for raw, processed, and evaluation outputs."""

    model_config = ConfigDict(extra="forbid")

    raw_dir: str = Field(..., min_length=1)
    processed_dir: str = Field(..., min_length=1)
    manifest_path: str = Field(..., min_length=1)
    edges_path: str = Field(..., min_length=1)
    graph_path: str = Field(..., min_length=1)
    results_dir: str = Field(..., min_length=1)
    results_json: str = Field(..., min_length=1)


class CRSConfig(BaseModel):
    """Coordinate reference system settings."""

    model_config = ConfigDict(extra="forbid")

    storage: str = Field(..., min_length=1)
    projected: str = Field(..., min_length=1)


class NetworkConfig(BaseModel):
    """Road network construction settings."""

    model_config = ConfigDict(extra="forbid")

    snap_tolerance_m: float = Field(..., gt=0)
    keep_largest_component: bool
    include_unknown_roads: bool
    min_valid_geom_share: float = Field(..., ge=0, le=1)


class WeightsConfig(BaseModel):
    """Edge weight parameters and allowed bounds."""

    model_config = ConfigDict(extra="forbid")

    alpha: float = Field(..., ge=0)
    beta: float = Field(..., ge=0)
    alpha_min: float = Field(..., ge=0)
    alpha_max: float = Field(..., gt=0)
    beta_min: float = Field(..., ge=0)
    beta_max: float = Field(..., gt=0)


class RainfallConfig(BaseModel):
    """Rainfall anomaly calculation parameters."""

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(..., gt=0)
    climatology_years: int = Field(..., gt=0)
    grid_step_deg: float = Field(..., gt=0)
    scale_percentile: float = Field(..., gt=0, le=100)
    eps: float = Field(..., gt=0)
    as_of_default: str | None = Field(default=None)


class EvaluationConfig(BaseModel):
    """Seeded evaluation parameters."""

    model_config = ConfigDict(extra="forbid")

    n_pairs: int = Field(..., gt=0)
    min_trip_km: float = Field(..., gt=0)
    seed: int
    alpha_grid: list[float] = Field(..., min_length=1)
    beta_grid: list[float] = Field(..., min_length=1)


class ServiceConfig(BaseModel):
    """FastAPI service settings."""

    model_config = ConfigDict(extra="forbid")

    max_snap_distance_m: float = Field(..., gt=0)
    flood_simplify_tolerance_deg: float = Field(..., gt=0)
    max_batch_pairs: int = Field(..., gt=0)


class PresetConfig(BaseModel):
    """Named alpha and beta weight preset rendered as a button in the UI."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    alpha: float = Field(..., ge=0)
    beta: float = Field(..., ge=0)


class RoadZoomTierConfig(BaseModel):
    """Road classes drawn at or above a given map zoom level."""

    model_config = ConfigDict(extra="forbid")

    min_zoom: float = Field(..., ge=0)
    classes: list[str] = Field(..., min_length=1)


class AsOfSelectionConfig(BaseModel):
    """Stated rule for picking the wet and dry as_of dates from rainfall data."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    start_date: str = Field(..., min_length=10, max_length=10)
    end_date: str = Field(..., min_length=10, max_length=10)
    step_days: int = Field(..., gt=0)


class UIConfig(BaseModel):
    """Dispatch console thresholds, layer caps, and date selection rule."""

    model_config = ConfigDict(extra="forbid")

    default_preset_id: str = Field(..., min_length=1)
    max_acceptable_detour_pct: float = Field(..., ge=0)
    flood_tolerance_km: float = Field(..., ge=0)
    roads_layer_path: str = Field(..., min_length=1)
    roads_simplify_tolerance_deg: float = Field(..., ge=0)
    roads_max_features: int = Field(..., gt=0)
    road_classes_by_zoom: list[RoadZoomTierConfig] = Field(..., min_length=1)
    as_of_selection: AsOfSelectionConfig


class AppConfig(BaseModel):
    """Root application configuration validated from config/default.yaml."""

    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(..., min_length=1)
    region: RegionConfig
    datasets: DatasetsConfig
    paths: PathsConfig
    crs: CRSConfig
    network: NetworkConfig
    weights: WeightsConfig
    rainfall: RainfallConfig
    evaluation: EvaluationConfig
    service: ServiceConfig
    presets: list[PresetConfig] = Field(..., min_length=1)
    ui: UIConfig

    @model_validator(mode="after")
    def _validate_presets(self) -> AppConfig:
        ids = [p.id for p in self.presets]
        if len(set(ids)) != len(ids):
            raise ValueError("presets must have unique ids.")
        w = self.weights
        for p in self.presets:
            if not (w.alpha_min <= p.alpha <= w.alpha_max):
                raise ValueError(f"preset '{p.id}' alpha is outside weights bounds.")
            if not (w.beta_min <= p.beta <= w.beta_max):
                raise ValueError(f"preset '{p.id}' beta is outside weights bounds.")
        default = next((p for p in self.presets if p.id == self.ui.default_preset_id), None)
        if default is None:
            raise ValueError("ui.default_preset_id must name an entry in presets.")
        if default.alpha != w.alpha or default.beta != w.beta:
            raise ValueError("The default preset must equal weights.alpha and weights.beta.")
        return self


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_config(
    config_path: str | Path,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load and validate YAML configuration with optional dictionary overrides."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw_data = yaml.safe_load(handle)

    if not isinstance(raw_data, dict):
        raise ValueError(f"Configuration file must contain a YAML mapping: {path}")

    if overrides:
        raw_data = _deep_merge(raw_data, overrides)

    return AppConfig.model_validate(raw_data)
