# 00 PROJECT: Flood-Resilient Supply Chain Router

Read this file first. Then read 01 to 05 in order. Context for DataNova 2026 Global Data-thon, Round 1 due 10 Nov 2026 (ppt link, working demo, optional video). Round 2 on 25 Nov 2026.

## 1. One-line goal
A routing service that scores every road segment by historical flood exposure and recent rainfall, then returns a flood-resilient route next to a standard shortest-distance route, with the flood-zone kilometres of each, so the benefit is measured on every request.

## 2. Scope
In scope:
- One pilot region (any administrative area, chosen in config, not in code)
- Road graph from GRID3 roads, flood exposure from HDX flood polygons, rainfall anomaly from NASA POWER
- Python analysis package, FastAPI routing service, Next.js and Mapbox comparison map
- A reproducible evaluation notebook with baseline vs proposed metrics

Out of scope (do not build):
- Live traffic, live flood sensing, elevation models, multi-vehicle optimization, user accounts, databases
- Training any ML model. This is a weighted network analysis, and it must be described that way

## 3. THE NO HARD-CODED EDGE CASES RULE (binding on every file in the repo)
There will be no hard-coded edge cases in the code. Concretely:
1. No branch, condition or lookup keyed on a specific place name, state name, road ID, node ID, edge ID, coordinate, date, or origin-destination pair.
2. Every tunable value (alpha, beta, snapping tolerance, rainfall window, min trip distance, random seed, pilot region, CRS choice) lives in `config/default.yaml` or in a request parameter. Config values are data, not code.
3. Failures are handled by general mechanisms: schema validation, geometry filtering, structured error codes. When a specific input breaks the code, fix the general cause and add a test that generates the whole class of input. Never patch for the one input.
4. Demo scenarios are data (`config/demo_scenarios.json`) chosen by a stated rule, for example "top 5 random pairs by baseline flood km". Hand-picking the best-looking pair is cherry-picking. Reported metrics come only from seeded random pairs.
5. Tests assert invariants (for example "resilient route never has higher edge-weight cost than baseline under the same weights"), not outputs for named roads.
6. `scripts/check_no_hardcoding.py` fails the build if source files contain lat/lon-like float literals outside tests and config, or any string from `config/forbidden_literals.txt`.
If an instruction elsewhere appears to conflict with this rule, this rule wins.

## 4. Success criteria (what judges score)
- Technical quality: clean pipeline, tests passing, one command to reproduce results
- Data and analytical rigor: baseline comparison, sensitivity sweep, stated limitations, no fabricated numbers
- Presentation: working demo, deck, 90-second story
- Never put a number in the deck or README that the evaluation notebook did not produce

## 5. Known weaknesses (state them openly, do not hide them)
- Evaluation uses the same flood polygons that build the weights, so some reduction is guaranteed by design. Mitigation: held-out check in 03 if data permits, otherwise say so plainly.
- NASA POWER rainfall is coarse relative to road segments. Mitigation: ablation with and without the rainfall term.
- Historical polygons miss new flood paths.
