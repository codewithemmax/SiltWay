# 03 ROUTING AND EVALUATION SPEC

Build rule: no hard-coded edge cases (see 00_PROJECT.md section 3).

## Edge weight
`w(e) = L(e) * (1 + alpha * F(e) + beta * R(e))`
- `L(e)` length in km, `F(e)` flood fraction 0 to 1, `R(e)` rainfall index 0 to 1
- `alpha, beta >= 0`, from config or request. `alpha = beta = 0` must reproduce the baseline exactly (this is a test).

## Routing
- Baseline: Dijkstra on `L(e)`. Resilient: Dijkstra on `w(e)`. Same graph, same snapped endpoints.
- Snapping: nearest graph node by projected distance. If distance exceeds `max_snap_distance_m`, return `OUT_OF_COVERAGE`. No hard-coded bounding boxes: coverage comes from the loaded graph.
- No path: return `NO_PATH`.
- Per route report: `length_km, flood_km (sum of L(e)*F(e)), weighted_cost, n_edges, geometry`.

## Required tests (invariants, not named roads)
1. alpha = beta = 0 gives identical path and length as baseline
2. Resilient `flood_km` is less than or equal to baseline `flood_km` on random pairs when beta = 0 (weights are monotone in F), within float tolerance
3. Resilient `length_km` is greater than or equal to baseline `length_km` (baseline is length-optimal)
4. All edge weights are finite and non-negative
5. Routes are connected paths from the origin node to the destination node

## Evaluation protocol (notebook, fully seeded)
1. Sample `n_pairs` node pairs uniformly at random from the largest component with straight-line distance at least `min_trip_km`. Seed from config. Same pairs for every method.
2. Methods: baseline (length only), resilient, and an alpha-only variant (beta = 0) as an ablation of the rainfall term.
3. Metrics per method: `flood_km` (mean, median, p90), `length_km`, extra distance % vs baseline, routing latency ms (median, p95), count of unreachable pairs.
4. Headline numbers, always both:
   - reduction in flood_km over all pairs
   - reduction over only the affected pairs (baseline `flood_km > 0`)
5. Sensitivity sweep over `alpha_grid x beta_grid`. Plot flood_km reduction vs extra distance %. Choose the default from the knee of the curve and say how it was chosen.
6. Held-out check: if flood polygons carry a date or event field, build F(e) from events before a cutoff and measure flood_km against the later events. The cutoff is chosen by a stated rule (for example the median event date), never by trying cutoffs until the result looks good. If no date field, split flood polygons spatially into two halves by a seeded random assignment of polygons, build F(e) from one half, and measure on the other. Label this a weaker check.
7. Write all figures and a `results.json` to `results/`. The deck reads from these.

## Reporting rules
- Report negative or small results as they are
- State that the main metric is avoidance of known flood zones and not prediction of real-world flooding
- Include the three known weaknesses from 00_PROJECT.md section 5 in the README
