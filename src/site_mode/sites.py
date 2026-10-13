"""Create, read, list and delete sites. Every function takes an open cursor."""
import json

from config.config_table_structure import SITE_PLZ_PREFIX
from src.site_mode.errors import SiteConflict, SiteError, SiteNotFound

MAX_SITE_BUILDINGS = 2000
# A job whose worker has not reported for this long is treated as failed.
STALE_JOB_SECONDS = 120

GEOJSON_TO_3035 = (
    "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform("
    "ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326), 3035)), 3))"
)

# Column lists mirror what set_residential_buildings_table and
# set_other_buildings_table read from res and oth, including the 25 m2 floor.
COPY_BUILDINGS_SQL = """
INSERT INTO site_building (site_id, building_id, kind, osm_id, area, use, building_t, occupants,
    floors, constructi, f_class, f_classes, geom, height_max, height_ground, height_median,
    floors_3dbag, bag_id, energy_label, energy_index, cbs_population, cbs_households,
    cbs_avg_household_size)
SELECT %(site)s, row_number() OVER (ORDER BY kind, osm_id, f_class), b.*
FROM (
    SELECT 'res' AS kind, osm_id, area, use, building_t, occupants, floors, constructi, f_class,
           f_classes, r.geom, height_max, height_ground, height_median, floors_3dbag, bag_id,
           energy_label, energy_index, cbs_population, cbs_households, cbs_avg_household_size
    FROM res r JOIN site s ON s.site_id = %(site)s
    WHERE r.country_code = s.country_code AND r.geom && s.geom
      AND ST_Intersects(r.geom, s.geom) AND ST_Area(r.geom) >= 25
    UNION ALL
    SELECT 'oth', osm_id, area, use, NULL, NULL, NULL, NULL, f_class,
           f_classes, o.geom, height_max, height_ground, height_median, floors_3dbag, bag_id,
           energy_label, energy_index, cbs_population, cbs_households, cbs_avg_household_size
    FROM oth o JOIN site s ON s.site_id = %(site)s
    WHERE o.country_code = s.country_code AND o.geom && s.geom
      AND ST_Intersects(o.geom, s.geom) AND ST_Area(o.geom) >= 25
) b(kind, osm_id, area, use, building_t, occupants, floors, constructi, f_class, f_classes, geom,
    height_max, height_ground, height_median, floors_3dbag, bag_id, energy_label, energy_index,
    cbs_population, cbs_households, cbs_avg_household_size);
"""

# Stations placed through /add-transformer live in the shared table as
# type 'UserPlaced'; they belong to other users' models, not to the site.
COPY_TRANSFORMERS_SQL = """
INSERT INTO site_transformer (site_id, osm_id, source, rated_kva, geom)
SELECT %(site)s, t.osm_id, 'osm', NULL, ST_Centroid(t.geom)
FROM transformers t JOIN site s ON s.site_id = %(site)s
WHERE t.country_code = s.country_code AND ST_Within(t.geom, s.geom)
  AND COALESCE(t.type, '') <> 'UserPlaced' AND t.osm_id NOT LIKE 'user/%%'
ORDER BY t.osm_id;
"""

SITE_SUMMARY_SQL = f"""
SELECT s.site_id, s.user_id, s.name, s.country_code, ST_AsGeoJSON(ST_Transform(s.geom, 4326)),
       s.revision, s.generated_revision, j.job_id,
       CASE WHEN j.status IN ('queued', 'running')
             AND COALESCE(j.heartbeat_at, j.started_at, j.created_at)
                 < NOW() - INTERVAL '{STALE_JOB_SECONDS} seconds'
            THEN 'failed' ELSE j.status END
FROM site s
LEFT JOIN LATERAL (
    SELECT job_id, status, heartbeat_at, started_at, created_at FROM site_job
    WHERE site_id = s.site_id ORDER BY job_id DESC LIMIT 1
) j ON TRUE
"""


def site_plz(site_id: int) -> str:
    """Pseudo-postcode under which a site's grid results are stored."""
    return f"{SITE_PLZ_PREFIX}{site_id}"


