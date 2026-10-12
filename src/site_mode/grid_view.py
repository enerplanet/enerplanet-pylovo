"""Turn a site's stored grid into the /generate-grid response shape with site fields added."""
from src.config_loader import VERSION_ID
from src.site_mode.buildings import list_buildings
from src.site_mode.errors import SiteNotFound
from src.site_mode.sites import get_site

STATIONS_SQL = """
SELECT g.grid_result_id, g.site_transformer_id, g.origin, g.loading_pct
FROM site_grid_transformer g WHERE g.site_id = %s
"""

GENERATED_AT_SQL = """
SELECT finished_at FROM site_job
WHERE site_id = %s AND status = 'succeeded' AND revision = %s
ORDER BY job_id DESC LIMIT 1
"""


def _demand_source(overrides: dict | None) -> str:
    """Name what a building's peak is based on: measured_peak, override or default."""
    if not overrides:
        return "default"
    if overrides["measured_peak_kw"] is not None:
        return "measured_peak"
    return "override" if overrides["dwellings"] is not None or overrides["extra_loads"] else "default"


def _site_buildings(features: list, site_buildings: dict) -> list:
    """Map generation keys back to site buildings and fold extra-load rows into them."""
    by_key, extra_kw = {}, {}
    for feature in features:
        parts = str(feature["properties"].get("osm_id", "")).split(":")
        if len(parts) == 4:  # site:<site_id>:<building_id>:<category> is an extra load
            extra_kw[int(parts[2])] = extra_kw.get(int(parts[2]), 0.0) + feature["properties"]["peak_load_in_kw"]
        elif len(parts) == 3:
            by_key[int(parts[2])] = feature
    result = []
    for building_id, feature in sorted(by_key.items()):
        site_building = site_buildings[building_id]
        props = feature["properties"]
        props.update({
            "building_id": building_id,
            "osm_id": site_building["osm_id"],
            "f_class": site_building["f_class"],
            "peak_kw_used": props["peak_load_in_kw"] + extra_kw.get(building_id, 0.0),
            "demand_source": _demand_source(site_building["overrides"]),
            "yearly_demand_kwh": site_building["yearly_kwh_used"],
        })
        result.append(feature)
    return result


def _site_transformers(cur, site_id: int, features: list) -> list:
    """Add each station's origin, site transformer id, loading and overload flag."""
    cur.execute(STATIONS_SQL, (site_id,))
    stations = {row[0]: row[1:] for row in cur.fetchall()}
    for feature in features:
        props = feature["properties"]
        site_transformer_id, origin, loading_pct = stations.get(props["grid_result_id"], (None, "new", None))
        props.update({
            "site_transformer_id": site_transformer_id,
            "origin": origin,
            "loading_pct": loading_pct,
            "overloaded": loading_pct is not None and loading_pct > 100,
        })
    return features


def site_grid(cur, site_id: int, grid_response: dict) -> dict:
    """Return grid_response rewritten for the site, with a meta block for labelling."""
    site = get_site(cur, site_id)
    if site["generated_revision"] is None:
        raise SiteNotFound(f"Site {site_id} has no generated grid")
    site_buildings = {f["properties"]["building_id"]: f["properties"]
                      for f in list_buildings(cur, site_id)["features"]}
    grid_response["buildings"]["features"] = _site_buildings(
        grid_response["buildings"]["features"], site_buildings)
    grid_response["transformers"]["features"] = _site_transformers(
        cur, site_id, grid_response["transformers"]["features"])
    cur.execute(GENERATED_AT_SQL, (site_id, site["generated_revision"]))
    row = cur.fetchone()
    grid_response["meta"] = {
        "mode": "site", "site_id": site_id, "revision": site["generated_revision"],
        "generated_at": row[0].isoformat() if row else None, "version_id": VERSION_ID,
    }
    return grid_response
