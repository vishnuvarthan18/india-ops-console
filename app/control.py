"""Console-side client for the ops-control service.

The console never runs a command. It asks the control service, over
127.0.0.1, with a shared token, and the control service decides — against its
own allowlist file, which the console cannot read or write.

Every call is recorded in ops_console.control_action before it is attempted,
so an action that kills the control service still leaves evidence that it was
asked for.
"""

import json
import logging
import urllib.error
import urllib.request

from app.db import cursor
from app.settings import get_settings

logger = logging.getLogger(__name__)

TIMEOUT = 30


class ControlUnavailable(RuntimeError):
    pass


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    s = get_settings()
    if not s.control_token:
        raise ControlUnavailable("CONTROL_TOKEN is not configured in the console's .env")

    url = f"{s.control_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Control-Token", s.control_token)
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}") | {"_status": exc.code}
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {exc.code}", "_status": exc.code}
    except urllib.error.URLError as exc:
        raise ControlUnavailable(
            f"control service unreachable at {s.control_url}: {exc.reason}"
        ) from exc


def _audit(action: str, target: str) -> int:
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO ops_console.control_action (action, target) VALUES (%s, %s) RETURNING id",
            (action, target),
        )
        return cur.fetchone()["id"]


def _finish(audit_id: int, ok: bool, detail: str) -> None:
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE ops_console.control_action SET finished_at = now(), ok = %s, detail = %s "
            "WHERE id = %s",
            (ok, (detail or "")[:4000], audit_id),
        )


def available() -> bool:
    try:
        _request("GET", "/units")
        return True
    except Exception:  # noqa: BLE001
        return False


def list_units() -> list[str]:
    try:
        return _request("GET", "/units").get("units", [])
    except ControlUnavailable:
        return []


def unit_status(unit: str) -> dict:
    return _request("GET", f"/status/{unit}")


def unit_logs(unit: str, lines: int = 120) -> str:
    return _request("GET", f"/logs/{unit}?lines={lines}").get("lines", "")


def run_unit(unit: str) -> dict:
    audit_id = _audit("run_job", unit)
    try:
        result = _request("POST", f"/run/{unit}", {})
    except ControlUnavailable as exc:
        _finish(audit_id, False, str(exc))
        raise
    ok = bool(result.get("ok"))
    _finish(audit_id, ok, result.get("detail") or result.get("error") or "")
    return result


def rotate_key(env_var: str, value: str, restart_unit: str | None = None) -> dict:
    """The value is passed through and never logged or stored — not in the
    audit row, not in the console's logs."""
    audit_id = _audit("rotate_key", env_var)
    payload: dict = {"value": value}
    if restart_unit:
        payload["restart_unit"] = restart_unit
    try:
        result = _request("POST", f"/rotate/{env_var}", payload)
    except ControlUnavailable as exc:
        _finish(audit_id, False, str(exc))
        raise
    ok = bool(result.get("ok"))
    detail = f"{result.get('detail', '')}; restart: {result.get('restarted') or 'not requested'}"
    _finish(audit_id, ok, detail if ok else (result.get("error") or detail))
    return result


def recent_actions(limit: int = 30) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT id, action, target, requested_at, finished_at, ok, detail "
            "FROM ops_console.control_action ORDER BY requested_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()
