# Tarcycle

Tarcycle is a small Tartu bicycle-routing prototype. The backend uses OSMnx
and NetworkX behind a routing interface that can later be implemented with
Valhalla. The MapLibre GL JS frontend displays bike-rack stations from
`Rattaparklad.csv` and routes between two selected stations.

The planner supports both station-to-station routes and address-to-address
routes. Address lookup uses OpenStreetMap Nominatim and is cached in memory.

## Run

Install dependencies:

```powershell
python -m pip install -r requirements.txt
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
second terminal or a separate frontend server.

The first route request downloads and caches the Tartu bicycle graph in
`cache/tartu_bike.graphml`.

DTM files in `data/` matching `*_dtm_5m.tif` are loaded automatically. Their
elevation is sampled along OSM edge geometry and cached as uphill grade
attributes in the graph. The current four tiles cover central Tartu.

The router also downloads and caches Tartu water features from OpenStreetMap.
Cycleways and paths within 250 metres of mapped water receive a strong
riverside preference.
Major roads near mapped water are also penalised so the router will cross to a
connected riverside cycleway instead of following a parallel arterial road.

The current route is a custom shortest path, not a guaranteed globally
optimal cycling route. It prefers dedicated cycleways, cycleway tags on roads,
paths, and tracks, while penalising major roads and cobblestones. Better
elevation, traffic, and GPS-derived preferences require additional data.
NetworkX A* is used instead of an uninformed shortest-path search; the
heuristic is a conservative geographic lower bound, so it improves search
speed without changing the weighted-route objective.

## GPS traces vs OSM (bike route expansion)

Compares where Smart Bike riders actually ride (GPS traces) with the
OpenStreetMap network, to find paths worth adding to or re-tagging in the
OSM bike map.

1. Put the monthly GPS files (`locations_2025MM.csv`, from `Locations 2025.zip`)
   in `data/raw/Locations/`. `data/raw/` is gitignored: this data is private.
2. Build the comparison (about 10 s per month; the first run also downloads
   all Tartu OSM ways once into `cache/gps_coverage/`):

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
`cache/gps_coverage/{cells,edges,candidates}.geojson` and `summary.json`
(share of ridden distance per class), served at `/api/coverage/{layer}`.

### GPS layer for routing

`cache/gps_coverage/gps_ways.geojson` has one row per OSM way that riders use,
drawn as its ridden part only: `way_id` (OSM way id), `highway`, `name`,
`trips` (median Smart Bike trips in 2025), `ridden_m`, `length_m`,
`covered_share` and `in_bike_graph`. `in_bike_graph=false` means the router's
osmnx `bike` graph does not contain the way at all (mostly `highway=footway`).
Those "gaps" also get `alongside_share` (fraction within 25 m of a way the
router has) and `gap_group`:

| `gap_group` | Meaning | Value for routing |
|---|---|---|
| `separate` | < 50% alongside: park, riverside, courtyard, shortcut | a connection the router lacks |
| `partly` | 50–80% alongside | mixed |
| `alongside` | ≥ 80% alongside, mostly sidewalks next to roads | none; the router can use the road |

The map shows three independently switchable layers: the router's graph
(`router_graph.geojson`, the same `graph_from_place(..., network_type="bike")`
call as `router()`), where riders go (all ridden ways), and the three gap
groups. These layers can be rebuilt from the saved outputs without the raw GPS
data: `python -m backend.gps_coverage --from-cache`.

The layer does not change routing. To use it in the router, join it by OSM way
id with `backend/gps_layer.py`:

```python
from backend.gps_layer import load_gps_ways, edge_gps_trips

gps_ways = load_gps_ways("cache/gps_coverage/gps_ways.geojson")  # once
trips = edge_gps_trips(edge_attributes, gps_ways)                # per edge; None if unridden
```

`edges.geojson` (trips per OSM edge) is still written, for comparison with
earlier runs.

Known limitations: OSM pedestrian squares mapped as areas are not ways, so
riding across them shows up as `no_osm_way`; station surroundings may too.
Candidates should be checked on the ground (or on imagery) before anything is
edited in OSM. Do not upload the traces themselves to OSM.

Tests: `python -m pytest`.
