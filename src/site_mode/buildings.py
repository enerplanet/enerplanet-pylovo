"""Read a site's buildings and replace their demand overrides."""
import json

from src.site_mode.errors import SiteValidationError
from src.site_mode.sites import bump_revision, lock_site_for_edit

# Mirrors set_building_peak_load (preprocessing_mixin.py) for the peak and
# the yearly_demand_kwh expression of build_grid_response (api/routers/grid.py)
# for the yearly figure. Change them together.
BUILDINGS_SQL = """
WITH b AS (
    SELECT sb.*, ST_Area(sb.geom) AS a, COALESCE(sb.floors, 1) AS fl,
           CASE WHEN NULLIF(TRIM(COALESCE(sb.f_class_override, sb.f_class)), '') IS NULL
                  OR LOWER(TRIM(COALESCE(sb.f_class_override, sb.f_class)))
                     IN ('unknown', 'none', 'null', 'nan', 'n_a', 'na')
                THEN 'yes' ELSE LOWER(TRIM(COALESCE(sb.f_class_override, sb.f_class))) END AS fc
    FROM site_building sb WHERE sb.site_id = %(site)s
), d AS (
    SELECT b.*, cc.load_method, cc.peak_load, cc.peak_load_per_m2,
           COALESCE(b.occupants::int,
               CASE WHEN cc.load_method = 'household'
                    THEN CASE WHEN b.a < 160 THEN GREATEST(1, b.fl)
                              ELSE GREATEST(1, floor(b.a / 90) * b.fl) END
                    ELSE 1 END) AS hh,
           def.peak_load AS def_peak, def.peak_load_per_m2 AS def_per_m2,
           COALESCE(cc.load_method, def.load_method, 'area') = 'household' AS yearly_per_household,
           COALESCE(cc.yearly_consumption, def.yearly_consumption, 0) AS yearly_household_kwh,
           b.a * COALESCE(cc.yearly_consumption_per_m2, def.yearly_consumption_per_m2, 0) AS yearly_area_kwh,
           cc.definition IS NOT NULL AS has_cc
    FROM b
    LEFT JOIN consumer_categories cc ON cc.definition = b.fc
    LEFT JOIN consumer_categories def ON def.definition = '_default'
)
SELECT building_id, osm_id, fc, a, ST_AsGeoJSON(ST_Transform(geom, 4326)), hh,
       CASE WHEN has_cc THEN
                CASE WHEN load_method = 'household' THEN hh * COALESCE(peak_load, 0)
                     WHEN load_method = 'area' THEN a * COALESCE(peak_load_per_m2, 0) / 1000
                     ELSE 0 END
            WHEN def_peak IS NOT NULL AND def_peak != 'NaN' THEN hh * def_peak
            ELSE a * COALESCE(NULLIF(def_per_m2, 'NaN'), 29.0) / 1000 END AS peak_kw,
       measured_peak_kw, measured_yearly_kwh, dwellings, excluded,
       (SELECT COALESCE(json_agg(json_build_object('category', l.category, 'kw', l.kw)
                                 ORDER BY l.category), '[]'::json)
        FROM site_building_load l
        WHERE l.site_id = %(site)s AND l.building_id = d.building_id),
       CASE WHEN yearly_per_household THEN hh * yearly_household_kwh ELSE yearly_area_kwh END,
       COALESCE(measured_yearly_kwh,
                CASE WHEN yearly_per_household THEN COALESCE(dwellings, hh) * yearly_household_kwh
                     ELSE yearly_area_kwh END),
       source, source_id, f_class_override
FROM d ORDER BY building_id;
"""


def _overrides(row) -> dict | None:
    """Return the overrides object of a building row, or None when it has none."""
    peak, yearly, dwellings, excluded, loads = row[7:12]
    f_class = row[16]
    if peak is None and yearly is None and dwellings is None and not excluded and not loads and f_class is None:
        return None
    return {"measured_peak_kw": peak, "measured_yearly_kwh": yearly, "dwellings": dwellings,
            "extra_loads": loads, "excluded": excluded, "f_class": f_class}


