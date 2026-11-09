"""Display road layer: class filtering by zoom, bbox clipping, simplification, and a feature cap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import mapping

from floodrouter.config import AppConfig, RoadZoomTierConfig
from floodrouter.ingest import load_roads


def load_or_build_roads_layer(config: AppConfig, repo_root: Path) -> pd.DataFrame:
    """Load cached display roads (road_class, geometry) or build them from the raw road file."""
    cache_path = repo_root / config.ui.roads_layer_path
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        cached["geometry"] = shapely.from_wkb(cached["geometry_wkb"].to_numpy())
        return cached[["road_class", "geometry"]].reset_index(drop=True)

    roads = load_roads(config.datasets.roads.path, config)
    out = pd.DataFrame(
        {
            "road_class": roads["road_class"].astype(str).to_numpy(),
            "geometry": roads["geometry"].to_numpy(),
        }
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "road_class": out["road_class"],
            "geometry_wkb": shapely.to_wkb(out["geometry"].to_numpy()),
        }
    ).to_parquet(cache_path, index=False)
    return out


def classes_for_zoom(tiers: list[RoadZoomTierConfig], zoom: float) -> list[str]:
    """Return the class list of the tier with the largest min_zoom not above zoom."""
    eligible = [t for t in tiers if t.min_zoom <= zoom]
    if not eligible:
        eligible = [min(tiers, key=lambda t: t.min_zoom)]
    chosen = max(eligible, key=lambda t: t.min_zoom)
    return list(chosen.classes)


def build_roads_layer(
    roads: pd.DataFrame,
    bbox: list[float],
    classes: list[str],
    simplify_tolerance_deg: float,
    max_features: int,
) -> dict[str, Any]:
    """Filter roads by class and bbox, simplify, cap, and return a GeoJSON FeatureCollection.

    When more roads match than max_features, roads are kept in class priority order
    (the order of classes) and then by descending length, and truncated is set True.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox must satisfy min_lon < max_lon and min_lat < max_lat.")

    priority = {name: rank for rank, name in enumerate(classes)}
    class_mask = roads["road_class"].isin(list(priority.keys())).to_numpy()
    subset = roads.loc[class_mask]
    if len(subset) == 0:
        selected = subset
    else:
        box = shapely.box(min_lon, min_lat, max_lon, max_lat)
        geoms = subset["geometry"].to_numpy()
        hit = shapely.intersects(box, geoms)
        selected = subset.loc[hit]

    total_matching = int(len(selected))
    if total_matching > 0:
        geoms = selected["geometry"].to_numpy()
        order_df = pd.DataFrame(
            {
                "rank": selected["road_class"].map(priority).to_numpy(),
                "neg_len": -shapely.length(geoms),
                "pos": np.arange(total_matching),
            }
        ).sort_values(["rank", "neg_len", "pos"], kind="mergesort")
        keep_pos = order_df["pos"].to_numpy()[:max_features]
        kept = selected.iloc[keep_pos]
    else:
        kept = selected

    features: list[dict[str, Any]] = []
    for idx, (r_class, geom) in enumerate(
        zip(kept["road_class"].to_numpy(), kept["geometry"].to_numpy())
    ):
        simple = (
            shapely.simplify(geom, tolerance=simplify_tolerance_deg, preserve_topology=True)
            if simplify_tolerance_deg > 0.0
            else geom
        )
        if simple is None or simple.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "id": idx,
                "properties": {"road_class": str(r_class)},
                "geometry": mapping(simple),
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "truncated": total_matching > max_features,
        "total_matching": total_matching,
        "returned_features": len(features),
        "max_features": int(max_features),
        "road_classes": list(classes),
    }
