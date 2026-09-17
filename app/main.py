"""india-ops-console — the private admin web app for the India data platform.

Read-only phase (plan sections 1, 2, 6, 8, 9). There is deliberately no
endpoint here that runs a command, touches a secret, or writes to any engine
table. Adding run-triggering later means adding a separate privileged control
service, not widening this one.
"""

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import alerting, browse, control, decisions, icons, keys, metrics, queries
from app.auth import (
    EPOCH_KEY,
    SESSION_KEY,
    check_login,
    clear_failures,
    client_key,
    lockout_remaining,
    record_failure,
    require_admin,
    session_is_current,
)
from app.db import close_pool, execute, open_pool
from app.settings import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ops-console")


@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    yield
    close_pool()


app = FastAPI(title="india-ops-console", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=settings.session_max_age_hours * 3600,
    same_site="lax",
    https_only=False,  # reached over an SSH tunnel, not public TLS
)

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["bytes"] = metrics.human_bytes
templates.env.globals["icon"] = icons.icon


def _nav_counts() -> dict:
    """The two numbers the sidebar carries. Kept cheap and failure-tolerant:
    a badly-timed database hiccup should not take out every page's chrome, so
    a failure here shows no badge rather than a 500."""
    try:
        c = queries.alert_counts()
        pending = sum(1 for k in keys.registry() if k["status"] == "pending")
        return {
            "nav_alerts": (c.get("heartbeat_alerts") or 0) + (c.get("empty_successful_runs") or 0),
            "nav_keys_pending": pending,
        }
    except Exception:  # noqa: BLE001
        logger.exception("nav counts unavailable")
        return {"nav_alerts": 0, "nav_keys_pending": 0}
app.mount("/static", StaticFiles(directory="app/static"), name="static")

ADMIN = Depends(require_admin)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    """Every template gets `signed_in` from one place, so the chrome a visitor
    sees can never disagree with what the server will actually serve them."""
    current = session_is_current(request)
    if current:
        ctx = {**_nav_counts(), **ctx}
    return templates.TemplateResponse(request, name, {"signed_in": current, **ctx})


HTTP_MESSAGES = {
    403: ("Not permitted",
          "That action is not on the control service's allowlist. This is a "
          "deliberate limit, not a fault — see control/units.allow."),
    404: ("Not found", "There is nothing at that address."),
    413: ("Too large", "That request was larger than this console accepts."),
    422: ("That input was refused",
          "One of the values submitted was not one this console accepts."),
    503: ("A service is unavailable",
          "The control service could not be reached. The rest of the console "
          "is read-only but still working."),
}


