"""Add buildings from a 3D source that match no OSM building, and remove them again."""
import json

from src.site_mode.buildings import list_buildings, require_categories
from src.site_mode.errors import SiteNotFound, SiteValidationError
from src.site_mode.sites import GEOJSON_TO_3035, MAX_SITE_BUILDINGS, bump_revision, lock_site_for_edit

FOOTPRINT_SQL = f"""
SELECT ST_Area(g.geom), ST_Intersects(g.geom, s.geom)
FROM (SELECT {GEOJSON_TO_3035} AS geom) g, site s
WHERE s.site_id = %(site)s
"""

# kind follows the class's load method, which matches the res/oth split of
# OSM buildings for all but a few classes. Only kind 'res' keeps the floors the
# caller sends; the household estimate uses them.
INSERT_SQL = f"""
INSERT INTO site_building (site_id, building_id, osm_id, kind, area, f_class, geom, floors, source, source_id)
SELECT %(site)s, %(id)s, NULL, CASE cc.load_method WHEN 'household' THEN 'res' ELSE 'oth' END,
       ST_Area(g.geom), %(f_class)s, g.geom, %(floors)s, %(source)s, %(source_id)s
FROM (SELECT {GEOJSON_TO_3035} AS geom) g
JOIN consumer_categories cc ON cc.definition = %(f_class)s
"""


def _validate_added(cur, site_id: int, items: list) -> None:
    """Reject repeated or already present source ids, unknown classes and an overfull site."""
    source_ids = [i["source_id"] for i in items]
    repeated = sorted({s for s in source_ids if source_ids.count(s) > 1})
    if repeated:
        raise SiteValidationError(f"source_id(s) repeated in the request: {repeated}")
    cur.execute(
        """SELECT source_id FROM site_building
           WHERE site_id = %s AND source = 'city2tabula' AND source_id = ANY(%s::text[])""",
        (site_id, source_ids),
    )
    present = sorted(r[0] for r in cur.fetchall())
    if present:
        raise SiteValidationError(f"source_id(s) already in site {site_id}: {present}")
    require_categories(cur, {i["f_class"] for i in items}, "f_class")
    cur.execute("SELECT count(*) FROM site_building WHERE site_id = %s", (site_id,))
    total = cur.fetchone()[0] + len(items)
    if total > MAX_SITE_BUILDINGS:
        raise SiteValidationError(f"Site would hold {total} buildings; the limit is {MAX_SITE_BUILDINGS}")


def _insert(cur, site_id: int, building_id: int, item: dict) -> None:
    """Insert one added building; reject a footprint outside the site or under 25 m2."""
    params = {"site": site_id, "id": building_id, "f_class": item["f_class"], "floors": item["floors"],
              "source": item["source"], "source_id": item["source_id"], "geom": json.dumps(item["geom"])}
    cur.execute(FOOTPRINT_SQL, params)
    area_m2, inside = cur.fetchone()
    if not inside:
        raise SiteValidationError(f"source_id {item['source_id']}: footprint does not intersect the site")
    if area_m2 < 25:
        raise SiteValidationError(f"source_id {item['source_id']}: footprint is {area_m2:.1f} m2, under 25 m2")
    cur.execute(INSERT_SQL, params)


def add_buildings(cur, site_id: int, items: list) -> dict:
    """Add 3D-only buildings to the site; return the new revision and the stored buildings."""
    lock_site_for_edit(cur, site_id)
    _validate_added(cur, site_id, items)
    # Ids come from a per-site counter, so a deleted building's id is never reused.
    cur.execute("UPDATE site SET last_building_id = last_building_id + %s WHERE site_id = %s "
                "RETURNING last_building_id", (len(items), site_id))
    last_id = cur.fetchone()[0]
    new_ids = list(range(last_id - len(items) + 1, last_id + 1))
    for building_id, item in zip(new_ids, items):
        _insert(cur, site_id, building_id, item)
    revision = bump_revision(cur, site_id)
    added = [f for f in list_buildings(cur, site_id)["features"] if f["properties"]["building_id"] in new_ids]
    return {"revision": revision, "buildings": added}


def delete_building(cur, site_id: int, building_id: int) -> int:
    """Remove an added building; OSM-copied buildings are excluded instead. Return the new revision."""
    lock_site_for_edit(cur, site_id)
    cur.execute("SELECT source FROM site_building WHERE site_id = %s AND building_id = %s", (site_id, building_id))
    row = cur.fetchone()
    if row is None:
        raise SiteNotFound(f"Building {building_id} not found in site {site_id}")
    if row[0] != "city2tabula":
        raise SiteValidationError(f"Building {building_id} came from OSM; exclude it instead of deleting it")
    cur.execute("SELECT transformer_id FROM site_transformer_assignment WHERE site_id = %s AND building_id = %s",
                (site_id, building_id))
    assigned = cur.fetchone()
    if assigned:
        raise SiteValidationError(f"Building {building_id} has a fixed assignment to transformer {assigned[0]}")
    cur.execute("DELETE FROM site_building WHERE site_id = %s AND building_id = %s", (site_id, building_id))
    return bump_revision(cur, site_id)
