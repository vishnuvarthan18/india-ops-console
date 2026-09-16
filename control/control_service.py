#!/usr/bin/env python3
"""ops-control — the privileged half of the ops console.

Runs ON THE HOST as a systemd unit, not in a container, because its whole job
is to talk to systemd. Listens on 127.0.0.1 only. The console calls it with a
shared token.

The plan's rule is "the web app should never get a raw shell/exec endpoint".
This service is how that rule survives contact with a feature that genuinely
needs to start jobs. Five properties make it safe:

  1. **Allowlist, not validation.** A unit name is looked up in a file of
     permitted names. Anything not literally present is refused. There is no
     pattern, no prefix rule, and no escaping — an attacker who controls the
     request string still cannot name a unit that is not on the list.
  2. **No shell anywhere.** Every subprocess call passes an argument list, with
     shell=False. There is no string a request can influence that is ever
     parsed by a shell.
  3. **Fixed verbs.** It can start a unit, read a unit's status, and tail a
     unit's journal. It cannot stop, disable, mask, edit, or run anything else.
  4. **Bound to localhost.** Not reachable from outside the VPS at all.
  5. **Constant-time token compare**, so the token cannot be discovered by
     timing.

Key rotation is the one write: it replaces a single line in the secrets file
(mode 0600) and restarts one allowlisted service. The variable name must be on
its own allowlist, and the value is never logged.
"""

import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger("ops-control")

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("CONTROL_PORT", "8011"))
TOKEN = os.environ.get("CONTROL_TOKEN", "")
ALLOWLIST_FILE = os.environ.get("CONTROL_ALLOWLIST", "/etc/ops-control/units.allow")
KEYVAR_FILE = os.environ.get("CONTROL_KEYVARS", "/etc/ops-control/keyvars.allow")
SECRETS_FILE = os.environ.get("CONTROL_SECRETS_FILE", "/home/ubuntu/core-infra/.env")

# A unit name must look like a unit name before it is even compared to the
# allowlist. Belt and braces: the allowlist is the real control.
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.\-]{1,96}\.service$")
ENVVAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _read_allowlist(path: str) -> set[str]:
    try:
        return {
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    except OSError:
        LOG.error("allowlist %s unreadable — refusing everything", path)
        return set()


def run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """Every subprocess in this service goes through here: argument list,
    shell=False, bounded time, output capped."""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, shell=False, check=False)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-20000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        LOG.exception("subprocess failed")
        return 1, str(exc)


def rotate_secret(env_var: str, new_value: str) -> tuple[bool, str]:
    """Replace one KEY=value line in the secrets file, atomically, 0600.

    The previous file is kept as .bak so a bad rotation is recoverable without
    a backup restore. The value is never logged, here or anywhere.
    """
    path = Path(SECRETS_FILE)
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        return False, f"cannot read secrets file: {exc}"

    lines = original.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == env_var:
            lines[i] = f"{env_var}={new_value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_var}={new_value}")

    body = "\n".join(lines) + "\n"
    try:
        # Same directory, so the rename is atomic on the same filesystem.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        if original:
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(original, encoding="utf-8")
            os.chmod(backup, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"cannot write secrets file: {exc}"

    return True, ("updated" if replaced else "appended (variable was not present)")


class Handler(BaseHTTPRequestHandler):
    server_version = "ops-control"

    def log_message(self, fmt, *args):  # quieter, and to the journal
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Control-Token", "")
        if not TOKEN or not hmac.compare_digest(supplied, TOKEN):
            LOG.warning("rejected request with bad or missing token")
            self._send(401, {"error": "bad token"})
            return False
        return True

    # ---- read endpoints ---------------------------------------------------
    def do_GET(self):  # noqa: N802
        if not self._authed():
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]

        if parts == ["units"]:
            return self._send(200, {"units": sorted(_read_allowlist(ALLOWLIST_FILE))})

        if len(parts) == 2 and parts[0] in ("status", "logs"):
            unit = parts[1]
            if not UNIT_RE.match(unit) or unit not in _read_allowlist(ALLOWLIST_FILE):
                LOG.warning("refused %s for non-allowlisted unit %r", parts[0], unit[:80])
                return self._send(403, {"error": "unit not allowlisted"})

            if parts[0] == "status":
                rc, out = run(["systemctl", "status", unit, "--no-pager", "-n", "5"])
                active, _ = run(["systemctl", "is-active", unit])
                failed, _ = run(["systemctl", "is-failed", unit])
                return self._send(200, {"unit": unit, "text": out,
                                        "active": active == 0, "failed": failed == 0})

            try:
                n = min(500, max(10, int(qs.get("lines", ["120"])[0])))
            except ValueError:
                n = 120
            rc, out = run(["journalctl", "-u", unit, "-n", str(n), "--no-pager"])
            return self._send(200, {"unit": unit, "lines": out})

        self._send(404, {"error": "no such endpoint"})

    # ---- action endpoints -------------------------------------------------
    def do_POST(self):  # noqa: N802
        if not self._authed():
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "body must be JSON"})

        # POST /run/<unit>
        if len(parts) == 2 and parts[0] == "run":
            unit = parts[1]
            if not UNIT_RE.match(unit) or unit not in _read_allowlist(ALLOWLIST_FILE):
                LOG.warning("refused run for non-allowlisted unit %r", unit[:80])
                return self._send(403, {"error": "unit not allowlisted"})
            # --no-block: a harvest can run for minutes; the console polls
            # status rather than holding an HTTP request open.
            rc, out = run(["systemctl", "start", "--no-block", unit], timeout=30)
            LOG.info("started %s rc=%s", unit, rc)
            return self._send(200 if rc == 0 else 500,
                              {"unit": unit, "ok": rc == 0, "detail": out.strip()[:2000]})

        # POST /rotate/<ENV_VAR>
        if len(parts) == 2 and parts[0] == "rotate":
            env_var = parts[1]
            if not ENVVAR_RE.match(env_var) or env_var not in _read_allowlist(KEYVAR_FILE):
                LOG.warning("refused rotate for non-allowlisted var %r", env_var[:80])
                return self._send(403, {"error": "variable not allowlisted"})
            value = body.get("value") or ""
            if not isinstance(value, str) or not (8 <= len(value) <= 4096):
                return self._send(422, {"error": "value must be a string of 8–4096 chars"})

            ok, detail = rotate_secret(env_var, value)
            LOG.info("rotated %s ok=%s (%s)", env_var, ok, detail)  # value never logged
            if not ok:
                return self._send(500, {"ok": False, "detail": detail})

            restarted = None
            unit = body.get("restart_unit")
            if unit:
                if not UNIT_RE.match(str(unit)) or unit not in _read_allowlist(ALLOWLIST_FILE):
                    return self._send(200, {"ok": True, "detail": detail,
                                            "restarted": "refused: unit not allowlisted"})
                rc, out = run(["systemctl", "restart", unit], timeout=120)
                restarted = f"{unit}: {'ok' if rc == 0 else out.strip()[:500]}"
            return self._send(200, {"ok": True, "detail": detail, "restarted": restarted})

        self._send(404, {"error": "no such endpoint"})


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not TOKEN:
        LOG.error("CONTROL_TOKEN is not set — refusing to start. "
                  "A control service with no token is an open shell.")
        return 2
    if len(TOKEN) < 32:
        LOG.error("CONTROL_TOKEN is shorter than 32 characters — refusing to start.")
        return 2

    units = _read_allowlist(ALLOWLIST_FILE)
    LOG.info("ops-control listening on %s:%s, %d allowlisted unit(s)",
             LISTEN_HOST, LISTEN_PORT, len(units))
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
