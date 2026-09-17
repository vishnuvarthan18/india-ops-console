"""Every page renders with real data shapes.

Cheap to run and it catches the class of bug that only appears once a template
meets a null column, an empty list or a record with no geometry.
"""

import pytest

from tests.conftest import needs_db

PAGES = [
    "/", "/alerts", "/engines", "/engines/protected_areas",
    "/data", "/data?engine=protected_areas", "/data?geom=no", "/data?geom=yes",
    "/data?precision=district_aggregate", "/data?q=reserve", "/data?page=2",
    "/data?engine=protected_areas&state=Kerala&geom=yes",
    "/entity/pa:protected_area:res-1", "/entity/culture:sacred_grove:sg-1",
    "/map", "/map?engine=protected_areas", "/server",
    "/decisions", "/decisions/india-data-core", "/decisions/india-data-core?q=D-71",
    "/decisions/india-data-core?q=zzznothing",
    "/keys", "/open-items",
]


@needs_db
@pytest.mark.parametrize("path", PAGES)
def test_page_renders(signed_in, path):
    r = signed_in.get(path)
    assert r.status_code == 200, f"{path} returned {r.status_code}"
    assert "<html" in r.text
    # A Jinja failure can still return 200 with the error swallowed into the
    # page; these markers prove the shell actually rendered.
    assert "nav-item" in r.text, f"{path} rendered without the navigation"


@needs_db
def test_empty_search_offers_a_way_out(signed_in):
    """A dead end is a bug. An empty result must say so and let you recover."""
    html = signed_in.get("/data?q=zzzznothingmatchesthis").text
    assert "Nothing matches" in html
    assert "Clear all filters" in html


@needs_db
def test_active_filters_are_shown_and_removable(signed_in):
    html = signed_in.get("/data?engine=protected_areas&state=Kerala").text
    assert html.count('class="chip"') >= 2
    assert "Clear all" in html


@needs_db
def test_a_record_carries_its_filter_back(signed_in):
    """Arriving at a record from a filtered list and going back must return to
    that list, not to an unfiltered one."""
    import re
    html = signed_in.get("/data?engine=protected_areas").text
    m = re.search(r'href="(/entity/[^"]*from=[^"]*)"', html)
    assert m, "record links should carry the current filter"
    detail = signed_in.get(m.group(1).replace("&amp;", "&")).text
    assert 'href="/data?engine' in detail


@needs_db
def test_pagination_keeps_the_filter(signed_in):
    html = signed_in.get("/data?engine=living_species").text
    if "Next" in html and "page=2" in html:
        assert "engine=living_species&page=2" in html.replace("&amp;", "&")


@needs_db
def test_login_page_has_no_navigation(client):
    """A signed-out visitor must not be shown the shape of the system."""
    client.post("/logout")
    html = client.get("/login").text
    assert "Sign in" in html
    assert "nav-item" not in html
    assert "Open items" not in html


@needs_db
def test_health_endpoint_needs_no_session(client):
    client.post("/logout")
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@needs_db
def test_accessibility_basics_are_present(signed_in):
    html = signed_in.get("/").text
    assert 'class="skip"' in html, "needs a skip-to-content link"
    assert "<main" in html and 'id="main"' in html
    assert 'aria-label="Sections"' in html
    assert "aria-current" in html, "the current page must be marked for screen readers"
    assert 'scope="col"' in html, "table headers must be scoped"