def list_buildings(cur, site_id: int) -> dict:
    """Return the site's buildings as a GeoJSON FeatureCollection in EPSG:4326."""
    cur.execute(BUILDINGS_SQL, {"site": site_id})
    features = []
    for row in cur.fetchall():
        properties = {
            "building_id": row[0], "osm_id": row[1], "f_class": row[2], "area_m2": row[3],
            "defaults": {"dwellings": row[5], "peak_kw": row[6], "yearly_kwh": row[12]},
            "yearly_kwh_used": row[13],
            "overrides": _overrides(row),
            "source": row[14], "source_id": row[15],
            # A 3D-only building starts with a placeholder class until someone sets one.
            "f_class_confirmed": row[14] == "osm" or row[16] is not None,
        }
        features.append({"type": "Feature", "geometry": json.loads(row[4]), "properties": properties})
    return {"type": "FeatureCollection", "features": features}


def _validate_patch(cur, site_id: int, patches: list) -> None:
    """Reject unknown buildings, duplicates and override combinations the rules forbid."""
    ids = [p["building_id"] for p in patches]
    if len(ids) != len(set(ids)):
        raise SiteValidationError("A building_id appears more than once in the request")
    cur.execute(
        "SELECT building_id FROM site_building WHERE site_id = %s AND building_id = ANY(%s::int[])",
        (site_id, ids),
    )
    unknown = sorted(set(ids) - {r[0] for r in cur.fetchall()})
    if unknown:
        raise SiteValidationError(f"Unknown building_id(s) in site {site_id}: {unknown}")
    for p in patches:
        o = p["overrides"]
        if o and o["measured_peak_kw"] is not None and o["extra_loads"]:
            raise SiteValidationError(
                f"Building {p['building_id']}: extra_loads cannot be combined with measured_peak_kw, "
                "which may already contain them"
            )
    overrides = [p["overrides"] for p in patches if p["overrides"]]
    require_categories(cur, {load["category"] for o in overrides for load in o["extra_loads"]}, "extra-load")
    require_categories(cur, {o["f_class"] for o in overrides if o["f_class"]}, "f_class")
    _validate_excluded_unassigned(cur, site_id, patches)


def require_categories(cur, categories: set, what: str) -> None:
    """Reject category names that are not definitions in consumer_categories."""
    if not categories:
        return
    cur.execute("SELECT definition FROM consumer_categories WHERE definition = ANY(%s::text[])",
                (list(categories),))
    unknown = sorted(categories - {r[0] for r in cur.fetchall()})
    if unknown:
        raise SiteValidationError(f"Unknown {what} categories: {unknown}")


def _validate_excluded_unassigned(cur, site_id: int, patches: list) -> None:
    """Reject excluding a building that has a fixed transformer assignment."""
    excluded = [p["building_id"] for p in patches if p["overrides"] and p["overrides"]["excluded"]]
    if not excluded:
        return
    cur.execute(
        """SELECT building_id, transformer_id FROM site_transformer_assignment
           WHERE site_id = %s AND building_id = ANY(%s::int[]) ORDER BY building_id""",
        (site_id, excluded),
    )
    assigned = cur.fetchall()
    if assigned:
        pairs = ", ".join(f"building {b} on transformer {t}" for b, t in assigned)
        raise SiteValidationError(f"Excluded buildings have fixed assignments: {pairs}")


def _write_overrides(cur, site_id: int, building_id: int, o: dict | None) -> None:
    """Replace one building's overrides and extra loads."""
    o = o or {"measured_peak_kw": None, "measured_yearly_kwh": None, "dwellings": None,
              "extra_loads": [], "excluded": False, "f_class": None}
    cur.execute(
        """UPDATE site_building SET measured_peak_kw = %s, measured_yearly_kwh = %s,
               dwellings = %s, excluded = %s, f_class_override = %s
           WHERE site_id = %s AND building_id = %s""",
        (o["measured_peak_kw"], o["measured_yearly_kwh"], o["dwellings"], o["excluded"],
         o["f_class"], site_id, building_id),
    )
    cur.execute("DELETE FROM site_building_load WHERE site_id = %s AND building_id = %s",
                (site_id, building_id))
    for load in o["extra_loads"]:
        cur.execute("INSERT INTO site_building_load VALUES (%s, %s, %s, %s)",
                    (site_id, building_id, load["category"], load["kw"]))


def patch_buildings(cur, site_id: int, patches: list) -> int:
    """Replace the overrides of the listed buildings and return the new site revision."""
    lock_site_for_edit(cur, site_id)
    _validate_patch(cur, site_id, patches)
    for p in patches:
        _write_overrides(cur, site_id, p["building_id"], p["overrides"])
    return bump_revision(cur, site_id)