def create_site(cur, user_id: str, name: str, country_code: str, geom: dict) -> dict:
    """Create a site and copy the buildings and OSM transformers inside its polygon."""
    cur.execute("SELECT 1 FROM country WHERE country_code = %s", (country_code,))
    if cur.fetchone() is None:
        raise SiteError(f"Unknown country_code {country_code!r}")
    cur.execute(
        f"""INSERT INTO site (user_id, name, country_code, geom)
            VALUES (%(user)s, %(name)s, %(cc)s, {GEOJSON_TO_3035})
            RETURNING site_id, ST_Area(geom)""",
        {"user": user_id, "name": name, "cc": country_code, "geom": json.dumps(geom)},
    )
    site_id, area_m2 = cur.fetchone()
    if not area_m2:
        raise SiteError("Site geometry has no area after validation")
    cur.execute(COPY_BUILDINGS_SQL, {"site": site_id})
    building_count = cur.rowcount
    if building_count == 0:
        raise SiteError(f"No {country_code} buildings inside the site polygon; is the region loaded?")
    if building_count > MAX_SITE_BUILDINGS:
        raise SiteError(f"Site holds {building_count} buildings; the limit is {MAX_SITE_BUILDINGS}")
    cur.execute("UPDATE site SET last_building_id = %s WHERE site_id = %s", (building_count, site_id))
    cur.execute(COPY_TRANSFORMERS_SQL, {"site": site_id})
    return {"site_id": site_id, "revision": 1, "building_count": building_count,
            "transformer_count": cur.rowcount}


def _summary(row) -> dict:
    """Convert a SITE_SUMMARY_SQL row into the API's site object."""
    last_job = {"job_id": row[7], "status": row[8]} if row[7] is not None else None
    return {"site_id": row[0], "user_id": row[1], "name": row[2], "country_code": row[3],
            "geom": json.loads(row[4]), "revision": row[5], "generated_revision": row[6],
            "last_job": last_job}


def get_site(cur, site_id: int) -> dict:
    """Return one site, raising SiteNotFound if it does not exist."""
    cur.execute(SITE_SUMMARY_SQL + " WHERE s.site_id = %s", (site_id,))
    row = cur.fetchone()
    if row is None:
        raise SiteNotFound(f"Site {site_id} not found")
    return _summary(row)


def list_sites(cur, user_id: str) -> list:
    """Return the sites owned by user_id, newest first, without geometry."""
    cur.execute(SITE_SUMMARY_SQL + " WHERE s.user_id = %s ORDER BY s.site_id DESC", (user_id,))
    keys = ("site_id", "name", "revision", "generated_revision", "last_job")
    return [{k: v for k, v in _summary(row).items() if k in keys} for row in cur.fetchall()]


def lock_site_for_edit(cur, site_id: int) -> None:
    """Lock the site row; refuse edits while a generation job is active."""
    cur.execute("SELECT 1 FROM site WHERE site_id = %s FOR UPDATE", (site_id,))
    if cur.fetchone() is None:
        raise SiteNotFound(f"Site {site_id} not found")
    last_job = get_site(cur, site_id)["last_job"]
    if last_job and last_job["status"] in ("queued", "running"):
        raise SiteConflict(f"Site {site_id} has an active generation job {last_job['job_id']}")


def bump_revision(cur, site_id: int) -> int:
    """Increment the site's revision after an edit and return the new value."""
    cur.execute(
        "UPDATE site SET revision = revision + 1, updated_at = NOW() WHERE site_id = %s RETURNING revision",
        (site_id,),
    )
    return cur.fetchone()[0]


def delete_site(cur, site_id: int) -> None:
    """Delete a site, its copied inputs and its grid results."""
    lock_site_for_edit(cur, site_id)
    cur.execute("DELETE FROM postcode WHERE plz = %s", (site_plz(site_id),))
    cur.execute("DELETE FROM site WHERE site_id = %s", (site_id,))
