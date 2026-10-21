# Station-status poller

Builds our own history of Tartu Smart Bike station status (the public API is
live-only). Every 5 minutes it fetches all stations and stores bikes and free
docks per station in a SQLite database. The data is for station demand
prediction (empty-station risk).

## Deploy (Docker)

From the repo root:

```bash
docker compose -f poller/compose.yaml up -d --build
```

- `restart: unless-stopped` restarts it after crashes and server reboots.
- Data lives in the named volume `poller-data` (`/data/station_status.sqlite3`
  in the container) and survives rebuilds and `docker compose down`.
  Only `docker compose down -v` deletes it.
- A health check marks the container `unhealthy` if no poll succeeded in the
  last 15 minutes: `docker ps` shows the status, `docker logs <container>`
  shows every poll.
- Disk: about 3 MB per day (compressed raw responses + parsed rows).

Without compose:

```bash
docker build -t tartu-bike-poller poller/
docker run -d --name tartu-bike-poller --restart unless-stopped \
    -v poller-data:/data tartu-bike-poller
```

Settings (environment variables): `POLL_INTERVAL_SECONDS` (default 300),
`DB_PATH` (default `/data/station_status.sqlite3` in Docker), `API_URL`.

## Getting the data out

```bash
# Copy the database out of the running container (safe while it runs):
docker compose -f poller/compose.yaml exec poller \
    python -c "import sqlite3; sqlite3.connect('/data/station_status.sqlite3').backup(sqlite3.connect('/data/backup.sqlite3'))"
docker compose -f poller/compose.yaml cp poller:/data/backup.sqlite3 ./station_status.sqlite3

# Or a CSV:
python -m poller.export --db station_status.sqlite3 --out status.csv --since 2026-10-21
```

## Data

`station_status`: one row per station per successful poll.

| Column | Meaning |
|---|---|
| `polled_at` | UTC time of the request, ISO 8601 |
| `station_id`, `station_number`, `station_name` | station |
| `bikes_available` | bikes at the station |
| `docks_available` | **free** docks (total docks are not in the API; ≈ bikes + free docks) |
| `stock_level` | API's own label: empty / low / normal / full / overfull |
| `status` | open / … |
| `is_physical`, `is_dockless` | 8 of 109 stations are virtual (dockless) |
| `lat`, `lon` | station position |

`polls`: one row per request: `http_status`, `error` (`NULL` on success) and
`raw_gzip`, the full response. If parsing ever turns out to be wrong, the raw
responses can be parsed again.

## API notes

- Endpoint: `GET https://serverapp.ratas.tartu.ee/api/map/stations/`, the one
  the official map (ratas.tartu.ee) uses. It answers only with the header
  `Origin: https://ratas.tartu.ee`; without it, HTTP 500.
- The older `https://api.ratas.tartu.ee/cxf/am/station/map/search` endpoint is
  POST-only and rejects requests without the right body.
- Not polled yet: `GET .../api/map/station/{station_id}/cycles` lists each bike
  at a station (serial number, type, battery). That would show rebalancing
  directly, but costs one request per station (109 per poll).

## Run locally (without Docker)

```bash
python -m poller.run            # writes cache/poller/station_status.sqlite3
python -m poller.health         # exit code 0 = a poll succeeded recently
python -m pytest tests/test_poller.py
```
