"""Read and replace a site's transformers and fixed building assignments."""
import json

from src.site_mode.errors import SiteValidationError
from src.site_mode.sites import bump_revision, get_site, lock_site_for_edit

POINT_SQL = "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326), 3035)"

# Positions within 1 cm (EPSG:3035 metres) count as unchanged, so coordinates
# read back from GET, which are rounded, compare equal.
STATION_CHANGED_SQL = f"""
SELECT rated_kva IS DISTINCT FROM %(kva)s OR NOT ST_DWithin(geom, {POINT_SQL}, 0.01)
FROM site_transformer WHERE site_id = %(site)s AND transformer_id = %(id)s
"""

TRANSFORMERS_SQL = """
SELECT t.transformer_id, t.osm_id, t.source, t.rated_kva, t.cost_eur, ST_AsGeoJSON(ST_Transform(t.geom, 4326)),
       COALESCE(array_agg(a.building_id ORDER BY a.building_id)
                FILTER (WHERE a.building_id IS NOT NULL), '{}')
FROM site_transformer t
LEFT JOIN site_transformer_assignment a
       ON a.site_id = t.site_id AND a.transformer_id = t.transformer_id
WHERE t.site_id = %s
GROUP BY t.transformer_id
ORDER BY t.transformer_id;
"""


def list_transformers(cur, site_id: int) -> list:
    """Return the site's transformers with their fixed building assignments."""
    cur.execute(TRANSFORMERS_SQL, (site_id,))
    return [
        {"transformer_id": r[0], "osm_id": r[1], "source": r[2], "rated_kva": r[3], "cost_eur": r[4],
         "geom": json.loads(r[5]), "assigned_building_ids": list(r[6])}
        for r in cur.fetchall()
    ]


def _validate_transformers(cur, site_id: int, items: list) -> None:
    """Check ids belong to the site and every assigned building is assignable once."""
    given_ids = [i["transformer_id"] for i in items if i["transformer_id"] is not None]
    if len(given_ids) != len(set(given_ids)):
        raise SiteValidationError("A transformer_id appears more than once in the request")
    cur.execute("SELECT transformer_id FROM site_transformer WHERE site_id = %s", (site_id,))
    unknown = sorted(set(given_ids) - {r[0] for r in cur.fetchall()})
    if unknown:
        raise SiteValidationError(f"Unknown transformer_id(s) in site {site_id}: {unknown}")
    assigned = [b for i in items for b in i["assigned_building_ids"]]
    duplicates = sorted({b for b in assigned if assigned.count(b) > 1})
    if duplicates:
        raise SiteValidationError(f"Buildings assigned to more than one transformer: {duplicates}")
    cur.execute(
        "SELECT building_id, excluded FROM site_building WHERE site_id = %s AND building_id = ANY(%s::int[])",
        (site_id, assigned),
    )
    rows = dict(cur.fetchall())
    missing = sorted(set(assigned) - set(rows))
    if missing:
        raise SiteValidationError(f"Unknown building_id(s) in site {site_id}: {missing}")
    excluded = sorted(b for b, is_excluded in rows.items() if is_excluded)
    if excluded:
        raise SiteValidationError(f"Excluded buildings cannot be assigned: {excluded}")


def _station_params(site_id: int, item: dict) -> dict:
    """Query parameters for one station of a PUT /transformers body."""
    return {"site": site_id, "kva": item["rated_kva"], "cost": item["cost_eur"],
            "geom": json.dumps(item["geom"]), "id": item["transformer_id"]}


def _changes_grid(cur, site_id: int, items: list) -> bool:
    """Whether items differ from the stored stations in anything a generation reads, so not in cost."""
    if any(i["transformer_id"] is None for i in items):
        return True
    cur.execute("SELECT transformer_id FROM site_transformer WHERE site_id = %s", (site_id,))
    if {r[0] for r in cur.fetchall()} != {i["transformer_id"] for i in items}:
        return True
    cur.execute("SELECT building_id, transformer_id FROM site_transformer_assignment WHERE site_id = %s",
                (site_id,))
    if set(cur.fetchall()) != {(b, i["transformer_id"]) for i in items for b in i["assigned_building_ids"]}:
        return True
    for item in items:
        cur.execute(STATION_CHANGED_SQL, _station_params(site_id, item))
        if cur.fetchone()[0]:
            return True
    return False


def _upsert_transformer(cur, site_id: int, item: dict) -> int:
    """Update an existing station or insert a new user station; return its id."""
    params = _station_params(site_id, item)
    if item["transformer_id"] is None:
        cur.execute(
            f"""INSERT INTO site_transformer (site_id, source, rated_kva, cost_eur, geom)
                VALUES (%(site)s, 'user', %(kva)s, %(cost)s, {POINT_SQL}) RETURNING transformer_id""",
            params,
        )
        return cur.fetchone()[0]
    cur.execute(
        f"""UPDATE site_transformer SET rated_kva = %(kva)s, cost_eur = %(cost)s, geom = {POINT_SQL}
            WHERE site_id = %(site)s AND transformer_id = %(id)s""",
        params,
    )
    return item["transformer_id"]


def replace_transformers(cur, site_id: int, items: list) -> int:
    """Replace the site's stations and assignments with items; return the site's revision.

    The revision is bumped unless only station costs change, since cost does
    not change the grid.
    """
    lock_site_for_edit(cur, site_id)
    _validate_transformers(cur, site_id, items)
    changes_grid = _changes_grid(cur, site_id, items)
    kept = [i["transformer_id"] for i in items if i["transformer_id"] is not None]
    cur.execute(
        "DELETE FROM site_transformer WHERE site_id = %s AND NOT (transformer_id = ANY(%s::int[]))",
        (site_id, kept),
    )
    cur.execute("DELETE FROM site_transformer_assignment WHERE site_id = %s", (site_id,))
    for item in items:
        transformer_id = _upsert_transformer(cur, site_id, item)
        for building_id in item["assigned_building_ids"]:
            cur.execute("INSERT INTO site_transformer_assignment VALUES (%s, %s, %s)",
                        (site_id, building_id, transformer_id))
    return bump_revision(cur, site_id) if changes_grid else get_site(cur, site_id)["revision"]
