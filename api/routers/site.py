"""Site-mode endpoints: sites with their own buildings, demand overrides and transformers.

Geometry is GeoJSON in EPSG:4326. pylovo stores the owning user_id but does
not authenticate; the calling platform enforces access.
"""
from contextlib import contextmanager
from typing import List

from fastapi import APIRouter, HTTPException, Response

from models.site import BuildingPatch, CreateSiteRequest, SiteTransformerInput
from src.database.connection_pool import get_cursor
from src.site_mode import buildings, jobs, sites, transformers
from src.site_mode.errors import SiteError

router = APIRouter(prefix="/sites", tags=["site"])


@contextmanager
def site_cursor():
    """Yield a pooled cursor in one transaction; map site errors to HTTP errors."""
    try:
        with get_cursor() as cur:
            yield cur
    except SiteError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e


@router.post("", status_code=201)
def create_site(payload: CreateSiteRequest):
    """Create a site from a polygon and copy the buildings and OSM transformers inside it."""
    with site_cursor() as cur:
        return sites.create_site(cur, payload.user_id, payload.name, payload.country_code,
                                 payload.geom.model_dump())


@router.get("")
def list_sites(user_id: str):
    """List the sites owned by user_id."""
    with site_cursor() as cur:
        return sites.list_sites(cur, user_id)


@router.get("/{site_id}")
def get_site(site_id: int):
    """Return a site with its revision and last generation job."""
    with site_cursor() as cur:
        return sites.get_site(cur, site_id)


@router.delete("/{site_id}", status_code=204)
def delete_site(site_id: int):
    """Delete a site, its copied inputs and its grid results."""
    with site_cursor() as cur:
        sites.delete_site(cur, site_id)
    return Response(status_code=204)


@router.get("/{site_id}/buildings")
def list_buildings(site_id: int):
    """Return the site's buildings with default demand and overrides."""
    with site_cursor() as cur:
        sites.get_site(cur, site_id)
        return buildings.list_buildings(cur, site_id)


@router.patch("/{site_id}/buildings")
def patch_buildings(site_id: int, payload: List[BuildingPatch]):
    """Replace the overrides of the listed buildings."""
    patches = [p.model_dump() for p in payload]
    with site_cursor() as cur:
        return {"revision": buildings.patch_buildings(cur, site_id, patches)}


@router.get("/{site_id}/transformers")
def list_transformers(site_id: int):
    """Return the site's transformers and fixed assignments."""
    with site_cursor() as cur:
        sites.get_site(cur, site_id)
        return transformers.list_transformers(cur, site_id)


@router.put("/{site_id}/transformers")
def replace_transformers(site_id: int, payload: List[SiteTransformerInput]):
    """Replace the site's transformers and fixed assignments with the given list."""
    items = [i.model_dump() for i in payload]
    with site_cursor() as cur:
        revision = transformers.replace_transformers(cur, site_id, items)
        return {"revision": revision, "transformers": transformers.list_transformers(cur, site_id)}


@router.post("/{site_id}/generate", status_code=202)
def generate(site_id: int):
    """Queue generation of the site's current revision; 409 while another job is active."""
    with site_cursor() as cur:
        job = jobs.queue_job(cur, site_id)
    jobs.start_job_process(job["job_id"])
    return job


@router.get("/{site_id}/jobs/{job_id}")
def get_job(site_id: int, job_id: int):
    """Return a generation job's status and result."""
    with site_cursor() as cur:
        return jobs.get_job(cur, site_id, job_id)
