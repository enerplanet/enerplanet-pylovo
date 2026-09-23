"""
Administrative boundary API endpoint.
Fetches region boundaries from OpenStreetMap Nominatim API.
"""

import httpx
import json
import re
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import asyncio
from functools import lru_cache
import time
from shapely.geometry import Point, shape

router = APIRouter(prefix="/boundary", tags=["boundary"])

# Simple in-memory cache for boundaries
_boundary_cache = {}
_cache_ttl = 3600  # 1 hour


class BoundaryRequest(BaseModel):
    """Request model for boundary lookup."""
    lat: float
    lon: float
    admin_level: Optional[int] = 4  # 4=state, 6=district, 8=municipality


class RegionInfo(BaseModel):
    """Region metadata."""
    name: str
    admin_level: int
    country: Optional[str] = None
    country_code: Optional[str] = None
    osm_id: Optional[int] = None
    osm_type: Optional[str] = None


class BoundaryResponse(BaseModel):
    """Response model for boundary lookup."""
    status: str
    region: Optional[RegionInfo] = None
    boundary: Optional[dict] = None  # GeoJSON Feature
    message: Optional[str] = None


def _get_cache_key(lat: float, lon: float, admin_level: int) -> str:
    """Generate cache key from coordinates (rounded to reduce cache misses)."""
    # Round to 2 decimal places (~1km precision) for caching
    return f"{round(lat, 2)}:{round(lon, 2)}:{admin_level}"


def _get_cached_boundary(key: str) -> Optional[dict]:
    """Get boundary from cache if not expired."""
    if key in _boundary_cache:
        cached = _boundary_cache[key]
        if time.time() - cached["timestamp"] < _cache_ttl:
            return cached["data"]
        else:
            del _boundary_cache[key]
    return None


def _set_cached_boundary(key: str, data: dict):
    """Store boundary in cache."""
    _boundary_cache[key] = {
        "data": data,
        "timestamp": time.time()
    }
    # Limit cache size
    if len(_boundary_cache) > 1000:
        # Remove oldest entries
        oldest_keys = sorted(_boundary_cache.keys(), 
                           key=lambda k: _boundary_cache[k]["timestamp"])[:100]
        for k in oldest_keys:
            del _boundary_cache[k]


def _bbox_tuple_to_polygon_geometry(bbox: tuple[float, float, float, float]) -> dict:
    """Convert (west, south, east, north) bbox tuple to GeoJSON Polygon geometry."""
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


async def fetch_boundary_from_nominatim(
    lat: float, 
    lon: float, 
    admin_level: int = 4
) -> dict:
    """
    Fetch administrative boundary from Nominatim OSM API.
    
    Args:
        lat: Latitude
        lon: Longitude  
        admin_level: OSM admin level (4=state, 6=district, 8=municipality)
    
    Returns:
        Dict with region info and boundary GeoJSON
    """
    # Nominatim reverse geocoding endpoint
    url = "https://nominatim.openstreetmap.org/reverse"
    
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "polygon_geojson": 1,
        "zoom": admin_level + 3,  # Zoom level affects detail
        "extratags": 1,
        "namedetails": 1,
    }
    
    headers = {
        "User-Agent": "EnerplanET/1.0 (https://enerplanet-dev.th-deg.de; contact@th-deg.de)"
    }
    
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.get(url, params=params, headers=headers)
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Nominatim API error: {response.status_code}"
            )
        
        data = response.json()
        
        if "error" in data:
            return {
                "status": "not_found",
                "message": data.get("error", "Location not found")
            }
        
        # Extract region info
        address = data.get("address", {})
        
        # Determine region name based on admin level
        region_name = None
        if admin_level <= 4:
            # State level
            region_name = (
                address.get("state") or 
                address.get("province") or
                address.get("region") or
                data.get("name")
            )
        elif admin_level <= 6:
            # District level
            region_name = (
                address.get("county") or
                address.get("district") or
                address.get("state_district") or
                data.get("name")
            )
        else:
            # Municipality level
            region_name = (
                address.get("municipality") or
                address.get("city") or
                address.get("town") or
                address.get("village") or
                data.get("name")
            )
        
        # Get boundary geometry
        geojson = data.get("geojson")
        
        if not geojson:
            return {
                "status": "no_boundary",
                "message": "No boundary geometry available for this location"
            }
        
        # Build response
        result = {
            "status": "success",
            "region": {
                "name": region_name or "Unknown",
                "admin_level": admin_level,
                "country": address.get("country"),
                "country_code": address.get("country_code", "").upper(),
                "osm_id": data.get("osm_id"),
                "osm_type": data.get("osm_type"),
            },
            "boundary": {
                "type": "Feature",
                "properties": {
                    "name": region_name,
                    "admin_level": admin_level,
                    "country": address.get("country"),
                    "country_code": address.get("country_code", "").upper(),
                },
                "geometry": geojson
            }
        }
        
        return result


