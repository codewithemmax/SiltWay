# 05 BUILD PLAN AND AGENT RULES

Deadline: Round 1 due 10 Nov 2026. Budget: about 36 working hours. Evaluation comes before the frontend because data rigor is a scored criterion and a pretty map without numbers loses.

Build rule: there will be no hard-coded edge cases in the code (see 00_PROJECT.md section 3). Every task below must respect it.

## Tasks, in order, each with an acceptance check
| Hours | Task | Done when |
|---|---|---|
| 0 to 3 | Repo scaffold, config loader, manifest writer, download and inspect all datasets, map columns in config | `pytest` runs, manifest lists every dataset with license and checksum |
| 3 to 9 | `ingest.py` and `network.py` | Graph builds from config only, component stats logged, validation checks pass |
| 9 to 13 | `flood.py` | `0 <= flood_frac <= 1`, flood-edge share and total flood km printed and sanity checked on a map |
| 13 to 16 | `rainfall.py` | Rain index cached per `as_of`, all edges have a value in 0 to 1 |
| 16 to 20 | `weights.py` and `routing.py` with the 5 invariant tests | All tests pass, alpha = beta = 0 equals baseline |
| 20 to 26 | `evaluate.py` and notebook, `results.json` and figures | Re-running from scratch reproduces identical numbers (same seed) |
| 26 to 29 | FastAPI service | All endpoints respond per 04, error codes tested |
| 29 to 33 | Next.js and Mapbox frontend | Click, compare, toggle, metrics panel work against the real service |
| 33 to 36 | README, deck numbers from `results.json`, demo video, submission | Deck contains no number absent from `results.json` |

## Cut order if behind (cut from the top)
1. Node gateway
2. Rainfall date picker (use the config default `as_of`)
3. Held-out check (keep the weaker spatial split and label it)
4. Rainfall term entirely (set beta to 0 and say so honestly in the deck)
Never cut: baseline comparison, seeded evaluation, invariant tests, limitations statement.

## Rules for the coding agent
1. Read 00 to 04 before writing code. Work one task at a time and stop at each acceptance check.
2. No hard-coded edge cases. If a result looks wrong for one input, find the general cause. Add a test that generates the class of input.
3. Never invent data, columns, URLs or results. If a dataset field is unknown, inspect it and ask.
4. Never write a metric into the README or deck by hand. Only read from `results/results.json`.
5. Keep functions small and pure where possible. Parameters passed explicitly, tunables from config.
6. Before each commit run: `ruff`, `pytest`, `python scripts/check_no_hardcoding.py`.
7. If something in these files is wrong or impossible, say so and propose a fix. Do not silently work around it.
8. No em dashes in any written text, comments or docs.

## Definition of done
- One command (`make reproduce` or documented script) rebuilds the graph, runs the evaluation and regenerates `results/`
- Service and frontend run locally against the processed data
- README lists data sources, licenses, setup, results, and the three known weaknesses
- Deck numbers match `results.json` exactly
