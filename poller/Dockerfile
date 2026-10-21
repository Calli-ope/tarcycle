# Station-status poller image. Build from the repo root:
#   docker build -t tartu-bike-poller poller/

# Small official Python image; the poller needs only the standard library.
FROM python:3.12-slim

# Print logs immediately (visible with `docker logs`) and set defaults that
# can be overridden with -e / compose `environment:`.
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/station_status.sqlite3 \
    POLL_INTERVAL_SECONDS=300

# Run as an unprivileged user that owns only the data folder.
RUN useradd --create-home poller && mkdir /data && chown poller /data

WORKDIR /app
COPY --chown=poller . /app/poller/
USER poller

# The SQLite file lives here; mount a volume so data survives container rebuilds.
VOLUME /data

# Docker marks the container "unhealthy" if no poll succeeded in the last 15 min.
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m \
    CMD python -m poller.health || exit 1

CMD ["python", "-m", "poller.run"]