@router.get("", response_model=BoundaryResponse)
async def get_boundary(
    lat: float = Query(..., description="Latitude of point within region"),
    lon: float = Query(..., description="Longitude of point within region"),
    admin_level: int = Query(4, description="Admin level: 4=state, 6=district, 8=municipality")
):
    """
    Get administrative boundary for a location.
    
    Returns the boundary polygon for the administrative region containing
    the specified coordinates.
    
    Admin levels (Germany example):
    - 2: Country
    - 4: State (Bundesland) - e.g., Hamburg, Bayern
    - 6: District (Landkreis)
    - 8: Municipality (Gemeinde)
    
    Netherlands:
    - 4: Province - e.g., Noord-Holland, Zuid-Holland
    - 8: Municipality (Gemeente)
    """
    # Check cache first
    cache_key = _get_cache_key(lat, lon, admin_level)
    cached = _get_cached_boundary(cache_key)
    if cached:
        return BoundaryResponse(**cached)
    
    try:
        result = await fetch_boundary_from_nominatim(lat, lon, admin_level)
        
        # Cache successful results
        if result.get("status") == "success":
            _set_cached_boundary(cache_key, result)
        
        return BoundaryResponse(**result)
        
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Nominatim API timeout"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch boundary: {str(e)}"
        )


@router.post("", response_model=BoundaryResponse)
async def get_boundary_for_polygon(request: BoundaryRequest):
    """
    Get administrative boundary for a location (POST version).
    
    Same as GET but accepts JSON body.
    """
    return await get_boundary(
        lat=request.lat,
        lon=request.lon,
        admin_level=request.admin_level
    )


@router.get("/regions")
async def list_supported_regions(version_id: Optional[str] = Query(None, description="Version ID for grid counts")):
    """
    List all supported regions from the regions.yaml configuration.
    
    Returns the hierarchy of countries and their states/provinces
    that are configured in the system, including grid counts per state.
    """
    import yaml
    from pathlib import Path
    from src.config_loader import get_country_code
    from src.database.database_client import DatabaseClient
    from src.config_loader import VERSION_ID
    
    config_path = Path(__file__).parent.parent.parent / "datapipeline" / "config" / "regions.yaml"
    
    if not config_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Regions configuration not found"
        )
    
    with open(config_path) as f:
        regions = yaml.safe_load(f)

    target_version = version_id or VERSION_ID

    def _normalize_state_code(state_key: str) -> str:
        """Normalize state keys to DB canonical state_code format."""
        normalized = re.sub(r"[^a-z0-9]+", "_", str(state_key).strip().lower())
        return normalized.strip("_")

    # Load grid counts once for all country/state pairs.
    state_grid_counts: dict[tuple[str, str], int] = {}
    try:
        with DatabaseClient() as dbc:
            dbc.cur.execute(
                """
                WITH gc AS (
                    SELECT country_code, plz, COUNT(grid_result_id) AS grid_count
                    FROM grid_result
                    WHERE version_id = %s
                    GROUP BY country_code, plz
                )
                SELECT p.country_code, p.state_code, SUM(COALESCE(gc.grid_count, 0))::bigint AS grid_count
                FROM postcode p
                JOIN gc
                  ON gc.country_code = p.country_code
                 AND gc.plz = p.plz
                WHERE p.state_code IS NOT NULL
                  AND p.state_code <> ''
                GROUP BY p.country_code, p.state_code
                """,
                (target_version,),
            )
            for cc, sc, gc in dbc.cur.fetchall():
                state_grid_counts[(str(cc).upper(), _normalize_state_code(str(sc)))] = int(gc or 0)
    except Exception:
        # Keep API functional even if DB stats query fails.
        state_grid_counts = {}
    
    # Simplify output
    result = {}
    for country_key, country_data in regions.items():
        if isinstance(country_data, dict):
            country_code = get_country_code(country_key)
            country_grid_count = 0
            result[country_key] = {
                "name": country_data.get("name"),
                "country_code": country_code,
                "osm_relation_id": country_data.get("osm_relation_id"),
                "states": {},
                "grid_count": 0,
            }
            if "states" in country_data:
                for state_key, state_data in country_data["states"].items():
                    state_norm = _normalize_state_code(state_key)
                    state_grid_count = state_grid_counts.get((country_code, state_norm), 0)
                    country_grid_count += state_grid_count
                    result[country_key]["states"][state_key] = {
                        "name": state_data.get("name"),
                        "osm_relation_id": state_data.get("osm_relation_id"),
                        "grid_count": state_grid_count,
                    }
            result[country_key]["grid_count"] = country_grid_count
    
    return {"version_id": target_version, "regions": result}


