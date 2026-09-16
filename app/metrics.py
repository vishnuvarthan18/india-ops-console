"""Section 6 — reading the host metrics JSON.

The console cannot see the host it runs on: it is a container, so /proc and
`df` inside it describe the container, not the VPS. Rather than give the web
app a Docker socket or a shell (which the plan explicitly forbids), a small
shell script runs on the host under its own systemd timer, writes a JSON
snapshot to a directory, and the console reads that file read-only.

The console therefore has no privileged access at all, and the worst a bug
here can do is display a stale number.
"""

import json
import logging
from pathlib import Path

from app.settings import get_settings

logger = logging.getLogger(__name__)


def read_snapshot() -> dict:
    path = Path(get_settings().metrics_file)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"available": False, "error": "metrics file not found — is the ops-metrics timer installed and running on the host?"}
    except OSError as exc:
        return {"available": False, "error": f"could not read metrics file: {exc}"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A half-written file is a normal race, not an incident. Say so plainly
        # instead of showing a stack trace.
        return {"available": False, "error": f"metrics file is not valid JSON (likely mid-write): {exc}"}

    data["available"] = True
    return data


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:,.1f} PB"
