"""Site scope for /power-flow and /grid-statistics: reject grids of an older site revision."""
from fastapi import HTTPException

from src.database.connection_pool import get_cursor
from src.site_mode.errors import SiteError
from src.site_mode.scope import check_site_grids, site_building_keys


def require_site_grids(site_id: int, revision: int | None, grid_ids: list,
                       building_osm_ids: list | None = None) -> list | None:
    """Check grid_ids belong to the site's revision; return building_osm_ids as site result keys.

    Raises HTTPException 404 ("grid regenerated") or 422 when revision is missing.
    """
    if revision is None:
        raise HTTPException(status_code=422, detail="revision is required together with site_id")
    try:
        with get_cursor() as cur:
            check_site_grids(cur, site_id, revision, grid_ids)
            if building_osm_ids:
                return site_building_keys(cur, site_id, building_osm_ids)
            return building_osm_ids
    except SiteError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