@router.get("/states")
async def get_state_boundaries(
    country_code: Optional[str] = Query(None, description="Optional country code filter (e.g. DE, NL)"),
    version_id: Optional[str] = Query(None, description="Version ID for postcode/grid scope"),
    geometry_mode: str = Query("bbox", description="bbox (fast), full (postcode union), or osm (accurate OSM relation polygon)"),
    simplify_m: float = Query(200.0, description="Simplification tolerance in meters for full geometry"),
):
    """
    Return administrative state boundaries directly from local postcode_result geometry.

    This endpoint avoids external Nominatim calls and is suitable for fetching
    boundaries once and caching on the frontend.
    """
    from src.database.database_client import DatabaseClient
    from src.config_loader import VERSION_ID as DEFAULT_VERSION_ID

    mode = (geometry_mode or "bbox").strip().lower()
    if mode not in {"bbox", "full", "osm"}:
        raise HTTPException(status_code=400, detail="geometry_mode must be 'bbox', 'full', or 'osm'")

    target_version = str(version_id or DEFAULT_VERSION_ID)
    cc_filter = str(country_code or "").strip().upper()
    if simplify_m < 0:
        raise HTTPException(status_code=400, detail="simplify_m must be >= 0")

    cache_key = f"state_boundaries:{target_version}:{cc_filter or '*'}:{mode}:{round(float(simplify_m), 2)}"
    cached = _get_cached_boundary(cache_key)
    if cached:
        return cached

    if mode == "osm":
        country_clause = " AND s.country_code = %s" if cc_filter else ""
        db_params = [target_version, target_version]
        if cc_filter:
            db_params.append(cc_filter)

        query = f"""
            WITH gc AS (
                SELECT country_code, plz, COUNT(grid_result_id) AS cnt
                FROM grid_result
                WHERE version_id = %s
                GROUP BY country_code, plz
            ),
            grid_counts AS (
                SELECT pr.country_code,
                       pr.state_code,
                       SUM(COALESCE(gc.cnt, 0))::bigint AS grid_count
                FROM postcode_result pr
                JOIN gc
                  ON gc.country_code = pr.country_code
                 AND gc.plz = pr.postcode_result_plz
                WHERE pr.version_id = %s
                  AND pr.state_code IS NOT NULL
                  AND pr.state_code <> ''
                GROUP BY pr.country_code, pr.state_code
            )
            SELECT s.country_code,
                   s.state_code,
                   COALESCE(s.state_name, s.state_code) AS state_name,
                   s.osm_relation_id,
                   COALESCE(gc.grid_count, 0)::bigint AS grid_count
            FROM state s
            LEFT JOIN grid_counts gc
              ON gc.country_code = s.country_code
             AND gc.state_code = s.state_code
            WHERE s.osm_relation_id IS NOT NULL
              AND s.osm_relation_id > 0
              {country_clause}
            ORDER BY s.country_code, s.state_code;
        """

        try:
            with DatabaseClient() as dbc:
                dbc.cur.execute(query, tuple(db_params))
                rows = dbc.cur.fetchall()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to fetch OSM state metadata: {exc}") from exc

        if not rows:
            payload = {
                "status": "success",
                "version_id": target_version,
                "geometry_mode": mode,
                "regions": [],
            }
            _set_cached_boundary(cache_key, payload)
            return payload

        osm_lookup = {}
        row_meta = {}
        for cc, sc, state_name, osm_relation_id, grid_count in rows:
            if not osm_relation_id:
                continue
            osm_id = int(osm_relation_id)
            cc_norm = str(cc or "").upper()
            sc_norm = str(sc or "")
            state_name_norm = str(state_name or sc_norm or "Unknown")
            osm_lookup[osm_id] = {
                "name": state_name_norm,
                "country": None,
                "country_code": cc_norm,
                "admin_level": 4,
                "osm_type": "relation",
            }
            row_meta[osm_id] = {
                "country_code": cc_norm,
                "state_code": sc_norm,
                "state_name": state_name_norm,
                "grid_count": int(grid_count or 0),
            }

        boundaries_by_osm = await _batch_fetch_boundaries(osm_lookup)

        regions = []
        for osm_id, meta in row_meta.items():
            bdata = boundaries_by_osm.get(osm_id) or {}
            boundary_feature = bdata.get("boundary")
            bbox_tuple = None
            if boundary_feature:
                bbox_tuple = _bbox_from_geojson(boundary_feature.get("geometry"))
            if bbox_tuple is None:
                bbox_tuple = bdata.get("bbox")

            if not boundary_feature and bbox_tuple:
                source = "osm_relation_bbox_fallback"
                boundary_feature = {
                    "type": "Feature",
                    "properties": {
                        "name": meta["state_name"],
                        "admin_level": 4,
                        "country_code": meta["country_code"],
                        "state_code": meta["state_code"],
                        "source": source,
                    },
                    "geometry": _bbox_tuple_to_polygon_geometry(bbox_tuple),
                }
            else:
                source = "osm_relation_polygon" if boundary_feature else "osm_relation_unavailable"
                if boundary_feature:
                    # Keep response metadata explicit even for exact OSM geometry.
                    boundary_feature["properties"] = {
                        **(boundary_feature.get("properties") or {}),
                        "state_code": meta["state_code"],
                        "source": source,
                    }

            bbox = None
            if bbox_tuple:
                bbox = {
                    "west": bbox_tuple[0],
                    "south": bbox_tuple[1],
                    "east": bbox_tuple[2],
                    "north": bbox_tuple[3],
                }

            regions.append(
                {
                    "country_code": meta["country_code"],
                    "state_code": meta["state_code"],
                    "grid_count": meta["grid_count"],
                    "bbox": bbox,
                    "region": {
                        "name": meta["state_name"],
                        "admin_level": 4,
                        "country": None,
                        "country_code": meta["country_code"],
                        "osm_id": osm_id,
                        "osm_type": "relation",
                        "source": source,
                    },
                    "boundary": boundary_feature,
                }
            )

        payload = {
            "status": "success",
            "version_id": target_version,
            "geometry_mode": mode,
            "regions": regions,
        }
        _set_cached_boundary(cache_key, payload)
        return payload

    country_clause = " AND pr.country_code = %s" if cc_filter else ""
    bbox_sql = """
        ST_AsGeoJSON(
            ST_Transform(
                ST_SetSRID(ST_Envelope(ST_Extent(pr.geom)::geometry), 3035),
                4326
            )
        ) AS bbox_geojson
    """

    if mode == "full":
        query = f"""
            WITH gc AS (
                SELECT country_code, plz, COUNT(grid_result_id) AS cnt
                FROM grid_result
                WHERE version_id = %s
                GROUP BY country_code, plz
            )
            SELECT pr.country_code,
                   pr.state_code,
                   COALESCE(s.state_name, pr.state_code) AS state_name,
                   SUM(COALESCE(gc.cnt, 0))::bigint AS grid_count,
                   {bbox_sql},
                   ST_AsGeoJSON(
                       ST_Transform(
                           ST_SimplifyPreserveTopology(ST_UnaryUnion(ST_Collect(pr.geom)), %s),
                           4326
                       )
                   ) AS boundary_geojson
            FROM postcode_result pr
            LEFT JOIN state s
              ON s.country_code = pr.country_code
             AND s.state_code = pr.state_code
            LEFT JOIN gc
              ON gc.country_code = pr.country_code
             AND gc.plz = pr.postcode_result_plz
            WHERE pr.version_id = %s
              AND pr.state_code IS NOT NULL
              AND pr.state_code <> ''
              {country_clause}
            GROUP BY pr.country_code, pr.state_code, s.state_name
            ORDER BY pr.country_code, pr.state_code;
        """
        db_params = [target_version, float(simplify_m), target_version]
        if cc_filter:
            db_params.append(cc_filter)
    else:
        query = f"""
            WITH gc AS (
                SELECT country_code, plz, COUNT(grid_result_id) AS cnt
                FROM grid_result
                WHERE version_id = %s
                GROUP BY country_code, plz
            )
            SELECT pr.country_code,
                   pr.state_code,
                   COALESCE(s.state_name, pr.state_code) AS state_name,
                   SUM(COALESCE(gc.cnt, 0))::bigint AS grid_count,
                   {bbox_sql},
                   NULL::text AS boundary_geojson
            FROM postcode_result pr
            LEFT JOIN state s
              ON s.country_code = pr.country_code
             AND s.state_code = pr.state_code
            LEFT JOIN gc
              ON gc.country_code = pr.country_code
             AND gc.plz = pr.postcode_result_plz
            WHERE pr.version_id = %s
              AND pr.state_code IS NOT NULL
              AND pr.state_code <> ''
              {country_clause}
            GROUP BY pr.country_code, pr.state_code, s.state_name
            ORDER BY pr.country_code, pr.state_code;
        """
        db_params = [target_version, target_version]
        if cc_filter:
            db_params.append(cc_filter)

    try:
        with DatabaseClient() as dbc:
            dbc.cur.execute(query, tuple(db_params))
            rows = dbc.cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch state boundaries: {exc}") from exc

    regions = []
    for cc, sc, state_name, grid_count, bbox_geojson, boundary_geojson in rows:
        bbox_geom = json.loads(bbox_geojson) if bbox_geojson else None
        boundary_geom = json.loads(boundary_geojson) if boundary_geojson else bbox_geom
        if not bbox_geom:
            continue

        bbox_tuple = _bbox_from_geojson(bbox_geom)
        bbox = None
        if bbox_tuple:
            bbox = {
                "west": bbox_tuple[0],
                "south": bbox_tuple[1],
                "east": bbox_tuple[2],
                "north": bbox_tuple[3],
            }

        source = "postcode_result_full" if mode == "full" and boundary_geojson else "postcode_result_bbox"
        region_name = str(state_name or sc or "Unknown")
        country_code_value = str(cc or "").upper()
        state_code_value = str(sc or "")
        boundary_feature = None
        if boundary_geom:
            boundary_feature = {
                "type": "Feature",
                "properties": {
                    "name": region_name,
                    "admin_level": 4,
                    "country_code": country_code_value,
                    "state_code": state_code_value,
                    "source": source,
                },
                "geometry": boundary_geom,
            }

        regions.append(
            {
                "country_code": country_code_value,
                "state_code": state_code_value,
                "grid_count": int(grid_count or 0),
                "bbox": bbox,
                "region": {
                    "name": region_name,
                    "admin_level": 4,
                    "country": None,
                    "country_code": country_code_value,
                    "osm_id": None,
                    "osm_type": None,
                    "source": source,
                },
                "boundary": boundary_feature,
            }
        )

    payload = {
        "status": "success",
        "version_id": target_version,
        "geometry_mode": mode,
        "regions": regions,
    }
    _set_cached_boundary(cache_key, payload)
    return payload


