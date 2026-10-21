"""Poll the station-status API forever: ``python -m poller.run``."""

from __future__ import annotations

import logging
import signal
import threading
import time

from .core import Settings, connect, poll_once, seconds_until_next_poll

log = logging.getLogger("poller")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    connection = connect(settings.db_path)
    stop = threading.Event()
    # `docker stop` sends SIGTERM: finish cleanly instead of being killed mid-write.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    log.info("Polling %s every %ss into %s", settings.api_url, settings.interval_seconds, settings.db_path)
    while not stop.is_set():
        try:
            poll_once(connection, settings, sleep=stop.wait)
        except Exception:  # never let one bad poll end the loop
            log.exception("Unexpected error during poll")
        stop.wait(seconds_until_next_poll(time.time(), settings.interval_seconds))
    connection.close()
    log.info("Stopped")


if __name__ == "__main__":
    main()
