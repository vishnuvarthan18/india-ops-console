"""publish_precision, enforced on the way out.

The database already refuses to *store* a sacred grove, traditional-knowledge
record or FRA claim at full precision — that is a trigger in migration 0001.
These tests cover the other half: the console must also refuse to *show* or
*export* a location it is not allowed to publish.

PLAN.md §5 is about the data, not the viewer. An export stops being internal
the moment someone forwards it.
"""

import csv
import io
import re

from tests.conftest import needs_db


def visible_text(html: str) -> str:
    """The page with its markup stripped.

    Necessary because the icons are inline SVG, and an SVG path is full of
    numbers — `m10.2 10.2 3.3 3.3` in the search icon matched a naive search
    for a latitude and produced a false positive. A leak test that cries wolf
    gets deleted, so it has to look at what a reader actually sees.
    """
    html = re.sub(r"<svg\b.*?</svg>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)

GROVE = "culture:sacred_grove:sg-1"
OPEN_RECORD = "pa:protected_area:res-1"


@needs_db
def test_restricted_record_shows_no_coordinates(signed_in):
    text = visible_text(signed_in.get(f"/entity/{GROVE}").text)
    assert "Location withheld" in text
    # The seeded grove sits at 76.51, 10.21. Neither the raw pair nor the
    # five-decimal form the template renders may appear anywhere a reader sees.
    for leak in ["76.51", "10.21", "76.51000", "10.21000"]:
        assert leak not in text, f"the page revealed {leak}"


@needs_db
def test_restricted_record_explains_why(signed_in):
    html = signed_in.get(f"/entity/{GROVE}").text
    assert "district aggregate" in html
    assert "database itself" in html


@needs_db
def test_open_record_does_show_coordinates(signed_in):
    """The rule must be specific. If everything is hidden, the feature is
    broken rather than careful."""
    text = visible_text(signed_in.get(f"/entity/{OPEN_RECORD}").text)
    assert "Coordinates" in text
    assert "Location withheld" not in text
    assert re.search(r"\d+\.\d{5},\s*\d+\.\d{5}", text), (
        "an unrestricted record should show its actual coordinates"
    )


@needs_db
def test_export_drops_restricted_coordinates(signed_in):
    rows = list(csv.DictReader(io.StringIO(signed_in.get("/data/export.csv").text)))
    groves = [r for r in rows if r["entity_type"] == "sacred_grove"]
    assert groves, "fixture should contain restricted records"
    assert all(r["latitude"] == "" and r["longitude"] == "" for r in groves)


@needs_db
def test_export_keeps_unrestricted_coordinates(signed_in):
    rows = list(csv.DictReader(io.StringIO(signed_in.get("/data/export.csv").text)))
    open_rows = [r for r in rows if r["entity_type"] == "protected_area"]
    assert any(r["latitude"] for r in open_rows)


@needs_db
def test_export_excludes_withheld_records(signed_in):
    rows = list(csv.DictReader(io.StringIO(signed_in.get("/data/export.csv").text)))
    assert not [r for r in rows if r["publish_precision"] == "withhold"]


@needs_db
def test_map_coarsens_restricted_points(signed_in):
    gj = signed_in.get("/map/data.geojson").json()
    coarse = [f for f in gj["features"] if f["properties"]["coarsened"]]
    assert coarse, "fixture should contain restricted records with geometry"
    distinct = {tuple(f["geometry"]["coordinates"]) for f in coarse}
    assert len(distinct) < len(coarse), (
        "restricted points must collapse to shared district points, "
        f"but {len(coarse)} records produced {len(distinct)} distinct locations"
    )


@needs_db
def test_map_marks_coarsened_points_as_such(signed_in):
    """A viewer must be able to tell an exact location from an approximate one."""
    gj = signed_in.get("/map/data.geojson").json()
    assert any(f["properties"]["coarsened"] for f in gj["features"])
    assert any(not f["properties"]["coarsened"] for f in gj["features"])


@needs_db
def test_map_never_returns_withheld_records(signed_in):
    gj = signed_in.get("/map/data.geojson").json()
    uids = {f["properties"]["uid"] for f in gj["features"]}
    withheld = signed_in.get("/data?precision=withhold").text
    for uid in uids:
        assert uid not in withheld or "Nothing matches" in withheld