async def _batch_fetch_boundaries(osm_lookup: dict) -> dict:
    """Fetch boundaries for multiple OSM relations via Nominatim /lookup.

    Splits requests into chunks to respect Nominatim rate limits and uses
    the lightweight ``boundingbox`` field as a bbox fallback when polygon
    geometry is unavailable.  Region metadata is always returned so that
    downstream centroid→state matching works even when Nominatim is degraded.

    Returns: {osm_id: {region, boundary, bbox}}
    """
    if not osm_lookup:
        return {}

    cache_key = "__batch_boundaries__:" + ",".join(str(k) for k in sorted(osm_lookup.keys()))
    cached = _get_cached_boundary(cache_key)
    if cached:
        return cached

    # Pre-populate every OSM ID with region metadata so regions are never
    # silently dropped when Nominatim is unreachable.
    results: dict = {}
    for osm_id, meta in osm_lookup.items():
        results[osm_id] = {
            "region": {
                "name": meta["name"],
                "admin_level": meta["admin_level"],
                "country": meta["country"],
                "country_code": meta["country_code"],
                "osm_id": osm_id,
                "osm_type": "relation",
            },
            "boundary": None,
            "bbox": None,
        }

    url = "https://nominatim.openstreetmap.org/lookup"
    headers = {
        "User-Agent": "EnerplanET/1.0 (https://enerplanet-dev.th-deg.de; contact@th-deg.de)"
    }
    chunk_size = 50
    osm_ids = list(osm_lookup.keys())

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            for i in range(0, len(osm_ids), chunk_size):
                chunk = osm_ids[i:i + chunk_size]
                osm_ids_str = ",".join(f"R{oid}" for oid in chunk)
                params = {
                    "osm_ids": osm_ids_str,
                    "format": "jsonv2",
                    "polygon_geojson": 1,
                    "extratags": 1,
                }

                try:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code != 200:
                        print(f"[Boundary] Nominatim /lookup returned {resp.status_code} (chunk {i // chunk_size + 1})")
                    else:
                        data = resp.json()
                        if isinstance(data, list):
                            _merge_nominatim_items(data, osm_lookup, results)
                except Exception as exc:
                    print(f"[Boundary] Chunk {i // chunk_size + 1} error: {exc}")
                finally:
                    # Throttle between chunks to respect Nominatim usage policy
                    if i + chunk_size < len(osm_ids):
                        await asyncio.sleep(0.5)

        # Lightweight fallback for OSM IDs still missing a bbox
        missing = [oid for oid in osm_ids if results[oid]["bbox"] is None]
        if missing:
            print(f"[Boundary] Fetching bboxes for {len(missing)} regions (no-polygon fallback)")
            async with httpx.AsyncClient(timeout=None) as client:
                for i in range(0, len(missing), 50):
                    chunk = missing[i:i + 50]
                    osm_ids_str = ",".join(f"R{oid}" for oid in chunk)
                    try:
                        resp = await client.get(url, params={"osm_ids": osm_ids_str, "format": "jsonv2"}, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, list):
                                for item in data:
                                    osm_id = item.get("osm_id")
                                    if osm_id in results and results[osm_id]["bbox"] is None:
                                        results[osm_id]["bbox"] = _bbox_from_nominatim(item)
                    except Exception as exc:
                        print(f"[Boundary] Fallback bbox error: {exc}")
                    if i + 50 < len(missing):
                        await asyncio.sleep(1.0)

    except Exception as e:
        print(f"[Boundary] Batch fetch error: {e}")

    # Only cache if at least one bbox was resolved. Use a short TTL for
    # partial results so a transient Nominatim outage doesn't lock in
    # bad data for the full cache period.
    resolved = sum(1 for v in results.values() if v.get("bbox") is not None)
    if resolved == 0:
        return results
    if resolved < len(results):
        # Partial — cache for 5 minutes so retries happen soon
        _boundary_cache[cache_key] = {"data": results, "timestamp": time.time() - _cache_ttl + 300}
    else:
        _set_cached_boundary(cache_key, results)
    return results


