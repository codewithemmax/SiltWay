"""Edge weight calculation module: w(e) = L(e) * (1 + alpha * F(e) + beta * R(e))."""

from __future__ import annotations

import math

import networkx as nx
import numpy as np

METERS_PER_KM = 1000


def compute_edge_weight(
    length_km: float | np.ndarray,
    flood_frac: float | np.ndarray,
    rain_idx: float | np.ndarray,
    alpha: float,
    beta: float,
) -> float | np.ndarray:
    """Compute edge weight w(e) = L(e) * (1 + alpha * F(e) + beta * R(e))."""
    if alpha < 0.0 or beta < 0.0:
        raise ValueError(f"alpha ({alpha}) and beta ({beta}) must be non-negative.")

    weights = length_km * (1.0 + alpha * flood_frac + beta * rain_idx)
    if isinstance(weights, (int, float)):
        if not math.isfinite(weights) or weights < 0.0:
            raise ValueError("Computed edge weights must be finite and non-negative.")
        return float(weights)
    if np.any(~np.isfinite(weights)) or np.any(weights < 0.0):
        raise ValueError("Computed edge weights must be finite and non-negative.")
    return weights


def apply_weights_to_graph(
    graph: nx.Graph,
    alpha: float,
    beta: float,
    flood_attr: str = "flood_frac",
    rain_attr: str = "rain_idx",
) -> nx.Graph:
    """Compute and store 'length_km' and 'weight' attributes on every edge in graph."""
    if alpha < 0.0 or beta < 0.0:
        raise ValueError(f"alpha ({alpha}) and beta ({beta}) must be non-negative.")

    for _, _, data in graph.edges(data=True):
        length_m = float(data["length_m"])
        length_km = length_m / float(METERS_PER_KM)
        f_val = float(data.get(flood_attr, 0.0))
        r_val = float(data.get(rain_attr, 0.0))
        if f_val < 0.0 or f_val > 1.0 or r_val < 0.0 or r_val > 1.0:
            raise ValueError("Edge flood_frac and rain_idx must be in [0, 1].")
        w_val = float(compute_edge_weight(length_km, f_val, r_val, alpha, beta))
        data["length_km"] = length_km
        data["weight"] = w_val

    graph.graph["_weights_params"] = (float(alpha), float(beta), flood_attr, rain_attr)
    return graph
