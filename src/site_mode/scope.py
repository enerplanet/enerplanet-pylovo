"""Scope checks for endpoints that take grid_result_ids of a site grid."""
from src.site_mode.errors import SiteNotFound
from src.site_mode.sites import site_plz

REGENERATED = "grid regenerated"


def check_site_grids(cur, site_id: int, revision: int, grid_ids: list) -> None:
    """Raise SiteNotFound unless every id is a grid of the site's current generated revision.

    This holds even if a database restore reissued old grid_result_ids.
    """
    cur.execute("SELECT generated_revision FROM site WHERE site_id = %s", (site_id,))
    row = cur.fetchone()
    if row is None:
        raise SiteNotFound(f"Site {site_id} not found")
    if row[0] is None or row[0] != revision:
        raise SiteNotFound(f"{REGENERATED}: site {site_id} is at generated revision {row[0]}, not {revision}")
    cur.execute(
        "SELECT grid_result_id FROM grid_result WHERE plz = %s AND grid_result_id = ANY(%s::int[])",
        (site_plz(site_id), grid_ids),
    )
    foreign = sorted(set(grid_ids) - {r[0] for r in cur.fetchall()})
    if foreign:
        raise SiteNotFound(f"{REGENERATED}: grid_result_id(s) {foreign} are not in site {site_id}")


def site_building_keys(cur, site_id: int, osm_ids: list) -> list:
    """Map building ids to the keys a site's buildings and their extra loads carry in buildings_result.

    An id matches a copied building's OSM id or an added building's source_id,
    since buildings added from a 3D source have no OSM id.
    """
    cur.execute(
        """SELECT 'site:' || sb.site_id || ':' || sb.building_id FROM site_building sb
           WHERE sb.site_id = %(site)s AND (sb.osm_id = ANY(%(ids)s::text[]) OR sb.source_id = ANY(%(ids)s::text[]))
           UNION ALL
           SELECT 'site:' || l.site_id || ':' || l.building_id || ':' || l.category
           FROM site_building_load l
           JOIN site_building sb ON sb.site_id = l.site_id AND sb.building_id = l.building_id
           WHERE l.site_id = %(site)s AND (sb.osm_id = ANY(%(ids)s::text[]) OR sb.source_id = ANY(%(ids)s::text[]))""",
        {"site": site_id, "ids": osm_ids},
    )
    return [r[0] for r in cur.fetchall()]
