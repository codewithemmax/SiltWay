"""Road network graph construction, topology noding, endpoint snapping, and validation."""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely import Geometry

from floodrouter.config import AppConfig, load_config
from floodrouter.ingest import (
    estimate_utm_zone,
    load_region_boundary,
    load_roads,
    project_to_metric,
    project_to_storage,
)

logger = logging.getLogger(__name__)


class _UnionFind:
    """Disjoint-set data structure for clustering nearby endpoints."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, idx: int) -> int:
        root = idx
        while self.parent[root] != root:
            root = self.parent[root]
        curr = idx
        while curr != root:
            nxt = self.parent[curr]
            self.parent[curr] = root
            curr = nxt
        return root

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _cluster_points(coords: np.ndarray, tolerance_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Cluster 2D metric coordinates within tolerance_m and return (cluster_ids, centroids)."""
    n_pts = len(coords)
    if n_pts == 0:
        return np.array([], dtype=np.int64), np.empty((0, 2), dtype=np.float64)

    uf = _UnionFind(n_pts)
    if tolerance_m > 0.0:
        tree = cKDTree(coords)
        pairs = tree.query_pairs(r=tolerance_m)
        for i, j in pairs:
            uf.union(int(i), int(j))

    roots = np.array([uf.find(i) for i in range(n_pts)], dtype=np.int64)
    unique_roots, inverse_ids = np.unique(roots, return_inverse=True)
    n_clusters = len(unique_roots)

    centroids = np.zeros((n_clusters, 2), dtype=np.float64)
    counts = np.bincount(inverse_ids, minlength=n_clusters).astype(np.float64)
    np.add.at(centroids[:, 0], inverse_ids, coords[:, 0])
    np.add.at(centroids[:, 1], inverse_ids, coords[:, 1])
    centroids[:, 0] /= counts
    centroids[:, 1] /= counts

    return inverse_ids, centroids


def _snap_line_endpoints(lines_metric: np.ndarray, snap_tolerance_m: float) -> np.ndarray:
    """Snap line start/end vertices within snap_tolerance_m to shared cluster centroids."""
    n_lines = len(lines_metric)
    if n_lines == 0 or snap_tolerance_m <= 0.0:
        return lines_metric

    start_pts = shapely.get_point(lines_metric, 0)
    end_pts = shapely.get_point(lines_metric, -1)
    all_endpoints = np.vstack(
        [
            shapely.get_coordinates(start_pts),
            shapely.get_coordinates(end_pts),
        ]
    )
    cluster_ids, centroids = _cluster_points(all_endpoints, snap_tolerance_m)
    start_targets = centroids[cluster_ids[:n_lines]]
    end_targets = centroids[cluster_ids[n_lines:]]

    snapped: list[Geometry] = []
    for idx, line in enumerate(lines_metric):
        coords = np.array(line.coords, dtype=np.float64)
        coords[0] = start_targets[idx]
        coords[-1] = end_targets[idx]
        if len(coords) == 2 and np.allclose(coords[0], coords[1]):
            continue
        snapped.append(shapely.linestrings(coords))
    return np.array(snapped, dtype=object)


