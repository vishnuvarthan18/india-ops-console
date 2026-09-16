"""Section 3 — the data browser and map.

Two rules shape everything here.

**Filters are built from a fixed column map, never from user strings.** Every
filter the UI offers maps to a hardcoded SQL fragment with a placeholder; a
parameter the map does not know is ignored rather than interpolated. There is
no path by which a query string reaches SQL as text.

**publish_precision is honoured even internally.** The database already refuses
to store a sacred grove, traditional-knowledge record or FRA claim at full
precision (the entity_publish_precision_guard trigger). This module applies the
matching read-side rule: anything not marked 'full' has its geometry reduced to
a district-level representative point on the map, and 'withhold' rows never
appear in an export at all. That is stricter than strictly necessary for an
internal tool — but an export is a file that leaves the platform the moment
someone emails it, and the plan's §5 line is about the data, not the viewer.
"""

import logging

from app.db import fetch_all, fetch_one

logger = logging.getLogger(__name__)

PAGE_SIZE = 50

# filter name -> (SQL fragment with one placeholder, value transform)
FILTERS: dict[str, tuple[str, callable]] = {
    "engine":      ("e.key = %s",                      str),
    "entity_type": ("en.entity_type = %s",             str),
    "state":       ("en.state = %s",                   str),
    "precision":   ("en.publish_precision = %s::publish_precision", str),
    "geom":        ("(en.geom IS NOT NULL) = %s",      lambda v: v == "yes"),
    "q":           ("lower(en.name) LIKE %s",          lambda v: f"%{v.lower()}%"),
}


def _where(params: dict) -> tuple[str, list]:
    clauses, values = [], []
    for name, raw in params.items():
        if name not in FILTERS or raw in (None, "", "any"):
            continue
        frag, transform = FILTERS[name]
        clauses.append(frag)
        values.append(transform(raw))
    return (" AND ".join(clauses) if clauses else "true"), values


def search(params: dict, page: int = 1) -> dict:
    where, values = _where(params)
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    total = fetch_one(
        f"""
        SELECT count(*) AS n
        FROM entity en JOIN engine e ON e.id = en.engine_id
        WHERE {where}
        """,
        tuple(values),
    )["n"]

    rows = fetch_all(
        f"""
        SELECT en.id, en.uid, en.name, en.name_local, en.entity_type,
               en.state, en.district,
               en.publish_precision::text AS publish_precision,
               (en.geom IS NOT NULL) AS has_geom,
               en.is_permanent_gap, en.updated_at,
               e.key AS engine_key, e.name AS engine_name,
               COALESCE(fc.n, 0) AS fact_count
        FROM entity en
        JOIN engine e ON e.id = en.engine_id
        LEFT JOIN (SELECT entity_id, count(*) n FROM entity_fact GROUP BY 1) fc
               ON fc.entity_id = en.id
        WHERE {where}
        ORDER BY en.name
        LIMIT %s OFFSET %s
        """,
        tuple(values) + (PAGE_SIZE, offset),
    )

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "page_size": PAGE_SIZE,
    }


def export_rows(params: dict, limit: int = 20000) -> list[dict]:
    """CSV export. 'withhold' entities are excluded outright, and anything
    below full precision is exported without coordinates — see the module
    docstring."""
    where, values = _where(params)
    return fetch_all(
        f"""
        SELECT en.uid, en.name, en.name_local, en.entity_type, en.state, en.district,
               e.key AS engine, en.publish_precision::text AS publish_precision,
               CASE WHEN en.publish_precision = 'full'
                    THEN ST_Y(en.centroid) END AS latitude,
               CASE WHEN en.publish_precision = 'full'
                    THEN ST_X(en.centroid) END AS longitude,
               en.updated_at
        FROM entity en
        JOIN engine e ON e.id = en.engine_id
        WHERE ({where}) AND en.publish_precision <> 'withhold'
        ORDER BY e.key, en.name
        LIMIT %s
        """,
        tuple(values) + (limit,),
    )


def facets() -> dict:
    """Values that actually exist, so the filter dropdowns never offer a
    combination that returns nothing."""
    return {
        "engines": fetch_all(
            "SELECT e.key, e.name, count(en.id) AS n FROM engine e "
            "LEFT JOIN entity en ON en.engine_id = e.id GROUP BY e.key, e.name "
            "HAVING count(en.id) > 0 ORDER BY e.name"
        ),
        "entity_types": fetch_all(
            "SELECT entity_type, count(*) AS n FROM entity GROUP BY 1 ORDER BY 2 DESC"
        ),
        "states": fetch_all(
            "SELECT state, count(*) AS n FROM entity WHERE state IS NOT NULL "
            "GROUP BY 1 ORDER BY 1"
        ),
    }


