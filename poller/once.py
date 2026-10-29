"""Poll the station-status API once and exit: ``python -m poller.once``.

For schedulers that start the poller every 5 minutes themselves (a
Kubernetes CronJob, cron). Exits with status 1 if the poll failed (after its
one retry), so the scheduler records the run as failed.
"""

from __future__ import annotations

import logging
import sys

from .core import Settings, connect, poll_once


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    connection = connect(settings.db_path)
    try:
        return 0 if poll_once(connection, settings) else 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
