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

The route search uses exact Dijkstra shortest paths. It first finds the
shortest viable route by physical length, then prefers the
dedicated-cycleway-heavy alternative when it is no more than 20% longer than
that baseline. Within that constraint it also penalises major roads,
cobblestones, elevation, and unnecessary facility transitions. The detour
limit is configurable through `RoutingPreferences.maximum_detour_factor`.
This is a constrained preference search, rather than an exhaustive
enumeration of every possible route. Better traffic and GPS-derived
preferences require additional data.

Routing factors must remain non-negative because Dijkstra's algorithm requires
non-negative edge costs. A factor of `0.05` makes a dedicated cycleway twenty
times more attractive than an ordinary road of the same length without
introducing invalid negative weights. Negative factors can create negative
cycles and invalidate shortest-path results.