def build_graph(
    roads: pd.DataFrame,
    snap_tolerance_m: float,
    keep_largest_component: bool = True,
    min_valid_geom_share: float = 1.0,
) -> tuple[nx.Graph, pd.DataFrame]:
    """Build a projected, noded, and endpoint-snapped NetworkX road graph and edge table."""
    if roads is None or len(roads) == 0:
        raise ValueError("Cannot build graph from empty roads DataFrame.")

    raw_geoms = roads["geometry"].to_numpy()
    valid_non_empty = [
        g is not None and not g.is_empty and g.is_valid and g.geom_type == "LineString"
        for g in raw_geoms
    ]
    valid_share = float(np.mean(valid_non_empty)) if len(raw_geoms) > 0 else 0.0
    if valid_share < min_valid_geom_share:
        raise ValueError(
            f"Valid geometry share {valid_share:.4f} is below threshold {min_valid_geom_share:.4f}"
        )

    clean_geoms = np.array(
        [g for g, ok in zip(raw_geoms, valid_non_empty) if ok],
        dtype=object,
    )
    bounds_arr = shapely.bounds(clean_geoms)
    minx = float(np.min(bounds_arr[:, 0]))
    miny = float(np.min(bounds_arr[:, 1]))
    maxx = float(np.max(bounds_arr[:, 2]))
    maxy = float(np.max(bounds_arr[:, 3]))
    lon0 = (minx + maxx) * 0.5
    lat0 = (miny + maxy) * 0.5
    utm_info = estimate_utm_zone(lon0, lat0)

    lines_metric = project_to_metric(clean_geoms, lon0, lat0)
    snapped_lines = _snap_line_endpoints(lines_metric, snap_tolerance_m)
    if len(snapped_lines) == 0:
        raise ValueError("No valid line segments remained after endpoint snapping.")

    noded_multi = shapely.node(shapely.multilinestrings(list(snapped_lines)))
    noded_parts = shapely.get_parts(noded_multi)
    lengths_m = shapely.length(noded_parts)
    pos_mask = lengths_m > 0.0
    noded_parts = noded_parts[pos_mask]
    lengths_m = lengths_m[pos_mask]

    n_segs = len(noded_parts)
    if n_segs == 0:
        raise ValueError("No positive-length segments produced by topology noding.")

    start_coords = shapely.get_coordinates(shapely.get_point(noded_parts, 0))
    end_coords = shapely.get_coordinates(shapely.get_point(noded_parts, -1))
    all_endpoints = np.vstack([start_coords, end_coords])
    cluster_ids, node_xy = _cluster_points(all_endpoints, snap_tolerance_m)
    u_ids = cluster_ids[:n_segs]
    v_ids = cluster_ids[n_segs:]

    node_pts_metric = shapely.points(node_xy)
    node_pts_storage = project_to_storage(node_pts_metric, lon0, lat0)
    node_lonlat = shapely.get_coordinates(node_pts_storage)

    graph = nx.Graph()
    for node_id in range(len(node_xy)):
        graph.add_node(
            int(node_id),
            x=float(node_xy[node_id, 0]),
            y=float(node_xy[node_id, 1]),
            lon=float(node_lonlat[node_id, 0]),
            lat=float(node_lonlat[node_id, 1]),
        )

    next_node_id = int(len(node_xy))
    raw_edge_records: list[dict[str, Any]] = []

    for seg_idx in range(n_segs):
        u = int(u_ids[seg_idx])
        v = int(v_ids[seg_idx])
        if u == v:
            continue
        geom_m = noded_parts[seg_idx]
        seg_len_m = float(lengths_m[seg_idx])

        if not graph.has_edge(u, v):
            graph.add_edge(u, v, length_m=seg_len_m, geom_metric=geom_m)
            raw_edge_records.append(
                {
                    "u": u,
                    "v": v,
                    "length_m": seg_len_m,
                    "geom_metric": geom_m,
                }
            )
        else:
            coords = np.array(geom_m.coords, dtype=np.float64)
            mid_pt = geom_m.interpolate(0.5, normalized=True)
            mid_xy = np.array([mid_pt.x, mid_pt.y], dtype=np.float64)
            mid_lonlat_pt = project_to_storage(mid_pt, lon0, lat0)
            mid_id = next_node_id
            next_node_id += 1
            graph.add_node(
                mid_id,
                x=float(mid_xy[0]),
                y=float(mid_xy[1]),
                lon=float(mid_lonlat_pt.x),
                lat=float(mid_lonlat_pt.y),
            )
            half_len = seg_len_m * 0.5
            geom_1 = shapely.linestrings(np.vstack([coords[0], mid_xy]))
            geom_2 = shapely.linestrings(np.vstack([mid_xy, coords[-1]]))
            graph.add_edge(u, mid_id, length_m=half_len, geom_metric=geom_1)
            graph.add_edge(mid_id, v, length_m=half_len, geom_metric=geom_2)
            raw_edge_records.append(
                {
                    "u": u,
                    "v": mid_id,
                    "length_m": half_len,
                    "geom_metric": geom_1,
                }
            )
            raw_edge_records.append(
                {
                    "u": mid_id,
                    "v": v,
                    "length_m": half_len,
                    "geom_metric": geom_2,
                }
            )

    isolated = list(nx.isolates(graph))
    graph.remove_nodes_from(isolated)

    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    n_components_before = len(components)
    component_sizes_before = [len(c) for c in components]
    n_nodes_before = graph.number_of_nodes()
    n_edges_before = graph.number_of_edges()

    if keep_largest_component and n_components_before > 1:
        largest_nodes = set(components[0])
        dropped_components = n_components_before - 1
        graph = graph.subgraph(largest_nodes).copy()
        raw_edge_records = [
            rec
            for rec in raw_edge_records
            if rec["u"] in largest_nodes and rec["v"] in largest_nodes
        ]
    else:
        dropped_components = 0

    n_components_after = nx.number_connected_components(graph) if graph.number_of_nodes() > 0 else 0
    logger.info(
        "Built road graph: %d components before filtering (largest=%d nodes), "
        "dropped %d components; final graph has %d nodes and %d edges.",
        n_components_before,
        component_sizes_before[0] if component_sizes_before else 0,
        dropped_components,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )

    metric_geoms_arr = np.array([r["geom_metric"] for r in raw_edge_records], dtype=object)
    storage_geoms_arr = (
        project_to_storage(metric_geoms_arr, lon0, lat0)
        if len(metric_geoms_arr) > 0
        else np.array([], dtype=object)
    )

    edge_rows: list[dict[str, Any]] = []
    for edge_id, (rec, geom_storage) in enumerate(zip(raw_edge_records, storage_geoms_arr)):
        u = int(rec["u"])
        v = int(rec["v"])
        len_m = float(rec["length_m"])
        graph.edges[u, v]["edge_id"] = edge_id
        graph.edges[u, v]["length_m"] = len_m
        graph.edges[u, v]["flood_frac"] = 0.0
        graph.edges[u, v]["rain_idx"] = 0.0
        graph.edges[u, v]["geometry"] = geom_storage
        edge_rows.append(
            {
                "edge_id": edge_id,
                "u": u,
                "v": v,
                "length_m": len_m,
                "flood_frac": 0.0,
                "geometry": geom_storage,
            }
        )

    edges_df = pd.DataFrame(edge_rows)
    if len(edges_df) > 0:
        final_bounds = shapely.bounds(edges_df["geometry"].to_numpy())
        bbox = [
            float(np.min(final_bounds[:, 0])),
            float(np.min(final_bounds[:, 1])),
            float(np.max(final_bounds[:, 2])),
            float(np.max(final_bounds[:, 3])),
        ]
    else:
        bbox = [minx, miny, maxx, maxy]

    graph.graph["crs"] = {
        "storage": "EPSG:4326",
        "projected": utm_info["crs_name"],
        "lon0": lon0,
        "lat0": lat0,
    }
    graph.graph["bbox"] = bbox
    graph.graph["stats"] = {
        "valid_geom_share": valid_share,
        "n_components_before": n_components_before,
        "component_sizes_before": component_sizes_before[:10],
        "n_components_dropped": dropped_components,
        "n_components_after": n_components_after,
        "n_nodes_before": n_nodes_before,
        "n_edges_before": n_edges_before,
        "n_nodes_after": graph.number_of_nodes(),
        "n_edges_after": graph.number_of_edges(),
        "total_length_km": float(edges_df["length_m"].sum() / 1000.0) if len(edges_df) > 0 else 0.0,
    }
    return graph, edges_df