def _merge_nominatim_items(data: list, osm_lookup: dict, results: dict):
    """Merge Nominatim response items into *results* dict."""
    for item in data:
        osm_id = item.get("osm_id")
        if osm_id not in osm_lookup:
            continue
        meta = osm_lookup[osm_id]
        geojson = item.get("geojson")
        region_name = meta["name"]

        boundary_feature = None
        if geojson:
            boundary_feature = {
                "type": "Feature",
                "properties": {
                    "name": region_name,
                    "admin_level": meta["admin_level"],
                    "country": meta["country"],
                    "country_code": meta["country_code"],
                },
                "geometry": geojson,
            }

        bbox = _bbox_from_geojson(geojson) if geojson else None
        if bbox is None:
            bbox = _bbox_from_nominatim(item)

        results[osm_id] = {
            "region": {
                "name": region_name,
                "admin_level": meta["admin_level"],
                "country": meta["country"],
                "country_code": meta["country_code"],
                "osm_id": osm_id,
                "osm_type": "relation",
            },
            "boundary": boundary_feature,
            "bbox": bbox,
        }


def _boundary_contains_point(boundary_feature: Optional[dict], lon: float, lat: float) -> bool:
    """Return whether the boundary geometry contains the given WGS84 point."""
    if not boundary_feature:
        return False

    geometry = boundary_feature.get("geometry")
    if not geometry:
        return False

    try:
        return shape(geometry).covers(Point(lon, lat))
    except Exception:
        return False