def _wants_json(request: Request) -> bool:
    """JSON endpoints get JSON errors; pages get pages."""
    return (request.url.path.endswith((".json", ".geojson", ".csv"))
            or request.url.path == "/healthz")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """An unauthenticated page request goes to the login form rather than a
    bare 401 body — this is a browser app, not an API.

    Keyed on the request method rather than the Accept header: some browsers
    and most tools send a vague or absent Accept, and a signed-out user
    hitting a bookmark should land on the login form, not on a JSON error.
    """
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.method == "GET":
        return RedirectResponse("/login", status_code=302)
    if _wants_json(request) or request.method != "GET":
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    heading, message = HTTP_MESSAGES.get(
        exc.status_code, ("Something went wrong", str(exc.detail)))
    return templates.TemplateResponse(
        request, "error.html",
        {"signed_in": session_is_current(request),
         "code": exc.status_code, "heading": heading,
         "message": exc.detail if isinstance(exc.detail, str) and exc.status_code == 404 else message,
         **(_nav_counts() if session_is_current(request) else {})},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """An unexpected failure should say so in the console's own voice, not
    drop a stack trace or a bare JSON blob in front of the operator.

    The detail goes to the log, never to the page: an error message can carry
    a connection string or a query fragment.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    if _wants_json(request):
        return JSONResponse({"detail": "internal error"}, status_code=500)
    try:
        return templates.TemplateResponse(
            request, "error.html",
            {"signed_in": session_is_current(request),
             "code": 500, "heading": "Something went wrong",
             "message": "The console hit an unexpected error. It has been logged.",
             **(_nav_counts() if session_is_current(request) else {})},
            status_code=500,
        )
    except Exception:  # noqa: BLE001 — the error page itself must never fail
        return JSONResponse({"detail": "internal error"}, status_code=500)


# ---------------------------------------------------------------------------
# Health + auth
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if session_is_current(request):
        return RedirectResponse("/", status_code=302)
    request.session.clear()
    return page(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    key = client_key(request)

    wait = lockout_remaining(key)
    if wait:
        # Refused before the password is even checked, so a locked-out caller
        # learns nothing from how long the response took.
        logger.warning("login refused: %s is locked out for another %ds", key, wait)
        minutes = max(1, (wait + 59) // 60)
        return page(
            request, "login.html",
            error=f"Too many failed attempts. Try again in {minutes} minute"
                  f"{'' if minutes == 1 else 's'}.",
        )

    if check_login(username, password):
        clear_failures(key)
        # A fresh session id on sign-in, so a session cookie captured before
        # login cannot be reused afterwards.
        request.session.clear()
        request.session[SESSION_KEY] = username
        # get_settings(), not the module-level snapshot taken at import:
        # session_is_current() reads it fresh, and the two must never
        # disagree about which epoch is current.
        request.session[EPOCH_KEY] = get_settings().session_epoch
        logger.info("admin login succeeded from %s", key)
        return RedirectResponse("/", status_code=302)

    record_failure(key)
    logger.warning("failed admin login for username=%r from %s", username[:40], key)
    return page(request, "login.html", error="Wrong username or password.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Section 1 — overview
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, dependencies=[ADMIN])
def overview(request: Request):
    counts = queries.alert_counts()
    # Green only when nothing at all is flagged. Anything genuinely broken —
    # a failed job or a silently-empty run — is red; slower-burning problems
    # are amber.
    hard = (counts.get("heartbeat_alerts", 0) or 0) + (counts.get("empty_successful_runs", 0) or 0)
    soft = (counts.get("stale_sources", 0) or 0) + (counts.get("open_rejections", 0) or 0)
    light = "red" if hard else ("amber" if soft else "green")

    return page(
        request, "overview.html",
        totals=queries.totals(),
        counts=counts,
        light=light,
        attention=queries.attention(),
        job_count=queries.job_count(),
        keys_pending=sum(1 for k in keys.registry() if k["status"] == "pending"),
        control_ok=bool(get_settings().control_token),
        engines=queries.engines(),
        runs=queries.recent_runs(12),
        growth=queries.growth_last_30_days(),
    )


@app.get("/alerts", response_class=HTMLResponse, dependencies=[ADMIN])
def alerts_page(request: Request):
    return page(request, "alerts.html",
                alerts=queries.alerts_detail(),
                jobs=queries.jobs(),
                history=alerting.history(60),
                control_units=set(control.list_units()),
                control_ok=bool(get_settings().control_token),
                actions=control.recent_actions(15))


# ---------------------------------------------------------------------------
# Section 3 — data browser and map
# ---------------------------------------------------------------------------

BROWSE_PARAMS = ("engine", "entity_type", "state", "precision", "geom", "q")


def _browse_params(request: Request) -> dict:
    """Only the filters the browser knows about are ever read out of the query
    string; anything else is dropped here rather than reaching browse.py."""
    return {k: request.query_params.get(k, "") for k in BROWSE_PARAMS}


@app.get("/data", response_class=HTMLResponse, dependencies=[ADMIN])
def data_browser(request: Request, page_no: int = 1):
    params = _browse_params(request)
    try:
        page_no = max(1, int(request.query_params.get("page", page_no)))
    except ValueError:
        page_no = 1
    result = browse.search(params, page_no)
    return page(request, "data.html", result=result, facets=browse.facets(),
                params=params, querystring=request.url.query)


@app.get("/data/export.csv", dependencies=[ADMIN])
def data_export(request: Request):
    """CSV of the current filter. Withheld entities are excluded and restricted
    ones lose their coordinates — see browse.export_rows."""
    import csv
    import io

    rows = browse.export_rows(_browse_params(request))
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    else:
        buf.write("no rows matched this filter\n")
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="india-data-export.csv"'},
    )


@app.get("/entity/{uid:path}", response_class=HTMLResponse, dependencies=[ADMIN])
def entity_page(request: Request, uid: str):
    row = browse.entity_detail(uid)
    if row is None:
        raise HTTPException(404, f"no entity with uid {uid!r}")
    # `from` carries the filter the visitor arrived with, so "back to results"
    # returns them to their own search rather than an unfiltered list.
    return page(request, "entity.html", e=row, back=request.query_params.get("from", ""))


@app.get("/map", response_class=HTMLResponse, dependencies=[ADMIN])
def map_page(request: Request):
    return page(request, "map.html", facets=browse.facets(),
                params=_browse_params(request),
                coverage=browse.geometry_coverage(),
                querystring=request.url.query)


@app.get("/map/data.geojson", dependencies=[ADMIN])
def map_data(request: Request):
    return JSONResponse(browse.map_geojson(_browse_params(request)))


# ---------------------------------------------------------------------------
# Section 4 — run control (through the privileged service, never directly)
# ---------------------------------------------------------------------------

@app.post("/jobs/{unit}/run", dependencies=[ADMIN])
def run_job(unit: str):
    try:
        result = control.run_unit(unit)
    except control.ControlUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(result.get("_status", 500),
                            result.get("error") or result.get("detail") or "run failed")
    return RedirectResponse(f"/jobs/{unit}", status_code=302)


@app.get("/jobs/{unit}", response_class=HTMLResponse, dependencies=[ADMIN])
def job_page(request: Request, unit: str, lines: int = 120):
    lines = min(500, max(30, lines))
    try:
        status_info = control.unit_status(unit)
        logs = control.unit_logs(unit, lines)
    except control.ControlUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if status_info.get("error"):
        raise HTTPException(403, status_info["error"])
    return page(request, "job.html", unit=unit, status=status_info, logs=logs, lines=lines)


# ---------------------------------------------------------------------------
# Section 7 — API keys
# ---------------------------------------------------------------------------

@app.get("/keys", response_class=HTMLResponse, dependencies=[ADMIN])
def keys_page(request: Request):
    return page(request, "keys.html",
                rows=keys.registry(),
                orphans=keys.unregistered_sources(),
                rotatable=bool(get_settings().control_token))


@app.post("/keys/{key_id}/status", dependencies=[ADMIN])
def key_status(key_id: int, status_value: str = Form(...)):
    try:
        keys.set_status(key_id, status_value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/keys", status_code=302)


@app.post("/keys/{env_var}/rotate", dependencies=[ADMIN])
def key_rotate(env_var: str, value: str = Form(...), restart_unit: str = Form("")):
    """The new value goes straight to the control service and is never written
    to the database, the session, or the logs."""
    row = keys.by_env_var(env_var)
    if row is None:
        raise HTTPException(404, f"{env_var} is not in the registry")
    try:
        result = control.rotate_key(env_var, value, restart_unit or row.get("service_unit"))
    except control.ControlUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(result.get("_status", 500), result.get("error") or "rotation failed")
    keys.mark_rotated(env_var)
    return RedirectResponse("/keys", status_code=302)


# ---------------------------------------------------------------------------
# Section 2 — engines
# ---------------------------------------------------------------------------

@app.get("/engines", response_class=HTMLResponse, dependencies=[ADMIN])
def engines_page(request: Request):
    return page(request, "engines.html", engines=queries.engines())


@app.get("/engines/{key}", response_class=HTMLResponse, dependencies=[ADMIN])
def engine_detail(request: Request, key: str):
    engine = queries.engine_by_key(key)
    if engine is None:
        raise HTTPException(404, f"no engine with key {key!r}")
    return page(
        request, "engine_detail.html",
        engine=engine,
        sources=queries.engine_sources(engine["id"]),
        entity_types=queries.engine_entity_types(engine["id"]),
        runs=queries.engine_runs(engine["id"]),
    )


# ---------------------------------------------------------------------------
# Section 6 — server & infrastructure
# ---------------------------------------------------------------------------

@app.get("/server", response_class=HTMLResponse, dependencies=[ADMIN])
def server_page(request: Request):
    return page(
        request, "server.html",
        snapshot=metrics.read_snapshot(),
        db=queries.db_size(),
        tables=queries.largest_tables(),
    )


# ---------------------------------------------------------------------------
# Section 8 — decisions log
# ---------------------------------------------------------------------------

@app.get("/decisions", response_class=HTMLResponse, dependencies=[ADMIN])
def decisions_index(request: Request):
    return page(request, "decisions.html", repos=decisions.list_repos(), doc=None, hits=None, q="")


@app.get("/decisions/{repo}", response_class=HTMLResponse, dependencies=[ADMIN])
def decisions_repo(request: Request, repo: str, q: str = ""):
    doc = decisions.read_decisions(repo)
    if doc is None:
        raise HTTPException(404, f"no DECISIONS.md for {repo!r}")
    hits = decisions.search(doc.text, q) if q else None
    return page(request, "decisions.html",
                repos=decisions.list_repos(), doc=doc, hits=hits, q=q)


# ---------------------------------------------------------------------------
# Section 9 — open items
# ---------------------------------------------------------------------------

@app.get("/open-items", response_class=HTMLResponse, dependencies=[ADMIN])
def open_items_page(request: Request):
    return page(request, "open_items.html", items=queries.open_items())


@app.post("/open-items/{item_id}/status", dependencies=[ADMIN])
def set_open_item_status(item_id: int, status_value: str = Form(...)):
    """The one write in the whole app, and it only touches ops_console.open_item —
    a schema the database role has write access to precisely because nothing
    about the platform's data integrity depends on it."""
    if status_value not in ("open", "in_progress", "done", "dropped"):
        raise HTTPException(422, f"invalid status {status_value!r}")
    execute(
        """
        UPDATE ops_console.open_item
        SET status = %s,
            completed_at = CASE WHEN %s IN ('done', 'dropped') THEN now() ELSE NULL END
        WHERE id = %s
        """,
        (status_value, status_value, item_id),
    )
    return RedirectResponse("/open-items", status_code=302)
