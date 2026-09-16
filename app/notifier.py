"""Entry point for the alert notifier timer.

Runs inside the console's own container (it needs the same database
credentials and settings), started on a timer by the host:

    docker compose run --rm ops-console python -m app.notifier

Exits 0 even when a delivery fails. A notifier that exits non-zero because a
webhook was down would itself become a failed systemd unit — an alerting
system generating the alerts, which is precisely the noise loop the rest of
this work was about removing. Delivery problems are logged instead.
"""

import logging
import sys

from app import alerting
from app.db import close_pool, open_pool
from app.settings import get_settings


def main() -> int:
    s = get_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("notifier")

    open_pool()
    try:
        transitions = alerting.sync_episodes()
        opened, resolved = transitions["opened"], transitions["resolved"]
        log.info("alert sync: %d opened, %d resolved, %d open in total",
                 len(opened), len(resolved), transitions["still_open"])
        if opened or resolved:
            alerting.notify(transitions)
        else:
            log.info("no transitions — nothing to notify")
    except Exception:  # noqa: BLE001
        log.exception("alert sync failed")
        return 1
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
