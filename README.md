# Tarcycle

Tarcycle is a small Tartu bicycle-routing prototype. The backend routes with
its own OSMnx/NetworkX cost model and can optionally use a local Valhalla
server instead. The MapLibre GL JS frontend displays bike-rack stations from
`Rattaparklad.csv` and routes between two selected stations.

The planner supports both station-to-station routes and address-to-address
routes. Address lookup uses OpenStreetMap Nominatim and is cached in memory.

## Run

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Build the routing graph once (downloads Tartu from Overpass, about a minute,
and tries mirrors if the main server is busy):

```powershell
python -m backend.graph
```

Start the complete app from the project root:

```powershell
python -m uvicorn backend.app:app --reload
```

Open this URL:

```text
http://127.0.0.1:8000
```

The FastAPI server serves both the API and the frontend. You do not need a
second terminal or a separate frontend server. If you skip the build step,
the app builds the graph in the background on startup.

## Valhalla (optional)

Start the Valhalla server with Docker. It reuses the tiles in `valhalla-data/`
and builds them from the Estonia OSM extract the first time:

```powershell
docker compose up -d
```

Then pick **Valhalla** under "Router" in the page to compare it with NetworkX
on the same trip. The app expects Valhalla at `http://127.0.0.1:8002`; set
`TARCYCLE_VALHALLA_URL` to use another address. If Valhalla is not reachable,
the route falls back to NetworkX and the status line says so. Its bicycle
costing lives in `BICYCLE_COSTING` in `backend/valhalla.py`. `use_hills` only
has an effect after building with `build_elevation: "True"` in
`docker-compose.yml`.

## NetworkX routing model

The graph is OSMnx's bicycle network plus every `highway=footway` that does
not carry `bicycle=no`, with the `cycleway*`, `surface` and `bicycle` tags
kept. It is cached in `cache/tartu_bike.graphml`.

DTM files in `data/` matching `*_dtm_5m.tif` are loaded automatically. Their
elevation is sampled along OSM edge geometry and cached as uphill grade
attributes in the graph. The current four tiles cover central Tartu.

Edge costs are length times factors from `RoutingPreferences` in
`backend/models.py`: dedicated cycleways (also footways and paths with
`bicycle=designated`), painted lanes and tracks on roads (`cycleway`,
`cycleway:left/right/both`), paths, sidewalks, cobblestones, major roads and
uphill grade. Changing between facility types adds `facility_transition_cost`.

Two rules use the geometry around each way (within 25 m, sampled at a
quarter, half and three quarters of its length):

- Cycleways beside a primary, secondary, tertiary or trunk road (for example
  the tracks along Turu and Narva mnt) are priced like painted lanes
  (`cycleway_factor`), not like standalone cycleways.
- Footways without `bicycle=yes/designated` beside a road are sidewalks
  (`sidewalk_factor`; set it to `float("inf")` to forbid them). Footways
  elsewhere, such as park and riverside paths, are priced as paths.

The search runs A* over edges rather than nodes so that facility transitions
can be priced; its straight-line estimate never overestimates, so results are
the same as Dijkstra's. It first finds the shortest route by physical length,
then the cheapest route under the cost model, and returns the cheapest one if
it is at most `maximum_detour_factor` (1.5) times longer than the shortest;
otherwise it returns the shortest.

It also returns an alternative, like a map app: the cheapest route once the
best route's edges cost `ALTERNATIVE_PENALTY` (3×) more. The alternative is
dropped if it shares more than `MAXIMUM_SHARED_SHARE` (70 %) of its length
with the best route or exceeds the detour limit. Valhalla's alternative comes
from its own `alternates` option. The page draws the alternative in grey;
click it to switch.

Routing factors must remain non-negative because Dijkstra's algorithm requires
non-negative edge costs. Keep the factors within about one order of magnitude
of a plain road (1.0): with a cycleway factor of 0.05 the router zigzagged
after every short cycleway fragment up to the detour limit.

## GPS traces vs OSM (bike route expansion)

Compares where Smart Bike riders actually ride (GPS traces) with OpenStreetMap
and with the router's graph, to find the ways riders use that the router
cannot: the "gaps" (mostly footways, which osmnx's `bike` network leaves out;
the router has included them since the routing rework, see below).

1. Put the monthly GPS files (`locations_2025MM.csv`, from `Locations 2025.zip`)
   in `data/raw/Locations/`. `data/raw/` is gitignored: this data is private.
2. Build the comparison. The full 2025 data (22 files, 16 GB) takes about
   15 minutes and 2.5 GB of memory; files are read in chunks. The first run
   also downloads all Tartu OSM ways once:

   ```powershell
   python -m backend.gps_coverage --locations data/raw/Locations --routes data/raw/Routes/routes_2025.csv
   ```

   `--routes` is optional; with it, aborted rentals (length 0 or under 2
   minutes) are dropped. Only `RouteCode`, `Length` and `DurationMinutes` are
   read from that file.

3. Start the server as above and open <http://127.0.0.1:8000/coverage.html>.