def validate_network(
    graph: nx.Graph,
    edges_df: pd.DataFrame,
    config: AppConfig,
) -> dict[str, Any]:
    """Run generic validation checks on the constructed road network and edge table."""
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise ValueError("Constructed road graph is empty.")
    if len(edges_df) != graph.number_of_edges():
        raise ValueError(
            f"Edge table count ({len(edges_df)}) does not match graph edges "
            f"({graph.number_of_edges()})."
        )

    geoms = edges_df["geometry"].to_numpy()
    valid_mask = [
        g is not None and not g.is_empty and g.is_valid and g.geom_type == "LineString"
        for g in geoms
    ]
    valid_share = float(np.mean(valid_mask))
    if valid_share < config.network.min_valid_geom_share:
        raise ValueError(
            f"Edge valid geometry share {valid_share:.4f} is below config threshold "
            f"{config.network.min_valid_geom_share:.4f}."
        )

    lengths = edges_df["length_m"].to_numpy(dtype=np.float64)
    if not np.all(np.isfinite(lengths)) or np.any(lengths <= 0.0):
        raise ValueError("All edges must have finite positive length_m.")

    n_comp = nx.number_connected_components(graph)
    if config.network.keep_largest_component and n_comp != 1:
        raise ValueError(f"Expected 1 connected component after filtering, found {n_comp}.")

    stats = dict(graph.graph.get("stats", {}))
    stats["edge_valid_geom_share"] = valid_share
    return stats


