# Tarcycle

Bicycle-routing prototype for Tartu. It routes between bike-rack stations
(`Rattaparklad.csv`), addresses, map clicks or typed coordinates
(`58.38, 26.72`) with its own OSMnx/NetworkX cost model, or with local Valhalla
or BRouter servers for comparison. Frontend: MapLibre GL JS; markers can be
dragged to re-plan. Addresses are looked up with Nominatim (cached) and
autocompleted with Photon.

## Run

```powershell
python -m pip install -r requirements.txt
python -m backend.graph                      # build the graph once (~1 min, Overpass with mirror fallback)
python -m uvicorn backend.app:app --reload   # API and frontend at http://127.0.0.1:8000
```

Without the build step, the app builds the graph in the background. When
`OSM_MODEL` in `backend/graph.py` changes, the app keeps using the old cache so
a busy Overpass server never blocks routing; run `python -m backend.graph` to
update it. Tests: `python -m pytest`.

## Valhalla and BRouter (optional)

```powershell
New-Item -ItemType Directory -Force brouter-data/segments4
Invoke-WebRequest https://brouter.de/brouter/segments4/E25_N55.rd5 -OutFile brouter-data/segments4/E25_N55.rd5
docker compose up -d
```

Pick the engine under "Router" in the page. The app expects Valhalla at
`http://127.0.0.1:8002` and BRouter at `http://127.0.0.1:17777`
(`TARCYCLE_VALHALLA_URL`, `TARCYCLE_BROUTER_URL`). If an engine is not
reachable, NetworkX routes instead and the status line says so.

- **Valhalla** builds tiles from the Estonia extract into `valhalla-data/` on
  first start. Costing: `BICYCLE_COSTING` in `backend/valhalla.py`;
  `use_hills` only works after building with `build_elevation: "True"` in
  `docker-compose.yml`. Alternatives come from `alternates`.
- **BRouter** uses the `trekking` profile (`TARCYCLE_BROUTER_PROFILE` for
  another, e.g. `fastbike-verylowtraffic`). Alternatives come from
  `alternativeidx=1`. No release image reads segment format v11 yet, so the
  nightly image is pinned by digest; when updating it, download a fresh
  segment file too.

## NetworkX routing model

**Graph** (`cache/tartu_bike.graphml`): OSMnx's bike network plus every
footway without `bicycle=no`, keeping the `cycleway*`, `surface` and `bicycle`
tags. Uphill grades are sampled along each edge from `data/*_dtm_5m.tif` (four
tiles, central Tartu).

**Edge cost** = length × factors from `RoutingPreferences` in
`backend/models.py`:

| Factor | Applies to | Value |
|---|---|---|
| Dedicated cycleway | `highway=cycleway`, or path/footway with `bicycle=designated` | 0.5 |
| Path | path, track, pedestrian, footways away from roads (parks, riverside) | 0.6 |
| Lane or track | `cycleway*` lane/track/shared on a road; cycleways beside a major road | 0.7 |
| Sidewalk | footway beside a road without `bicycle=yes/designated` (`float("inf")` forbids) | 1.5 |
| Calm / fast road | roads only: `maxspeed` ≤ 30 or living street / ≥ 60 or 3+ lanes; untagged = 50 km/h | 0.8 / 1.5 |
| Cobblestones | `surface=cobblestone/sett` | 2.5 |
| Major road | primary, secondary, tertiary, trunk | 3.5 |
| Uphill | average uphill grade g | 1 + g |
| Popularity | Smart Bike trips on the way, see below | 1 − 0.3·p |

"Beside" means within 25 m at a quarter, half and three quarters of the way's
length (e.g. the tracks along Turu and Narva mnt).

**Junctions** are priced in metres when passing into the next edge: traffic
lights (`highway=traffic_signals`, `crossing=traffic_signals` or
`crossing:signals=yes`) cost the distance ridden in 20 s, an unsignalled
major-road crossing 60 m, a left turn at a major junction 30 m, and a change of
facility type 20 m.

**Search**: A* over edges (so transitions can be priced) with an estimate that
never overestimates, so results equal Dijkstra's. It returns the cheapest route
if it is at most 1.5× the shortest (`maximum_detour_factor`), otherwise the
shortest. The alternative is the cheapest route once the best route's edges
cost 3× more; it is dropped if it shares over 70 % of its length with the best
route or exceeds the detour limit. The page draws it in grey; click to switch.

**Travel time** (`time_minutes`, for a mostly e-bike fleet): 18 km/h, ×0.85 on
paths, at most 12 km/h on sidewalks, ×0.7 on rough surfaces, slower uphill,
plus 20 s per traffic light, 8 s per unsignalled major crossing and 5 s per
left turn. Routes also report the number of `traffic_lights`.

