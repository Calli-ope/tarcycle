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