How it works: each trip is cleaned (route breaks, GPS spikes and gaps longer
than 200 m or 60 s are dropped), its first and last 50 m are trimmed (station
surroundings), and it is resampled every 5 m and rasterised onto a 10 m grid. Each cell counts distinct trips and riders and is classified by the
nearest OSM way within 15 m:

| Class | Meaning | What it suggests for OSM |
|---|---|---|
| `cycle_infra` | cycleway, cycle lane/track, `bicycle=designated` | nothing, already mapped |
| `road_no_infra` | a way bikes may use, without cycle infrastructure | busy ones: candidates for cycle infrastructure |
| `osm_not_bikeable` | footway, steps or `bicycle=no` | check the tagging (e.g. `bicycle=yes`) |
| `no_osm_way` | no OSM way nearby | a path missing from OSM |

Busy neighbouring cells of the last two classes are grouped into candidate
segments, ranked by median trips × length. Only cells with at least 10 trips
and 5 distinct riders are exported (`--min-trips`, `--min-users`), so the
outputs are aggregated and do not reveal individual trips. Outputs:
`data/gps_coverage/{cells,edges,candidates}.geojson` and `summary.json`
(share of ridden distance per class), served at `/api/coverage/{layer}`.

### GPS layer for routing

`data/gps_coverage/gps_ways.geojson` has one row per OSM way that riders use,
drawn as its ridden part only: `way_id` (OSM way id), `highway`, `name`,
`trips` (median Smart Bike trips in 2025), `ridden_m`, `length_m`,
`covered_share` and `in_bike_graph`. `in_bike_graph=false` means the router's
graph does not contain the way at all.
Those "gaps" also get `alongside_share` (fraction within 25 m of a way the
router has) and `gap_group`:

| `gap_group` | Meaning | Value for routing |
|---|---|---|
| `separate` | < 50% alongside: park, riverside, courtyard, shortcut | a connection the router lacks |
| `partly` | 50–80% alongside | mixed |
| `alongside` | ≥ 80% alongside, mostly sidewalks next to roads | none; the router can use the road |

The map (`coverage.html`) has independently switchable layers:

1. **Router graph**: `router_graph.geojson`, exported from the router's own
   cached graph (`backend/graph.py`).
2. **Where riders go**: all ridden ways, coloured by trips.
3. **Gaps**: green = `separate` + `partly` (gaps the GPS data covers), grey =
   `alongside` (sidewalks next to router ways).

A collapsed "First analysis" section keeps the earlier layers (grid cells,
candidate zones, trips per edge) for comparison.

These layers can be rebuilt from the saved outputs without the raw GPS data:
`python -m backend.gps_coverage --from-cache` (about 15 s).

`in_bike_graph` comes from `in_router_network()`, a copy of the router's
filter (`BIKE_FILTER` in `backend/graph.py`: osmnx's `bike` filter without
`footway`). If the router's filter changes, update that function to match and
run `--from-cache --refresh-osm`.

The router uses the layer as a popularity signal: an edge's cost factor is
multiplied by `1 - popularity_weight × p`, where `p` grows logarithmically
from 0 at 10 trips to 1 at `popularity_saturation_trips` (5,000) trips in
2025 (`RoutingPreferences`; `popularity_weight = 0` turns it off). With the
default weight of 0.3, routes for 40 random station pairs run on ways with at
least 1,000 trips 74 % of the time instead of 69 %, for 0.7 % more length.
The join is by OSM way id with `backend/gps_layer.py`:

```python
from backend.gps_layer import load_gps_ways, edge_gps_trips

gps_ways = load_gps_ways("data/gps_coverage/gps_ways.geojson")  # once
trips = edge_gps_trips(edge_attributes, gps_ways)                # per edge; None if unridden
```

`edges.geojson` (trips per OSM edge) is still written, for comparison with
earlier runs.

Outputs go to `data/gps_coverage/`. The GPS-derived files there
(`cells`, `edges`, `candidates`, `gps_ways`, `summary`) are committed so the
team can use them without the raw data. They only hold counts that passed
the privacy filter, never individual trips. The large OSM reference files
(`osm_edges.gpkg`, `study_area.geojson`, `osm_cycleways.geojson`) are
gitignored and downloaded again when needed.

### Results (2025)

From 660,556 real trips (aborted rentals dropped), with no
`locations_202510_1.csv` (first part of October missing from the data):

- 94.8% of ridden distance is on ways in the router graph, 4.6% on other OSM
  ways (mostly footways), 0.6% where OSM has no way.
- Ridden ways missing from the router graph: 162 km. Of the riding on them,
  43% is on paths away from router ways (`separate` + `partly`, 71 km) and
  57% on sidewalks alongside router ways (91 km).

Known limitations: OSM pedestrian squares mapped as areas are not ways, so
riding across them shows up as `no_osm_way`. Counts are Smart Bike trips
only, not all cyclists.
Candidates should be checked on the ground (or on imagery) before anything is
edited in OSM. Do not upload the traces themselves to OSM.

Tests: `python -m pytest`.
