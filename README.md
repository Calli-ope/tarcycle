# Tarcycle

Tarcycle is a small Tartu bicycle-routing prototype. The backend uses OSMnx
and NetworkX behind a routing interface that can later be implemented with
Valhalla. The MapLibre GL JS frontend displays bike-rack stations from
`Rattaparklad.csv` and routes between two selected stations.

## Run

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the API from the project root:

```powershell
python -m backend
```

Serve `frontend/` with any static file server, then open its URL:

```powershell
python -m http.server 5173 --directory frontend
```

The first route request downloads and caches the Tartu bicycle graph in
`cache/tartu_bike.graphml`.

The current route is a custom shortest path, not a guaranteed globally
optimal cycling route. It prefers dedicated cycleways, cycleway tags on roads,
paths, and tracks, while penalising major roads and cobblestones. Better
elevation, traffic, and GPS-derived preferences require additional data.