def _bbox_area(west: float, south: float, east: float, north: float) -> float:
    """Approximate bbox area in geographic degrees for tie-breaking."""
    return max(0.0, east - west) * max(0.0, north - south)


def _match_region_for_point(
    state_bboxes: list[tuple],
    boundaries_by_osm: dict,
    country_code: str,
    lon: float,
    lat: float,
) -> Optional[int]:
    """Match a centroid to the most specific state boundary for a country."""
    exact_matches: list[tuple[float, int]] = []
    bbox_matches: list[tuple[float, int]] = []

    for osm_id, _name, scc, sw, ss, se, sn in state_bboxes:
        if scc != country_code:
            continue
        if not (sw <= lon <= se and ss <= lat <= sn):
            continue

        bbox_matches.append((_bbox_area(sw, ss, se, sn), osm_id))
        bdata = boundaries_by_osm.get(osm_id)
        if _boundary_contains_point(bdata.get("boundary") if bdata else None, lon, lat):
            exact_matches.append((_bbox_area(sw, ss, se, sn), osm_id))

    if exact_matches:
        exact_matches.sort(key=lambda item: item[0])
        return exact_matches[0][1]
    if bbox_matches:
        bbox_matches.sort(key=lambda item: item[0])
        return bbox_matches[0][1]
    return None