def entity_detail(uid: str) -> dict | None:
    row = fetch_one(
        """
        SELECT en.*, e.key AS engine_key, e.name AS engine_name,
               ST_AsGeoJSON(en.geom) AS geom_json,
               ST_Y(en.centroid) AS lat, ST_X(en.centroid) AS lon,
               ST_GeometryType(en.geom) AS geom_type
        FROM entity en JOIN engine e ON e.id = en.engine_id
        WHERE en.uid = %s
        """,
        (uid,),
    )
    if row is None:
        return None

    row["facts"] = fetch_all(
        """
        SELECT f.field_name, f.value_text, f.value_num, f.value_date, f.value_json,
               f.unit, f.confidence::text AS confidence, f.license,
               f.publish_precision::text AS publish_precision,
               f.retrieved_at, f.valid_from, f.valid_to, f.superseded_at,
               s.key AS source_key, s.name AS source_name,
               (b.entity_id IS NOT NULL) AS is_best
        FROM entity_fact f
        JOIN source s ON s.id = f.source_id
        LEFT JOIN entity_fact_best b
               ON b.entity_id = f.entity_id AND b.field_name = f.field_name
              AND b.source_id = f.source_id
        WHERE f.entity_id = %s
        ORDER BY f.field_name, f.retrieved_at DESC
        """,
        (row["id"],),
    )

    row["relations"] = fetch_all(
        """
        SELECT r.relation_type, 'out' AS direction, o.uid, o.name, o.entity_type
        FROM entity_relation r JOIN entity o ON o.id = r.to_entity_id
        WHERE r.from_entity_id = %s
        UNION ALL
        SELECT r.relation_type, 'in', o.uid, o.name, o.entity_type
        FROM entity_relation r JOIN entity o ON o.id = r.from_entity_id
        WHERE r.to_entity_id = %s
        ORDER BY 1, 4
        """,
        (row["id"], row["id"]),
    )
    return row


def map_geojson(params: dict, limit: int = 3000) -> dict:
    """GeoJSON for the map.

    Full-precision entities get their real centroid. Anything restricted is
    shown at a coarse representative point derived from its district's own
    average, so the map still conveys *that* something is there without
    conveying *where* — and 'withhold' is dropped entirely.

    Polygons are not returned: at national zoom they are invisible and cost
    megabytes. The detail page draws the real geometry for one entity.
    """
    where, values = _where(params)
    rows = fetch_all(
        f"""
        WITH district_centre AS (
          SELECT state, district,
                 avg(ST_Y(centroid)) AS lat, avg(ST_X(centroid)) AS lon
          FROM entity
          WHERE centroid IS NOT NULL AND publish_precision = 'full'
          GROUP BY state, district
        )
        SELECT en.uid, en.name, en.entity_type, en.state, en.district,
               e.key AS engine_key,
               en.publish_precision::text AS publish_precision,
               CASE WHEN en.publish_precision = 'full'
                    THEN ST_Y(en.centroid) ELSE dc.lat END AS lat,
               CASE WHEN en.publish_precision = 'full'
                    THEN ST_X(en.centroid) ELSE dc.lon END AS lon,
               (en.publish_precision <> 'full') AS coarsened
        FROM entity en
        JOIN engine e ON e.id = en.engine_id
        LEFT JOIN district_centre dc
               ON dc.state = en.state AND dc.district IS NOT DISTINCT FROM en.district
        WHERE ({where})
          AND en.publish_precision <> 'withhold'
          AND en.centroid IS NOT NULL
        LIMIT %s
        """,
        tuple(values) + (limit,),
    )

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "uid": r["uid"], "name": r["name"], "type": r["entity_type"],
                "engine": r["engine_key"], "state": r["state"],
                "district": r["district"], "coarsened": r["coarsened"],
            },
        }
        for r in rows
        if r["lat"] is not None and r["lon"] is not None
    ]
    return {"type": "FeatureCollection", "features": features,
            "returned": len(features), "limit": limit}


def geometry_coverage() -> list[dict]:
    """The gap view: how much of each engine actually has coordinates.
    This is the number that tells you whether the map is showing you the
    platform or only the lucky quarter of it."""
    return fetch_all(
        """
        SELECT e.key AS engine_key, e.name AS engine_name, en.entity_type,
               count(*) AS total,
               count(*) FILTER (WHERE en.geom IS NOT NULL) AS with_geom,
               round(100.0 * count(*) FILTER (WHERE en.geom IS NOT NULL)
                     / NULLIF(count(*), 0), 1) AS pct
        FROM entity en JOIN engine e ON e.id = en.engine_id
        GROUP BY e.key, e.name, en.entity_type
        ORDER BY e.name, count(*) DESC
        """
    )
