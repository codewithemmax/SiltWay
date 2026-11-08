# flood-router

Flood-resilient supply chain routing service for Nigeria.

## Overview

`flood-router` scores road network segments by historical flood exposure and recent rainfall anomaly, returning a flood-resilient route alongside a standard shortest-distance baseline route and reporting flood-zone kilometres for each.

## Known Limitations

1. Both the 2012 training flood extents and the October 2022 held-out evaluation flood extents are coarse satellite water detection polygons, not road-level inundation measurements.
2. NASA POWER daily precipitation (`PRECTOTCORR`) is coarse relative to individual road segments; we evaluate with and without the rainfall term (`beta = 0` ablation).
3. Historical flood polygons do not capture new or altered flood pathways outside observed satellite extents.
