"""Shortest-path baseline and flood-resilient routing on the road network."""

from __future__ import annotations

import math
from typing import Any

import networkx as nx
import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LineString, mapping

from floodrouter.ingest import project_to_metric
from floodrouter.weights import METERS_PER_KM, apply_weights_to_graph

MAX_LAT_DEG = 90
MAX_LON_DEG = 180


class RoutingError(Exception):
    """Structured routing exception carrying an API error code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _get_or_build_spatial_index(graph: nx.Graph) -> dict[str, Any]:
    """Build and cache a cKDTree over projected node coordinates on graph."""
    cached = graph.graph.get("_spatial_index")
    if cached is not None and cached.get("n_nodes") == graph.number_of_nodes():
        return cached

    node_ids: list[int] = []
    coords_xy: list[tuple[float, float]] = []
    for nid, data in graph.nodes(data=True):
        node_ids.append(int(nid))
        coords_xy.append((float(data["x"]), float(data["y"])))

    if not node_ids:
        raise RoutingError("OUT_OF_COVERAGE", "Loaded road graph contains no nodes.")

    xy_arr = np.array(coords_xy, dtype=np.float64)
    crs_info = graph.graph.get("crs", {})
    lon0 = float(crs_info.get("lon0", 0.0))
    lat0 = float(crs_info.get("lat0", 0.0))

    index_data = {
        "n_nodes": len(node_ids),
        "node_ids": np.array(node_ids, dtype=np.int64),
        "xy": xy_arr,
        "tree": cKDTree(xy_arr),
        "lon0": lon0,
        "lat0": lat0,
    }
    graph.graph["_spatial_index"] = index_data
    return index_data


def snap_point_to_graph(
    graph: nx.Graph,
    lat: float,
    lon: float,
    max_snap_distance_m: float,
) -> tuple[int, float]:
    """Snap (lat, lon) in EPSG:4326 to the nearest graph node by projected distance in meters."""
    if not np.isfinite(lat) or not np.isfinite(lon):
        raise RoutingError("INVALID_INPUT", "Coordinates must be finite numbers.")
    if abs(lat) > float(MAX_LAT_DEG) or abs(lon) > float(MAX_LON_DEG):
        raise RoutingError(
            "INVALID_INPUT",
            f"Coordinates ({lat}, {lon}) out of valid geographic bounds.",
        )

    idx_data = _get_or_build_spatial_index(graph)
    pt_metric = project_to_metric(
        shapely.points([lon, lat]),
        idx_data["lon0"],
        idx_data["lat0"],
    )
    query_xy = np.array([pt_metric.x, pt_metric.y], dtype=np.float64)
    dist_m, pos = idx_data["tree"].query(query_xy, k=1)
    snap_dist_m = float(dist_m)
    if snap_dist_m > max_snap_distance_m:
        raise RoutingError(
            "OUT_OF_COVERAGE",
            f"Point ({lat}, {lon}) is {snap_dist_m:.1f} m from the road network "
            f"(maximum snap distance is {max_snap_distance_m:.1f} m).",
        )
    node_id = int(idx_data["node_ids"][int(pos)])
    return node_id, snap_dist_m


def _stitch_path_geometry(graph: nx.Graph, node_path: list[int]) -> LineString:
    """Orient and concatenate edge LineStrings along node_path into a single LineString."""
    if not node_path:
        raise ValueError("node_path cannot be empty.")
    if len(node_path) == 1:
        nd = graph.nodes[node_path[0]]
        pt = (float(nd["lon"]), float(nd["lat"]))
        return LineString([pt, pt])

    stitched_coords: list[tuple[float, float]] = []
    for idx in range(len(node_path) - 1):
        u = node_path[idx]
        v = node_path[idx + 1]
        u_lon = float(graph.nodes[u]["lon"])
        u_lat = float(graph.nodes[u]["lat"])
        edge_data = graph.edges[u, v]
        geom = edge_data.get("geometry")
        if geom is None or geom.is_empty:
            v_lon = float(graph.nodes[v]["lon"])
            v_lat = float(graph.nodes[v]["lat"])
            seg_coords = [(u_lon, u_lat), (v_lon, v_lat)]
        else:
            raw_coords = [(float(c[0]), float(c[1])) for c in geom.coords]
            d_start = (raw_coords[0][0] - u_lon) ** 2 + (raw_coords[0][1] - u_lat) ** 2
            d_end = (raw_coords[-1][0] - u_lon) ** 2 + (raw_coords[-1][1] - u_lat) ** 2
            seg_coords = raw_coords if d_start <= d_end else list(reversed(raw_coords))

        if not stitched_coords:
            stitched_coords.extend(seg_coords)
        else:
            stitched_coords.extend(seg_coords[1:])

    if len(stitched_coords) < 2:
        nd = graph.nodes[node_path[0]]
        pt = (float(nd["lon"]), float(nd["lat"]))
        stitched_coords = [pt, pt]
    return LineString(stitched_coords)


def extract_route_metrics(
    graph: nx.Graph,
    node_path: list[int],
    flood_attr: str = "flood_frac",
) -> dict[str, Any]:
    """Compute length_km, flood_km, weighted_cost, n_edges, and geometry for node_path."""
    if not node_path:
        raise ValueError("node_path cannot be empty.")

    total_length_km = 0.0
    total_flood_km = 0.0
    total_weighted_cost = 0.0
    n_edges = len(node_path) - 1

    for idx in range(n_edges):
        u = node_path[idx]
        v = node_path[idx + 1]
        if not graph.has_edge(u, v):
            raise ValueError(f"Disconnected path: missing edge ({u}, {v}).")
        edata = graph.edges[u, v]
        len_km = float(edata.get("length_km", float(edata["length_m"]) / float(METERS_PER_KM)))
        f_val = float(edata.get(flood_attr, 0.0))
        w_val = float(edata.get("weight", len_km))

        total_length_km += len_km
        total_flood_km += len_km * f_val
        total_weighted_cost += w_val

    geom = _stitch_path_geometry(graph, node_path)
    return {
        "nodes": node_path,
        "length_km": total_length_km,
        "flood_km": total_flood_km,
        "weighted_cost": total_weighted_cost,
        "n_edges": n_edges,
        "geometry": mapping(geom),
        "shapely_geometry": geom,
    }


def compute_route_delta(
    baseline_length_km: float,
    baseline_flood_km: float,
    resilient_length_km: float,
    resilient_flood_km: float,
) -> dict[str, float | None]:
    """Compute flood_km_reduction_pct and extra_distance_pct with null on zero denominator."""
    if baseline_flood_km <= 0.0:
        flood_reduction_pct: float | None = None
    else:
        flood_reduction_pct = (
            (baseline_flood_km - resilient_flood_km) / baseline_flood_km
        ) * 100.0

    if baseline_length_km <= 0.0:
        extra_distance_pct: float | None = None
    else:
        extra_distance_pct = (
            (resilient_length_km - baseline_length_km) / baseline_length_km
        ) * 100.0

    return {
        "flood_km_reduction_pct": flood_reduction_pct,
        "extra_distance_pct": extra_distance_pct,
    }


def compute_route_recommendation(
    baseline_length_km: float,
    baseline_flood_km: float,
    resilient_length_km: float,
    resilient_flood_km: float,
    flood_tolerance_km: float,
    max_acceptable_detour_pct: float,
) -> dict[str, Any]:
    """Classify a baseline versus resilient comparison into a dispatch status.

    Statuses:
      clear: baseline crosses no mapped flood zone.
      recommended: resilient flood_km is lower by more than flood_tolerance_km and
        the extra distance is within max_acceptable_detour_pct.
      trade_off: resilient flood_km is lower by more than flood_tolerance_km but
        the extra distance exceeds max_acceptable_detour_pct.
      no_better_route: baseline is flooded and the resilient route is not lower
        beyond flood_tolerance_km.
    """
    extra_distance_km = resilient_length_km - baseline_length_km
    if baseline_length_km > 0.0:
        extra_distance_pct = (extra_distance_km / baseline_length_km) * 100.0
    else:
        extra_distance_pct = 0.0
    avoided_flood_km = baseline_flood_km - resilient_flood_km

    if baseline_flood_km <= 0.0:
        status = "clear"
    elif resilient_flood_km < baseline_flood_km - flood_tolerance_km:
        status = "recommended" if extra_distance_pct <= max_acceptable_detour_pct else "trade_off"
    else:
        status = "no_better_route"

    return {
        "status": status,
        "baseline_length_km": baseline_length_km,
        "baseline_flood_km": baseline_flood_km,
        "resilient_length_km": resilient_length_km,
        "resilient_flood_km": resilient_flood_km,
        "avoided_flood_km": avoided_flood_km,
        "extra_distance_km": extra_distance_km,
        "extra_distance_pct": extra_distance_pct,
        "flood_tolerance_km": flood_tolerance_km,
        "max_acceptable_detour_pct": max_acceptable_detour_pct,
    }


def extract_flooded_segments(
    graph: nx.Graph,
    node_path: list[int],
    flood_attr: str = "flood_frac",
) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of route edges with a positive flood fraction."""
    features: list[dict[str, Any]] = []
    for idx in range(len(node_path) - 1):
        u = node_path[idx]
        v = node_path[idx + 1]
        edata = graph.edges[u, v]
        f_val = float(edata.get(flood_attr, 0.0))
        if f_val <= 0.0:
            continue
        len_km = float(edata.get("length_km", float(edata["length_m"]) / float(METERS_PER_KM)))
        geom = edata.get("geometry")
        if geom is None or geom.is_empty:
            geom = LineString(
                [
                    (float(graph.nodes[u]["lon"]), float(graph.nodes[u]["lat"])),
                    (float(graph.nodes[v]["lon"]), float(graph.nodes[v]["lat"])),
                ]
            )
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "edge_id": int(edata.get("edge_id", -1)),
                    "length_km": len_km,
                    "flood_frac": f_val,
                    "flood_km": len_km * f_val,
                },
                "geometry": mapping(geom),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _bearing_degrees(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Return the forward bearing for two longitude/latitude points."""
    lon1, lat1 = (math.radians(value) for value in start)
    lon2, lat2 = (math.radians(value) for value in end)
    delta_lon = lon2 - lon1
    bearing = math.degrees(
        math.atan2(
            math.sin(delta_lon) * math.cos(lat2),
            math.cos(lat1) * math.sin(lat2)
            - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon),
        )
    )
    return (bearing + 360.0) % 360.0


def build_route_steps(
    graph: nx.Graph,
    node_path: list[int],
    flood_attr: str = "flood_frac",
) -> list[dict[str, Any]]:
    """Describe each mapped road segment in a route using stored geometry."""
    steps: list[dict[str, Any]] = []
    previous_bearing: float | None = None
    for index in range(len(node_path) - 1):
        u, v = node_path[index], node_path[index + 1]
        edge = graph.edges[u, v]
        start = (float(graph.nodes[u]["lon"]), float(graph.nodes[u]["lat"]))
        end = (float(graph.nodes[v]["lon"]), float(graph.nodes[v]["lat"]))
        bearing = _bearing_degrees(start, end)
        if previous_bearing is None:
            instruction = "Start on the mapped road segment."
        else:
            delta = (bearing - previous_bearing + 540) % 360 - 180
            if abs(delta) < 30:
                instruction = "Continue on the mapped road segment."
            elif delta > 0.0:
                instruction = "Turn right onto the mapped road segment."
            else:
                instruction = "Turn left onto the mapped road segment."
        length_km = float(edge.get("length_km", edge["length_m"] / METERS_PER_KM))
        flood_km = length_km * float(edge.get(flood_attr, 0.0))
        steps.append(
            {
                "step": index + 1,
                "instruction": instruction,
                "road_label": "Mapped road segment",
                "start": {"lat": start[1], "lon": start[0]},
                "end": {"lat": end[1], "lon": end[0]},
                "length_km": length_km,
                "flood_km": flood_km,
            }
        )
        previous_bearing = bearing
    return steps


def route_between_nodes(
    graph: nx.Graph,
    origin_node: int,
    destination_node: int,
    alpha: float,
    beta: float,
    flood_attr: str = "flood_frac",
    rain_attr: str = "rain_idx",
) -> dict[str, Any]:
    """Compute baseline (length-only) and resilient (weighted) Dijkstra paths between nodes."""
    if origin_node not in graph or destination_node not in graph:
        raise RoutingError("OUT_OF_COVERAGE", "Origin or destination node not in graph.")

    desired_params = (float(alpha), float(beta), flood_attr, rain_attr)
    if graph.graph.get("_weights_params") != desired_params:
        apply_weights_to_graph(
            graph=graph,
            alpha=alpha,
            beta=beta,
            flood_attr=flood_attr,
            rain_attr=rain_attr,
        )

    try:
        base_nodes = nx.shortest_path(
            graph,
            source=origin_node,
            target=destination_node,
            weight="length_km",
        )
        res_nodes = nx.shortest_path(
            graph,
            source=origin_node,
            target=destination_node,
            weight="weight",
        )
    except nx.NetworkXNoPath as exc:
        raise RoutingError(
            "NO_PATH",
            f"No road path exists between node {origin_node} and node {destination_node}.",
        ) from exc

    baseline_report = extract_route_metrics(graph, base_nodes, flood_attr=flood_attr)
    resilient_report = extract_route_metrics(graph, res_nodes, flood_attr=flood_attr)
    delta = compute_route_delta(
        baseline_length_km=baseline_report["length_km"],
        baseline_flood_km=baseline_report["flood_km"],
        resilient_length_km=resilient_report["length_km"],
        resilient_flood_km=resilient_report["flood_km"],
    )
    return {
        "baseline": baseline_report,
        "resilient": resilient_report,
        "delta": delta,
    }


def compare_routes(
    graph: nx.Graph,
    origin: dict[str, float],
    destination: dict[str, float],
    alpha: float,
    beta: float,
    max_snap_distance_m: float,
    as_of: str = "",
    flood_attr: str = "flood_frac",
    rain_attr: str = "rain_idx",
) -> dict[str, Any]:
    """Snap origin/destination coordinates and return baseline vs resilient route comparison."""
    u_node, u_snap_m = snap_point_to_graph(
        graph=graph,
        lat=float(origin["lat"]),
        lon=float(origin["lon"]),
        max_snap_distance_m=max_snap_distance_m,
    )
    v_node, v_snap_m = snap_point_to_graph(
        graph=graph,
        lat=float(destination["lat"]),
        lon=float(destination["lon"]),
        max_snap_distance_m=max_snap_distance_m,
    )

    result = route_between_nodes(
        graph=graph,
        origin_node=u_node,
        destination_node=v_node,
        alpha=alpha,
        beta=beta,
        flood_attr=flood_attr,
        rain_attr=rain_attr,
    )

    base = result["baseline"]
    res = result["resilient"]
    return {
        "baseline": {
            "geometry": base["geometry"],
            "length_km": base["length_km"],
            "flood_km": base["flood_km"],
            "weighted_cost": base["weighted_cost"],
            "n_edges": base["n_edges"],
            "nodes": base["nodes"],
            "flooded_segments": extract_flooded_segments(graph, base["nodes"], flood_attr),
            "steps": build_route_steps(graph, base["nodes"], flood_attr),
        },
        "resilient": {
            "geometry": res["geometry"],
            "length_km": res["length_km"],
            "flood_km": res["flood_km"],
            "weighted_cost": res["weighted_cost"],
            "n_edges": res["n_edges"],
            "nodes": res["nodes"],
            "flooded_segments": extract_flooded_segments(graph, res["nodes"], flood_attr),
            "steps": build_route_steps(graph, res["nodes"], flood_attr),
        },
        "delta": result["delta"],
        "params": {
            "alpha": float(alpha),
            "beta": float(beta),
            "as_of": as_of,
        },
        "snap": {
            "origin_m": u_snap_m,
            "destination_m": v_snap_m,
            "origin_node": u_node,
            "destination_node": v_node,
        },
    }
