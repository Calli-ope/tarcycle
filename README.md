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

The current route is a custom shortest path, not a guaranteed globally
optimal cycling route. It prefers dedicated cycleways, cycleway tags on roads,
paths, and tracks, while penalising major roads and cobblestones. Better
elevation, traffic, and GPS-derived preferences require additional data.
NetworkX A* is used instead of an uninformed shortest-path search; the
heuristic is a conservative geographic lower bound, so it improves search
speed without changing the weighted-route objective.