def _bbox_from_nominatim(item: dict):
    """Extract (west, south, east, north) from Nominatim's ``boundingbox`` field."""
    bb = item.get("boundingbox")
    if bb and len(bb) == 4:
        try:
            # Nominatim returns [south, north, west, east]
            return (float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1]))
        except (ValueError, TypeError):
            pass
    return None


def _bbox_from_geojson(geojson: dict):
    """Extract (west, south, east, north) from a GeoJSON geometry."""
    try:
        coords_type = geojson.get("type", "")
        coordinates = geojson.get("coordinates", [])
        if not coordinates:
            return None

        all_coords = []
        if coords_type == "Polygon":
            for ring in coordinates:
                all_coords.extend(ring)
        elif coords_type == "MultiPolygon":
            for polygon in coordinates:
                for ring in polygon:
                    all_coords.extend(ring)
        if not all_coords:
            return None

        lons = [c[0] for c in all_coords]
        lats = [c[1] for c in all_coords]
        return (min(lons), min(lats), max(lons), max(lats))
    except Exception:
        return None


@router.get("/available")
async def get_available_regions(
    version_id: Optional[str] = Query(None, description="Version ID for grid scope"),
):
    """
    Get regions that have grid data available in the database.

    Returns the centroid, bounding box and covered-area outline of every region
    that has generated grids.
    """
    from src.database.database_client import DatabaseClient
    from src.config_loader import VERSION_ID as DEFAULT_VERSION_ID

    try:
        dbc = DatabaseClient()
        target_version = str(version_id or DEFAULT_VERSION_ID)

        # ── Step 1: aggregate grid stats by state_code (from postcode_result) ──
        dbc.cur.execute("""
            WITH gc AS (
                SELECT version_id, country_code, plz, COUNT(grid_result_id) AS cnt
                FROM grid_result
                WHERE version_id = %s
                GROUP BY version_id, country_code, plz
            ),
            agg AS (
                SELECT pr.country_code,
                       pr.state_code,
                       SUM(COALESCE(gc.cnt, 0)) as grid_count,
                       AVG(ST_X(ST_Centroid(pr.geom))) as avg_cx,
                       AVG(ST_Y(ST_Centroid(pr.geom))) as avg_cy,
                       ST_Extent(pr.geom) as bbox_3035,
                       ST_UnaryUnion(ST_Collect(pr.geom)) as covered_3035
                FROM postcode_result pr
                JOIN gc ON gc.plz = pr.postcode_result_plz
                    AND gc.country_code = pr.country_code
                    AND gc.version_id = pr.version_id
                WHERE pr.version_id = %s
                  AND pr.state_code IS NOT NULL
                  AND pr.state_code <> ''
                GROUP BY pr.country_code, pr.state_code
            ),
            has_3d AS (
                SELECT pr2.state_code, pr2.country_code
                FROM buildings_result br
                JOIN grid_result gr2
                    ON br.grid_result_id = gr2.grid_result_id
                   AND br.version_id = gr2.version_id
                JOIN postcode_result pr2
                    ON gr2.plz = pr2.postcode_result_plz
                   AND gr2.country_code = pr2.country_code
                   AND gr2.version_id = pr2.version_id
                WHERE gr2.version_id = %s
                GROUP BY pr2.state_code, pr2.country_code
                HAVING AVG(CASE WHEN br.height_median IS NOT NULL
                                  OR br.floors_3dbag IS NOT NULL
                                THEN 1 ELSE 0 END) >= 0.03
            ),
            transformed AS (
                SELECT a.country_code,
                       a.state_code,
                       a.grid_count,
                       ST_Transform(ST_SetSRID(ST_MakePoint(a.avg_cx, a.avg_cy), 3035), 4326) as centroid,
                       ST_Transform(ST_SetSRID(a.bbox_3035::geometry, 3035), 4326) as bbox,
                       ST_Transform(a.covered_3035, 4326) as covered,
                       COALESCE(s.state_name, a.state_code) as state_name,
                       c.country_name,
                       s.osm_relation_id,
                       CASE WHEN h.state_code IS NOT NULL THEN true ELSE false END as has_3d
                FROM agg a
                LEFT JOIN state s ON a.state_code = s.state_code AND a.country_code = s.country_code
                LEFT JOIN country c ON a.country_code = c.country_code
                LEFT JOIN has_3d h ON a.state_code = h.state_code AND a.country_code = h.country_code
            )
            SELECT country_code,
                   state_code,
                   grid_count,
                   ST_X(centroid) as centroid_lon,
                   ST_Y(centroid) as centroid_lat,
                   ST_XMin(bbox) as bbox_west,
                   ST_YMin(bbox) as bbox_south,
                   ST_XMax(bbox) as bbox_east,
                   ST_YMax(bbox) as bbox_north,
                   state_name,
                   country_name,
                   osm_relation_id,
                   has_3d,
                   ST_AsGeoJSON(covered) as covered_geojson
            FROM transformed
            ORDER BY country_code, state_code
        """, (target_version, target_version, target_version))

        rows = dbc.cur.fetchall()
        dbc.cur.close()
        dbc.conn.close()

        if not rows:
            return {"status": "success", "regions": []}

        # boundary is the union of the covered postcode_result geometries, not the
        # administrative outline: those geometries are clipped to the extent of the
        # 3D source, so a region's administrative area can be far larger.
        region_list = []
        for row in rows:
            (country_code, state_code, grid_count, centroid_lon, centroid_lat,
             bbox_west, bbox_south, bbox_east, bbox_north,
             state_name, country_name, osm_relation_id, has_3d, covered_geojson) = row
            if centroid_lat is None or centroid_lon is None:
                continue

            cc = str(country_code or "").upper()
            region_name = str(state_name or state_code or "Unknown")
            country_label = country_name or None
            region_info = {
                "country_code": cc,
                "state_code": state_code,
                "grid_count": grid_count,
                "centroid": {"lat": centroid_lat, "lon": centroid_lon},
                "bbox": {
                    "west": bbox_west,
                    "south": bbox_south,
                    "east": bbox_east,
                    "north": bbox_north,
                },
                "has_3d": bool(has_3d),
                "region": {
                    "name": region_name,
                    "admin_level": 4,
                    "country": country_label,
                    "country_code": cc,
                    "state_code": state_code,
                    "osm_id": osm_relation_id,
                    "osm_type": "relation" if osm_relation_id else None,
                },
                "boundary": None,
            }

            if covered_geojson:
                region_info["boundary"] = {
                    "type": "Feature",
                    "properties": {
                        "name": region_name,
                        "admin_level": 4,
                        "country": country_label,
                        "country_code": cc,
                        "state_code": state_code,
                        "source": "postcode_result_covered",
                    },
                    "geometry": json.loads(covered_geojson),
                }

            region_list.append(region_info)

        return {
            "status": "success",
            "regions": region_list,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get available regions: {str(e)}",
        )
