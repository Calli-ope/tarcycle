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
- Disk: about 4 MB per day (compressed raw responses + parsed rows).

Without compose:

```bash
docker build -t tartu-bike-poller poller/
docker run -d --name tartu-bike-poller --restart unless-stopped \
    -v poller-data:/data tartu-bike-poller
```

Settings (environment variables): `POLL_INTERVAL_SECONDS` (default 300),
`DB_PATH` (default `/data/station_status.sqlite3` in Docker), `API_URL`.

## Deploy (Kubernetes CronJob)

For a cluster (here: a friend's homelab). `poller/k8s/` holds:

| File | What it does |
|---|---|
| `cronjob.yaml` | Every 5 min (UTC) starts a container that runs `python -m poller.once`: one poll (with one retry), saved to SQLite, then exit. Exit status 1 marks the run as failed. Runs as UID 65532, read-only filesystem. |
| `pvc.yaml` | 2 GiB Longhorn volume for `/data/station_status.sqlite3` (~4 MB/day). |
| `network-policy.yaml` | Cilium egress rule: only `api.ratas.tartu.ee:443`. |
| `data-access.yaml` | Temporary pod for copying the database out (commands in the file). |

The image is built and pushed to `ghcr.io/calli-ope/tarcycle-poller` by
`.github/workflows/poller-image.yml` on every push that changes `poller/`.
The workflow log prints the digest to pin in `cronjob.yaml`.

```bash
kubectl create namespace tarcycle        # if the cluster does not create it
kubectl apply -f poller/k8s/pvc.yaml -f poller/k8s/network-policy.yaml -f poller/k8s/cronjob.yaml
kubectl -n tarcycle get cronjob,jobs     # a job every 5 min, last success time
kubectl -n tarcycle logs job/<job-name>  # "Saved 110 stations at ..."
```

No Docker health check here: the CronJob's job history is the health signal
(failed jobs are kept, 3 at most).

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

| Column | API field | Meaning |
|---|---|---|
| `polled_at` | | UTC time of the request, ISO 8601 |
| `station_id`, `station_number`, `station_name` | `id`, `serialNumber`, `name` | station |
| `bikes_total` | `totalLockedCycleCount` | bikes at the station |
| `bikes_docked` | `primaryLockedCycleCount` | bikes locked in physical docks |
| `bikes_temp_lock` | `secondaryLockedCycleCount` | bikes with a temporary lock in the station area |
| `free_docks` | `freeDocksCount` | free physical docks (total docks ≈ `bikes_docked + free_docks`) |
| `free_spaces` | `freeSpacesCount` | free virtual spaces in the station area |
| `stocking_status` | `stationStockingStatus` | EMPTY / LOW / NORMAL / FULL / OVERFULL |
| `station_status` | `stationStatus` | OPEN / CLOSED / PLANNED |
| `locking_type` | `lockingStationType` | PHYSICAL (101) / VIRTUAL (9) |
| `last_update_at` | `lastUpdateCycleCountAt` | last lock, unlock or manual update (UTC) |
| `lat`, `lon` | `areaCentroid` | station centre |

`polls`: one row per request: `http_status`, `error` (`NULL` on success) and
`raw_gzip`, the full response. Fields not parsed (e.g. `cyclesInStation`,
`area`) are kept there and can be extracted later.

## API notes

- Official public endpoint (from Tartu Smart Bike):
  `POST https://api.ratas.tartu.ee/cxf/am/station/map/search` with JSON body
  `{"start": 0, "limit": 500}`. A GET returns 405; an empty or other body
  returns "Invalid request". If `total` ever exceeds the limit, the poll is
  recorded as an error.
- Field meanings: "RR2021 Tartu - Bike share public API explanation" (2021,
  Estonian). `fullCycleStockingCount` (total docks) from that document is no
  longer in the response.
- `cyclesInStation` lists bikes only for a few virtual stations, not per dock.

## Run locally (without Docker)

```bash
python -m poller.run            # writes cache/poller/station_status.sqlite3
python -m poller.health         # exit code 0 = a poll succeeded recently
python -m pytest tests/test_poller.py
```
