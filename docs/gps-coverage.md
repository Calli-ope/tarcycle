# GPS coverage: where riders go vs the router graph

Notes for revisiting and debugging the work on branch `bikeroutes_expansion`.
Usage and file formats are in the README ("GPS traces vs OSM"); this file
records what was built, why, what we found and where it can break.

## Purpose

1. Find the ways Smart Bike riders use that the router cannot ("gaps"), so
   the routing graph can be expanded with paths people actually ride.
2. Give the router a per-way usage signal (trips in 2025) for future
   popularity-based weights.
3. A demo map for the report and presentation (`coverage.html`).

After merging `main` (routing rework), the router includes footways, reads
`cycleway*`/`surface`/`bicycle`, and uses `trips` as a popularity factor.

## Key facts to remember

- **Footways were not in the router graph** when this analysis ran: osmnx's
  `bike` filter drops every `highway=footway`. The router now uses that
  filter without `footway` (`BIKE_FILTER` in `backend/graph.py`), so
  footways are routable unless `bicycle=no`.
- **Cycle lanes and surfaces**: the router now downloads `cycleway*`,
  `surface` and `bicycle`, so lane and cobblestone factors apply.
- **`in_bike_graph` is a copy of the router's filter**
  (`in_router_network()` in `gps_coverage.py`), not read from the router.
  If the router's filter changes, change that function the same way and run
  `--from-cache --refresh-osm`.
- **The router uses `trips`** as a popularity factor (`popularity_weight`,
  `popularity_saturation_trips` in `RoutingPreferences`).

## Pipeline (`backend/gps_coverage.py`)

Input: `locations_*.csv` (22 files, 16 GB; GPS point every ~10 s per trip) and
optionally `routes_2025.csv`. Raw data is private and never committed.

1. **OSM reference**: `ox.graph_from_polygon(network_type="all",
   simplify=False)` over Tartu + 500 m, with extra tags (`bicycle`,
   `cycleway*`, `surface`). Unsimplified, so each edge belongs to exactly one
   OSM way (`way_id`). Each edge gets `edge_class` (cycle_infra / bikeable /
   not_bikeable) and `in_bike_graph`. Cached in `osm_edges.gpkg`.
2. **Trip filter**: with `--routes`, keep trips with `Length > 0` and
   `DurationMinutes >= 2` (660,556 of 746,814). Only `RouteCode, Length,
   DurationMinutes` are read; the file also has birth year and ID code.
3. **Read in chunks** of 1M rows; the last trip of each chunk is carried
   into the next (rows are grouped by trip), so trips are never split.
4. **Clean and densify** (`densify_tracks`): drop segments between
   different trips, gaps > 200 m or > 60 s, speed > 12 m/s (GPS spikes).
   Resample every 5 m. Drop the first and last 50 m of each trip (station
   surroundings looked like busy paths otherwise).
5. **Rasterise** onto a 10 m grid in EPSG:3301; per cell count distinct
   trips and distinct riders.
6. **Compare with OSM** (`compare_with_osm`): per cell, the distance to the
   nearest cycle-infra way, bikeable way, any way and router-graph way.
   15 m counts as "on" a way (GPS error + road width).
7. **Privacy filter**: only cells with ≥ 10 trips and ≥ 5 riders are used
   for exported layers.
8. **Per-way layer** (`way_usage`): cells are matched to their nearest edge
   and grouped by `way_id`. Geometry is only the ridden pieces. For ways
   outside the router graph, only cells with no router way within 15 m count,
   so riding on a road doesn't leak onto its sidewalk.
9. **Gap groups**: share of the ridden geometry within 25 m of a router way
   (`alongside_share`, stored rounded down): `separate` < 0.5, `partly`
   0.5–0.8, `alongside` ≥ 0.8. On the map `separate` + `partly` are green
   (gaps GPS covers), `alongside` grey (sidewalks, no routing value).

`--from-cache` reruns steps 6–9 from `cells.geojson` (all privacy-filtered
cells), so layers can be rebuilt without the raw data in ~15 s.

## Results (2025)

- 94.8% of ridden distance on router-graph ways, 4.6% on other OSM ways,
  0.6% where OSM has no way.
- Gaps: 162 km ridden. `separate` + `partly` 71 km (43% of gap riding),
  `alongside` 91 km (57%).
- Joining `gps_ways.geojson` onto the router's graph with `edge_gps_trips`:
  90.5% of its edges get a trip count. Ways flagged `in_bike_graph` but
  absent from it (12.7%) are mostly outside Tartu's boundary (our area has a
  500 m buffer, the router's has none) and carry 2.7% of trips.
- After footways joined the router graph (join by way id against the cached
  graph, not a pipeline rerun): 141.5 of the 162 km of gaps are now in it,
  94% of gap trip-km; of the real gaps (`separate` + `partly`) 57 of 71 km.
  Still missing: 20 km, mostly `service` (8 km), footways outside the Tartu
  boundary or with `bicycle=no` (10.7 km) and steps. `summary.json` and the
  `gap_group` values still describe the old graph until the pipeline reruns
  (`--from-cache --refresh-osm`; it failed while Overpass was overloaded).

## History of decisions

- **First run**: four cell classes and "candidate zones" (busy cells off the
  bike network). Rejected as the main output: candidates were mostly
  sidewalks next to existing paths and station surroundings, not new paths.
  Still on the map under "First analysis".
- **Second run**: trimmed trip ends, dropped aborted rentals, per-way layer
  keyed by OSM way id.
- **Ridden parts only**: whole-way geometry drew gaps 2× longer than ridden
  (261 vs 131 km); now only ridden edge pieces are drawn.
- **Gap groups** added after noticing most gaps run alongside roads.

## Debugging notes

- **Memory**: whole files don't fit in 8 GB RAM; chunked reading peaks at
  ~2.5 GB. Full run ~15 min.
- **Overpass 502 / "at capacity"**: the public OSM API is overloaded; retry
  later. osmnx caches responses under `cache/`.
- **Map shows old code or data**: browser cache, reload with Cmd+Shift+R.
  The server without `--reload` keeps old Python code; restart it.
- **"Unknown coverage layer"**: the server runs an old `app.py`; restart.
- **Map layers empty in headless Chrome**: don't use `--virtual-time-budget`
  (it stalls MapLibre's web worker); drive Chrome via DevTools in real time.
- **Tests**: `python -m pytest` (48), including an offline end-to-end run on
  synthetic trips with a fake OSM network.

## Open issues

- `locations_202510_1.csv` (first part of October) missing from the data.
- Trip filter is simpler than Kärt's cleaning rules (drop < 30 s, drop trips
  ending at Töökoda or at undetermined stations, flag > 5 h). Align once the
  shared trip-cleaning code exists.
- Counts are Smart Bike trips only (92% e-bikes), not all cyclists.
- OSM pedestrian squares are areas, not ways: riding across them is
  `no_osm_way`.
- `steps` appear in gaps (1.3 km): likely GPS noise next to stairs.
