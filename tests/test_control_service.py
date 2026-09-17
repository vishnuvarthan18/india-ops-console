"""The privileged service — the highest-consequence code in the repo.

It runs as root and starts systemd units, so it gets the most hostile tests.
None of these need a database.

The claim under test is narrow and total: a unit name that is not *literally* a
line in the allowlist file can never reach systemctl, whatever the request
contains. Not "is escaped correctly", not "is validated" — never reaches it.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest


def call(svc, method, path, body=None, token=None, raw_path=False):
    url = svc["base"] + (path if raw_path else path)
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    tok = svc["token"] if token is None else token
    if tok is not False:
        req.add_header("X-Control-Token", tok)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


# --- authentication --------------------------------------------------------

def test_no_token_is_refused(control_service):
    assert call(control_service, "GET", "/units", token=False)[0] == 401


def test_wrong_token_is_refused(control_service):
    assert call(control_service, "GET", "/units", token="x" * 64)[0] == 401


def test_correct_token_works(control_service):
    status, body = call(control_service, "GET", "/units")
    assert status == 200
    assert "pa-harvest.service" in body["units"]


def test_comments_are_not_treated_as_units(control_service):
    """A '#' line in the allowlist must not become a runnable unit name."""
    units = call(control_service, "GET", "/units")[1]["units"]
    assert not any(u.startswith("#") for u in units)
    assert len(units) == 2


# --- the allowlist ---------------------------------------------------------

HOSTILE = [
    pytest.param("pa-harvest.service;rm -rf /", id="shell-semicolon"),
    pytest.param("pa-harvest.service && id", id="shell-and"),
    pytest.param("pa-harvest.service | id", id="shell-pipe"),
    pytest.param("$(id).service", id="command-substitution"),
    pytest.param("`id`.service", id="backticks"),
    pytest.param("../../etc/passwd", id="path-traversal"),
    pytest.param("/etc/passwd", id="absolute-path"),
    pytest.param("pa-harvest.service\nssh.service", id="newline-injection"),
    pytest.param("pa-harvest.service\x00docker.service", id="null-byte"),
    pytest.param("PA-HARVEST.SERVICE", id="uppercase"),
    pytest.param("Pa-Harvest.Service", id="mixed-case"),
    pytest.param("pa-harvest.service ", id="trailing-space"),
    pytest.param(" pa-harvest.service", id="leading-space"),
    pytest.param("pa-harvest.service\t", id="trailing-tab"),
    pytest.param("*.service", id="wildcard"),
    pytest.param("pa-harvest.*", id="wildcard-suffix"),
    pytest.param("pa-harvest.timer", id="timer-not-service"),
    pytest.param("pa-harvest.socket", id="socket-not-service"),
    pytest.param("docker.service", id="docker"),
    pytest.param("ssh.service", id="ssh"),
    pytest.param("sshd.service", id="sshd"),
    pytest.param("systemd-journald.service", id="journald"),
    pytest.param("pa-harvest.service/../../ssh.service", id="traversal-through-allowed"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("unit", HOSTILE)
def test_hostile_unit_names_cannot_run(control_service, unit):
    status, _ = call(control_service, "POST", "/run/" + quote(unit, safe=""), {})
    assert status in (403, 404), f"{unit!r} was not refused (got {status})"


@pytest.mark.parametrize("unit", HOSTILE)
def test_hostile_unit_names_cannot_be_inspected(control_service, unit):
    for verb in ("status", "logs"):
        status, _ = call(control_service, "GET", f"/{verb}/" + quote(unit, safe=""))
        assert status in (403, 404), f"{verb} {unit!r} was not refused (got {status})"


def test_allowlisted_unit_is_allowed(control_service):
    assert call(control_service, "POST", "/run/pa-harvest.service", {})[0] == 200
    assert call(control_service, "GET", "/status/pa-harvest.service")[0] == 200
    assert call(control_service, "GET", "/logs/pa-harvest.service?lines=50")[0] == 200


def test_log_line_count_cannot_be_abused(control_service):
    """Absurd and non-numeric line counts are clamped, not passed through."""
    for q in ("999999999", "-1", "abc", "1e9", "'; id"):
        status, _ = call(control_service, "GET",
                         f"/logs/pa-harvest.service?lines={quote(q, safe='')}")
        assert status == 200


# --- key rotation ----------------------------------------------------------

PROTECTED = ["POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD", "SESSION_SECRET",
             "CONTROL_TOKEN", "PATH", "LD_PRELOAD"]


@pytest.mark.parametrize("var", PROTECTED)
def test_protected_variables_cannot_be_rotated(control_service, var):
    """Rotating these from a web form would let one compromised session take
    the whole platform apart. They are excluded on purpose."""
    status, _ = call(control_service, "POST", f"/rotate/{var}",
                     {"value": "attacker-supplied-value"})
    assert status == 403


def test_protected_variable_is_untouched_on_disk(control_service):
    call(control_service, "POST", "/rotate/POSTGRES_PASSWORD", {"value": "hijacked-value"})
    assert "POSTGRES_PASSWORD=must-not-change" in control_service["secrets_file"].read_text()


def test_allowlisted_variable_rotates(control_service):
    status, body = call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY",
                        {"value": "a-fresh-key-value"})
    assert status == 200 and body["ok"]
    assert "DATA_GOV_IN_API_KEY=a-fresh-key-value" in control_service["secrets_file"].read_text()


def test_rotation_keeps_a_backup(control_service):
    call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY", {"value": "another-value-here"})
    assert control_service["secrets_file"].with_suffix(".env.bak").exists()


def test_secrets_file_stays_private(control_service):
    call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY", {"value": "yet-another-value"})
    mode = control_service["secrets_file"].stat().st_mode & 0o777
    assert mode == 0o600, f"secrets file is {oct(mode)}, must be 0600"


@pytest.mark.parametrize("value", ["", "short", 12345, None, ["a"], {"k": "v"}])
def test_bad_rotation_values_are_refused(control_service, value):
    status, _ = call(control_service, "POST", "/rotate/WDPA_API_KEY", {"value": value})
    assert status == 422


def test_rotation_cannot_restart_an_unlisted_unit(control_service):
    _, body = call(control_service, "POST", "/rotate/WDPA_API_KEY",
                   {"value": "a-valid-length-value", "restart_unit": "docker.service"})
    assert str(body.get("restarted", "")).startswith("refused")


def test_key_values_never_reach_the_log(control_service):
    """A secret in a log file is a secret on disk, in backups, and in anything
    that ships logs elsewhere."""
    marker = "supersecret-canary-value-12345"
    call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY", {"value": marker})
    assert marker not in control_service["log"].read_text()


# --- shape -----------------------------------------------------------------

def test_unknown_endpoints_404(control_service):
    assert call(control_service, "GET", "/whatever")[0] == 404
    assert call(control_service, "POST", "/stop/pa-harvest.service", {})[0] == 404


def test_there_is_no_stop_or_disable_verb(control_service):
    """The service can start a unit, never stop, disable or mask one."""
    for verb in ("stop", "disable", "mask", "kill", "exec", "shell"):
        status, _ = call(control_service, "POST", f"/{verb}/pa-harvest.service", {})
        assert status == 404


def test_malformed_body_is_handled(control_service):
    req = urllib.request.Request(control_service["base"] + "/run/pa-harvest.service",
                                 data=b"not json at all", method="POST")
    req.add_header("X-Control-Token", control_service["token"])
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status in (200, 400)
    except urllib.error.HTTPError as e:
        assert e.code == 400


# --- hardening added after the first security review -----------------------

def test_newline_in_a_rotation_value_is_refused(control_service):
    """The secrets file is KEY=value per line. A newline in the value would let
    one rotation define a second variable — including one this service refuses
    to rotate directly. That is a privilege escalation, not a formatting bug."""
    status, _ = call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY",
                     {"value": "legit-value\nPOSTGRES_PASSWORD=owned"})
    assert status == 422
    assert "POSTGRES_PASSWORD=owned" not in control_service["secrets_file"].read_text()
    assert "POSTGRES_PASSWORD=must-not-change" in control_service["secrets_file"].read_text()


def test_carriage_return_in_a_rotation_value_is_refused(control_service):
    status, _ = call(control_service, "POST", "/rotate/DATA_GOV_IN_API_KEY",
                     {"value": "legit-value\rMINIO_ROOT_PASSWORD=owned"})
    assert status == 422
    assert "MINIO_ROOT_PASSWORD=owned" not in control_service["secrets_file"].read_text()


def test_an_oversized_body_is_refused(control_service):
    import urllib.error
    import urllib.request
    big = json.dumps({"value": "x" * (200 * 1024)}).encode()
    req = urllib.request.Request(control_service["base"] + "/rotate/DATA_GOV_IN_API_KEY",
                                 data=big, method="POST")
    req.add_header("X-Control-Token", control_service["token"])
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status in (413, 422)
    except urllib.error.HTTPError as e:
        assert e.code in (413, 422)


def test_an_over_long_path_is_refused(control_service):
    status, _ = call(control_service, "GET", "/status/" + ("a" * 2000))
    assert status in (403, 414)


def test_log_lines_cannot_be_forged(control_service):
    """A refused unit name is echoed into the journal. If control characters
    survived that, an attacker could write convincing fake lines into the log
    of the very service that refused them."""
    call(control_service, "POST",
         "/run/" + quote("evil\nops-control INFO started ssh.service", safe=""), {})
    log = control_service["log"].read_text()
    assert "INFO started ssh.service" not in log


def test_the_allowlist_is_reread_so_it_can_be_corrected_live(control_service):
    """Removing a unit from the file must take effect without a restart —
    otherwise revoking access means downtime, and revocation gets postponed."""
    allow = control_service["dir"] / "units.allow"
    original = allow.read_text()
    try:
        allow.write_text("culture-fra-jk.service\n")
        assert call(control_service, "POST", "/run/pa-harvest.service", {})[0] == 403
    finally:
        allow.write_text(original)
    assert call(control_service, "POST", "/run/pa-harvest.service", {})[0] == 200
