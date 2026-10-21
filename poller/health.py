"""Docker health check: healthy if a poll succeeded in the last 3 intervals.

Exit code 0 = healthy, 1 = unhealthy (``docker ps`` shows the result).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .core import Settings, connect, last_success_age_seconds


def main() -> int:
    settings = Settings.from_env()
    connection = connect(settings.db_path)
    age = last_success_age_seconds(connection, datetime.now(timezone.utc))
    connection.close()
    if age is None or age > 3 * settings.interval_seconds:
        print(f"unhealthy: last successful poll {age} s ago")
        return 1
    print(f"healthy: last successful poll {age:.0f} s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