def save_network_artifacts(
    graph: nx.Graph,
    edges_df: pd.DataFrame,
    edges_path: str | Path,
    graph_path: str | Path,
) -> None:
    """Save edges_df to Parquet (with WKB geometry) and graph to pickle."""
    e_path = Path(edges_path)
    g_path = Path(graph_path)
    e_path.parent.mkdir(parents=True, exist_ok=True)
    g_path.parent.mkdir(parents=True, exist_ok=True)

    out_df = edges_df.copy()
    out_df["geometry"] = [shapely.to_wkb(g) for g in out_df["geometry"]]
    out_df.to_parquet(e_path, index=False)

    with g_path.open("wb") as handle:
        pickle.dump(graph, handle, protocol=pickle.HIGHEST_PROTOCOL)


def load_edges_parquet(edges_path: str | Path) -> pd.DataFrame:
    """Load edges.parquet and decode WKB geometry back to Shapely geometries."""
    df = pd.read_parquet(edges_path)
    if "geometry" in df.columns and len(df) > 0:
        first_val = df["geometry"].iloc[0]
        if isinstance(first_val, (bytes, bytearray, memoryview)):
            df["geometry"] = [shapely.from_wkb(bytes(b)) for b in df["geometry"]]
    return df


def load_graph_pickle(graph_path: str | Path) -> nx.Graph:
    """Load precomputed NetworkX graph from pickle."""
    with Path(graph_path).open("rb") as handle:
        return pickle.load(handle)


def build_network_from_config(
    config_path: str | Path = "config/default.yaml",
    overrides: dict[str, Any] | None = None,
) -> tuple[nx.Graph, pd.DataFrame, dict[str, Any]]:
    """Build road network from config, run validation checks, and save artifacts."""
    cfg = load_config(config_path, overrides=overrides)
    boundary = load_region_boundary(
        boundary_path=cfg.region.boundary_file,
        region_name=cfg.region.name,
        name_column=cfg.region.name_column or "adm1_name",
    )
    roads_df = load_roads(
        path=cfg.datasets.roads.path,
        region=boundary,
        road_class_column=cfg.datasets.roads.road_class_column,
        unknown_class_value=cfg.datasets.roads.unknown_class_value,
        include_unknown_roads=cfg.network.include_unknown_roads,
    )
    graph, edges_df = build_graph(
        roads=roads_df,
        snap_tolerance_m=cfg.network.snap_tolerance_m,
        keep_largest_component=cfg.network.keep_largest_component,
        min_valid_geom_share=cfg.network.min_valid_geom_share,
    )
    stats = validate_network(graph, edges_df, cfg)
    save_network_artifacts(
        graph=graph,
        edges_df=edges_df,
        edges_path=cfg.paths.edges_path,
        graph_path=cfg.paths.graph_path,
    )
    return graph, edges_df, stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    _, _, summary = build_network_from_config()
    print(summary)