Keep factors non-negative (Dijkstra requires it) and within about one order of
magnitude of 1.0: with a cycleway factor of 0.05 the router zigzagged onto
every short cycleway fragment up to the detour limit.

## Evaluating route quality

```powershell
python -m backend.evaluate --pairs 100
```

Routes random station pairs 1–6 km apart with `networkx`, `networkx_no_gps`,
`shortest` (all factors neutral), `valhalla` and BRouter's `trekking` and
`fastbike-verylowtraffic` profiles (skipped if a server is not running). Every
route is matched to the router's graph every 10 m, so all engines are measured
the same way. Per-route rows go to `data/evaluation/route_quality.csv`: length,
detour, time, share of length per class (`car_free`, `lane_or_track`,
`sidewalk`, `quiet_road`, `major_road`, `unmatched`), rough surfaces, popular
ways (≥ 1,000 trips in 2025) and major-road crossings. Times are each engine's
own estimate and not comparable. The metrics score the router's own goals, not
closeness to real GPS traces.

Means over 99 routable pairs:

| Engine | Length (m) | Detour | Car-free | Lane/track | Sidewalk | Quiet road | Major road | Rough | Popular | Major crossings |
|---|---|---|---|---|---|---|---|---|---|---|
| networkx | 4,620 | 1.15 | 55 % | 24 % | 3 % | 12 % | 1.0 % | 3.8 % | 72 % | 4.8 |
| networkx_no_gps | 4,560 | 1.13 | 59 % | 21 % | 4 % | 11 % | 0.2 % | 4.7 % | 64 % | 5.1 |
| shortest | 4,040 | 1.00 | 23 % | 18 % | 11 % | 17 % | 25.2 % | 5.0 % | 60 % | 3.3 |
| valhalla | 4,240 | 1.08 | 22 % | 30 % | 0.1 % | 25 % | 13.3 % | 1.3 % | 71 % | 4.9 |
| brouter_trekking | 4,200 | 1.06 | 21 % | 35 % | 0.8 % | 25 % | 9.6 % | 2.8 % | 75 % | 6.2 |
| brouter_low_traffic | 4,280 | 1.09 | 12 % | 25 % | 0.6 % | 39 % | 14.8 % | 2.2 % | 68 % | 3.4 |

## GPS traces vs OSM

Compares where Smart Bike riders ride with OSM and the router's graph, to find
ways riders use that the router lacks and to give the router a popularity
signal. The raw data is private and stays in `data/raw/` (gitignored).

```powershell
# Monthly locations_2025MM.csv files (from "Locations 2025.zip") in data/raw/Locations/.
# 22 files, 16 GB: ~15 min and 2.5 GB of memory, read in chunks.
python -m backend.gps_coverage --locations data/raw/Locations --routes data/raw/Routes/routes_2025.csv
python -m backend.gps_coverage --from-cache   # rebuild the layers without the raw data (~15 s)
```

`--routes` is optional and drops aborted rentals (length 0 or under 2 minutes).
The map is at <http://127.0.0.1:8000/coverage.html>. Pipeline, gap groups,
decisions and debugging notes: [docs/gps-coverage.md](docs/gps-coverage.md).

Trips are cleaned, trimmed by 50 m at each end and rasterised onto a 10 m grid;
only cells with ≥ 10 trips and ≥ 5 riders are exported (`--min-trips`,
`--min-users`). The outputs in `data/gps_coverage/` (`cells`, `edges`,
`candidates`, `gps_ways` and `summary.json`, served at `/api/coverage/{layer}`)
hold only these privacy-filtered counts and are committed.

`gps_ways.geojson` has one row per ridden OSM way: `way_id`, `trips` (median
Smart Bike trips in 2025), `in_bike_graph`, and for ways missing from the
router graph a `gap_group` (`separate`, `partly`, `alongside`). The router
joins `trips` onto edges by way id (`backend/gps_layer.py`) for the popularity
factor: `p` grows logarithmically from 0 at 10 trips to 1 at 5,000, and
`popularity_weight = 0` turns it off. `in_bike_graph` uses a copy of
`BIKE_FILTER`; if the filter changes, update `in_router_network()` and run
`--from-cache --refresh-osm`.

**Results (2025)**: 660,556 trips (first part of October missing). 94.8 % of
ridden distance is on router-graph ways, 4.6 % on other OSM ways (mostly
footways), 0.6 % where OSM has no way. Ridden ways missing from the graph:
162 km, of which 71 km are real gaps (`separate` + `partly`) and 91 km
sidewalks alongside router ways. Counts are Smart Bike trips only. Check
candidates on the ground before editing OSM, and never upload the traces.
