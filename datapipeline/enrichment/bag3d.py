"""
3D BAG Enrichment Module

Enriches building data with attributes from the 3D BAG dataset
(https://3dbag.nl), maintained by TU Delft.

Provides:
- Actual number of floors (building:levels)
- Building height (from AHN LiDAR)
- Construction year (bouwjaar)
- BAG identifier (pand ID)
- 3D geometry (LoD1.2/LoD2.2)
- BAG units (verblijfsobjecten) inside each footprint, from the PDOK BAG WFS

Data source: https://3dbag.nl
License: CC-BY-4.0 (attribution required)
Attribution: 3D BAG by TU Delft 3D geoinformation group
"""

import gzip
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional

import requests

logger = logging.getLogger("datapipeline")

# 3D BAG data version and base URL
BAG3D_BASE_URL = "https://data.3dbag.nl"
BAG3D_VERSION = "v20240420"
BAG3D_TILE_INDEX_URL = f"{BAG3D_BASE_URL}/{BAG3D_VERSION}/tile_index.fgb"
BAG3D_API_URL = "https://api.3dbag.nl"

BAG_WFS_URL = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG_WFS_PAGE = 1000  # PDOK serves at most 1000 features per request
BAG_UNIT_CELL_M = 2000  # edge of one cached WFS request cell in EPSG:28992
BAG_UNIT_STATUSES_IN_USE = (
    "Verblijfsobject in gebruik",
    "Verblijfsobject in gebruik (niet ingemeten)",
    "Verbouwing verblijfsobject",
)


def count_units_in_footprints(gdf_buildings, units):
    """Add bag_units and bag_dwellings: BAG units inside each footprint, all and those with woonfunctie."""
    import geopandas as gpd

    hits = gpd.sjoin(units[["gebruiksdoel", "geometry"]], gdf_buildings[["geometry"]], predicate="within")
    building = hits["index_right"]
    is_dwelling = hits["gebruiksdoel"].str.contains("woonfunctie", na=False)
    gdf = gdf_buildings.copy()
    gdf["bag_units"] = building.value_counts().reindex(gdf.index, fill_value=0).astype(int)
    gdf["bag_dwellings"] = is_dwelling.groupby(building).sum().reindex(gdf.index, fill_value=0).astype(int)
    return gdf


def apply_bag_dwellings(gdf):
    """Take Occupants from BAG where a footprint holds dwellings, and drop footprints holding no BAG unit.

    BAG registers every unit, so a footprint without one is an outbuilding. A footprint with
    only non-residential units keeps its estimated Occupants.
    """
    gdf = gdf.copy()
    registered = gdf["bag_dwellings"] > 0
    gdf.loc[registered, "Occupants"] = gdf.loc[registered, "bag_dwellings"]
    return gdf[gdf["bag_units"] > 0]


class BAG3DEnricher:
    """Enrich building data with 3D BAG attributes.

    Downloads 3D BAG tiles that overlap the region of interest and
    spatially joins them to the building footprints, adding:
    - b3_h_max: maximum building height (m)
    - b3_h_min: ground level height (m)
    - b3_floors: number of floors (derived from height)
    - bouwjaar: construction year
    - bag_id: BAG pand identifier
    """

    PAND_LAYER = "pand"
    HEIGHT_LAYER_PRIORITY = ["lod22_2d", "lod13_2d", "lod12_2d"]

    # Canonical output columns expected by pylovo after normalization.
    ENRICH_COLUMNS = [
        "identificatie",         # BAG pand ID
        "bag_id",                # canonical BAG ID for pylovo
        "height_median",         # median roof height
        "height_max",            # max roof height
        "height_ground",         # ground level
        "b3_val3dity_lod12",
        "construction_year",
        "b3_bag_bag_overlap",
        "floors_3dbag",          # number of floors
    ]

    def __init__(self, region_config: Dict[str, Any], cache_dir: Optional[Path] = None):
        """
        Initialize the 3D BAG enricher.

        Args:
            region_config: Region configuration dict
            cache_dir: Directory to cache downloaded tiles
        """
        self.region_config = region_config
        self.cache_dir = cache_dir or (Path(__file__).parent.parent / "cache" / "3dbag")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    def enrich(self, buildings_gpkg: Path, output_gpkg: Optional[Path] = None) -> Path:
        """
        Enrich building footprints with 3D BAG data.

        Args:
            buildings_gpkg: Path to building footprints GPKG
            output_gpkg: Output path (defaults to <input>_enriched.gpkg)

        Returns:
            Path to enriched GPKG file
        """
        import geopandas as gpd

        if output_gpkg is None:
            output_gpkg = buildings_gpkg.parent / f"{buildings_gpkg.stem}_enriched.gpkg"

        logger.info(f"Enriching buildings with 3D BAG data: {buildings_gpkg}")

        # Load building footprints
        gdf_buildings = gpd.read_file(buildings_gpkg)
        if gdf_buildings.empty:
            logger.warning("No buildings to enrich")
            return buildings_gpkg

        logger.info(f"Loaded {len(gdf_buildings)} buildings")

        # Download 3D BAG tiles for this region
        gdf_3dbag = self._download_region_tiles(gdf_buildings)
        if gdf_3dbag is None or gdf_3dbag.empty:
            logger.warning("No 3D BAG data available for this region")
            return buildings_gpkg

        # Spatial join: match buildings to 3D BAG footprints
        gdf_enriched = self._spatial_join(gdf_buildings, gdf_3dbag)
        gdf_enriched = self._add_bag_unit_counts(gdf_enriched)

        # Save enriched output
        if output_gpkg.exists():
            output_gpkg.unlink()
        gdf_enriched.to_file(output_gpkg, driver="GPKG")
        logger.info(f"Saved enriched buildings to {output_gpkg}")

        # Log enrichment statistics
        self._log_stats(gdf_buildings, gdf_enriched)

        return output_gpkg

    def _download_region_tiles(self, gdf_buildings):
        """Download 3D BAG tiles that overlap the building extent."""
        import geopandas as gpd
        import pandas as pd

        # Get bounding box of buildings
        bounds = gdf_buildings.total_bounds  # [minx, miny, maxx, maxy]
        logger.info(f"Building extent: {bounds}")

        # Try tile-based download first
        try:
            gdf_tiles = self._get_tile_index()
            if gdf_tiles is not None:
                return self._download_overlapping_tiles(gdf_tiles, gdf_buildings)
        except Exception as e:
            logger.warning(f"Tile-based download failed: {e}")

        # Fallback: use WFS API with bounding box
        try:
            return self._download_via_wfs(bounds, gdf_buildings.crs)
        except Exception as e:
            logger.warning(f"WFS download also failed: {e}")
            return None

    def _get_tile_index(self):
        """Download and cache the 3D BAG tile index."""
        import geopandas as gpd

        tile_index_path = self.cache_dir / "tile_index.fgb"

        if not tile_index_path.exists():
            logger.info("Downloading 3D BAG tile index...")
            response = self.session.get(BAG3D_TILE_INDEX_URL, timeout=60)
            response.raise_for_status()
            with open(tile_index_path, "wb") as f:
                f.write(response.content)
            logger.info(f"Tile index saved to {tile_index_path}")

        return gpd.read_file(tile_index_path)

    def _download_overlapping_tiles(self, gdf_tiles, gdf_buildings):
        """Download tiles that overlap with building footprints."""
        import geopandas as gpd
        import pandas as pd

        # Ensure same CRS
        if gdf_tiles.crs != gdf_buildings.crs:
            gdf_tiles = gdf_tiles.to_crs(gdf_buildings.crs)

        # Find overlapping tiles
        buildings_bbox = gdf_buildings.unary_union.envelope
        overlapping = gdf_tiles[gdf_tiles.intersects(buildings_bbox)]

        if overlapping.empty:
            logger.warning("No 3D BAG tiles overlap with buildings")
            return None

        logger.info(f"Found {len(overlapping)} overlapping 3D BAG tiles")

        # Download each tile
        all_parts = []
        for _, tile in overlapping.iterrows():
            tile_id = tile.get("tile_id", tile.get("id", "unknown"))
            gpkg_url = tile.get("gpkg_download")
            try:
                gdf_tile = self._download_tile(tile_id, gpkg_url)
                if gdf_tile is not None and not gdf_tile.empty:
                    all_parts.append(gdf_tile)
            except Exception as e:
                logger.warning(f"Failed to download tile {tile_id}: {e}")

        if not all_parts:
            return None

        gdf_combined = gpd.GeoDataFrame(
            pd.concat(all_parts, ignore_index=True),
            crs=all_parts[0].crs,
        )
        logger.info(f"Loaded {len(gdf_combined)} 3D BAG features from {len(all_parts)} tiles")
        return gdf_combined

    def _download_tile(self, tile_id: str, gpkg_url: Optional[str] = None):
        """Download a single 3D BAG tile as GPKG."""
        import geopandas as gpd
        import pandas as pd

        # Use tile_id with slashes replaced for safe filesystem caching
        safe_tile_id = tile_id.replace("/", "-")
        tile_cache = self.cache_dir / f"{safe_tile_id}.gpkg"

        if tile_cache.exists():
            try:
                logger.debug(f"Using cached tile: {tile_id}")
                return self._load_tile_enrichment(tile_cache)
            except Exception:
                logger.warning(f"Corrupted cache for tile {tile_id}, re-downloading")
                tile_cache.unlink()

        # Use the download URL from tile index, or construct fallback
        if gpkg_url:
            tile_url = gpkg_url
        else:
            tile_url = f"{BAG3D_BASE_URL}/{BAG3D_VERSION}/tiles/{tile_id}/{safe_tile_id}.gpkg"
        logger.info(f"Downloading 3D BAG tile: {tile_id}")

        zip_path = self.cache_dir / f"{safe_tile_id}.gpkg.zip"
        try:
            response = self.session.get(tile_url, timeout=120)
            response.raise_for_status()

            data = response.content

            # Detect format by magic bytes, not headers
            if data[:2] == b'\x1f\x8b':  # gzip magic number
                data = gzip.decompress(data)

            if data[:4] == b'PK\x03\x04':  # zip magic number
                with open(zip_path, "wb") as f:
                    f.write(data)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    gpkg_names = [n for n in zf.namelist() if n.lower().endswith(".gpkg")]
                    if not gpkg_names:
                        raise ValueError(f"No .gpkg file found in ZIP for tile {tile_id}")
                    with zf.open(gpkg_names[0], "r") as src, open(tile_cache, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                zip_path.unlink(missing_ok=True)
            else:
                with open(tile_cache, "wb") as f:
                    f.write(data)

            try:
                return self._load_tile_enrichment(tile_cache)
            except Exception:
                tile_cache.unlink(missing_ok=True)
                raise

        except Exception as e:
            logger.warning(f"Could not download tile {tile_id}: {e}")
            tile_cache.unlink(missing_ok=True)
            zip_path.unlink(missing_ok=True)
            return None

    def _load_tile_enrichment(self, tile_cache: Path):
        """
        Load enrichment attributes from a single 3D BAG tile.

        Reads `pand` as base layer and augments it with roof/height fields
        from the best available LoD 2D layer.
        """
        import geopandas as gpd

        gdf_pand = gpd.read_file(tile_cache, layer=self.PAND_LAYER)
        if gdf_pand.empty:
            return gdf_pand

        # Normalize construction-year naming differences across 3D BAG versions.
        if "oorspronkelijkbouwjaar" in gdf_pand.columns and "oorspronkelijk_bouwjaar" not in gdf_pand.columns:
            gdf_pand["oorspronkelijk_bouwjaar"] = gdf_pand["oorspronkelijkbouwjaar"]

        gdf_height = self._load_tile_height_layer(tile_cache)
        if gdf_height is None or gdf_height.empty:
            return gdf_pand

        if "identificatie" not in gdf_pand.columns or "identificatie" not in gdf_height.columns:
            return gdf_pand

        merge_cols = [c for c in ["identificatie", "b3_h_50p", "b3_h_max", "b3_h_min"] if c in gdf_height.columns]
        if merge_cols == ["identificatie"]:
            return gdf_pand

        gdf_height = gdf_height[merge_cols].copy()
        gdf_height = gdf_height.groupby("identificatie", as_index=False).agg({
            c: "mean" for c in merge_cols if c != "identificatie"
        })

        return gdf_pand.merge(gdf_height, on="identificatie", how="left")

    def _load_tile_height_layer(self, tile_cache: Path):
        """Load first available LoD 2D layer containing per-building height metrics."""
        import geopandas as gpd

        for layer_name in self.HEIGHT_LAYER_PRIORITY:
            try:
                gdf = gpd.read_file(tile_cache, layer=layer_name)
            except Exception:
                continue
            if gdf is not None and not gdf.empty and "identificatie" in gdf.columns:
                # Only useful if at least one expected height column exists.
                if any(col in gdf.columns for col in ("b3_h_50p", "b3_h_max", "b3_h_min")):
                    return gdf
        return None

    def _download_via_wfs(self, bounds, target_crs):
        """Download 3D BAG data via WFS for a bounding box."""
        import geopandas as gpd

        bbox_str = f"{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}"
        wfs_url = (
            f"{BAG3D_API_URL}/collections/pand/items"
            f"?bbox={bbox_str}&limit=10000&f=json"
        )

        logger.info(f"Downloading 3D BAG via API (bbox: {bbox_str})...")
        response = self.session.get(wfs_url, timeout=120)
        response.raise_for_status()

        gdf = gpd.GeoDataFrame.from_features(
            response.json().get("features", []),
            crs="EPSG:28992",  # 3D BAG uses RD New
        )

        if target_crs and gdf.crs != target_crs:
            gdf = gdf.to_crs(target_crs)

        logger.info(f"Downloaded {len(gdf)} features from 3D BAG API")
        return gdf

    def _spatial_join(self, gdf_buildings, gdf_3dbag):
        """Join 3D BAG attributes to building footprints."""
        import geopandas as gpd
        import pandas as pd

        # Ensure same CRS
        if gdf_3dbag.crs != gdf_buildings.crs:
            gdf_3dbag = gdf_3dbag.to_crs(gdf_buildings.crs)

        gdf_3dbag = self._normalize_3dbag_columns(gdf_3dbag)

        # Select only columns we need from 3D BAG
        keep_cols = [c for c in self.ENRICH_COLUMNS if c in gdf_3dbag.columns]
        keep_cols.append("geometry")
        gdf_3dbag_slim = gdf_3dbag[keep_cols].copy()

        # Use centroid of buildings for point-in-polygon join to 3D BAG
        gdf_buildings = gdf_buildings.copy()
        orig_geom = gdf_buildings.geometry.copy()
        gdf_buildings.geometry = gdf_buildings.geometry.centroid

        # Spatial join
        joined = gpd.sjoin(
            gdf_buildings,
            gdf_3dbag_slim,
            how="left",
            predicate="within",
        )

        # Remove duplicates (keep first match based on original index)
        joined = joined[~joined.index.duplicated(keep="first")]

        # Restore original polygon geometry
        joined = joined.set_geometry(orig_geom.loc[joined.index])
        joined = joined.drop(columns=["index_right"], errors="ignore")

        # Ensure Floors column exists and is numeric
        if "Floors" in joined.columns:
            joined["Floors"] = pd.to_numeric(joined["Floors"], errors="coerce")
        else:
            joined["Floors"] = pd.NA

        # Update Floors column from 3D BAG if available
        if "floors_3dbag" in joined.columns:
            has_floors = joined["floors_3dbag"].notna() & (joined["floors_3dbag"] > 0)
            missing_floors = joined["Floors"].isna() | (joined["Floors"] <= 0)
            fill_mask = has_floors & missing_floors
            if fill_mask.any():
                joined.loc[fill_mask, "Floors"] = joined.loc[fill_mask, "floors_3dbag"].astype(int)

        # Final fallback to 1 for any remaining missing floors
        final_missing = joined["Floors"].isna() | (joined["Floors"] <= 0)
        if final_missing.any():
            joined.loc[final_missing, "Floors"] = 1
        
        joined["Floors"] = joined["Floors"].astype(int)

        # Update construction year
        if "construction_year" in joined.columns:
            joined["construction_year"] = pd.to_numeric(joined["construction_year"], errors="coerce")
            has_year = joined["construction_year"].notna() & (joined["construction_year"] > 0)
            
            # Map to standard OSM column names
            target_cols = ["construction_year", "start_date", "Constructi"]
            for col in target_cols:
                if col in joined.columns:
                    # Only fill where original value is missing
                    missing_val = joined[col].isna() | (joined[col].astype(str).str.strip() == "")
                    fill_mask = has_year & missing_val
                    if fill_mask.any():
                        joined.loc[fill_mask, col] = joined.loc[fill_mask, "construction_year"].astype(int).astype(str)
                        logger.info(f"Filled 3D BAG construction year into {col} for {fill_mask.sum()} buildings")

        return joined

    @staticmethod
    def _normalize_3dbag_columns(gdf_3dbag):
        """
        Normalize source-specific 3D BAG column variants to canonical pylovo fields.
        """
        gdf = gdf_3dbag.copy()

        if "bag_id" not in gdf.columns and "identificatie" in gdf.columns:
            gdf["bag_id"] = gdf["identificatie"]

        if "oorspronkelijkbouwjaar" in gdf.columns and "oorspronkelijk_bouwjaar" not in gdf.columns:
            gdf["oorspronkelijk_bouwjaar"] = gdf["oorspronkelijkbouwjaar"]

        if "construction_year" not in gdf.columns:
            if "oorspronkelijk_bouwjaar" in gdf.columns:
                gdf["construction_year"] = gdf["oorspronkelijk_bouwjaar"]
            elif "oorspronkelijkbouwjaar" in gdf.columns:
                gdf["construction_year"] = gdf["oorspronkelijkbouwjaar"]

        if "floors_3dbag" not in gdf.columns and "b3_bouwlagen" in gdf.columns:
            gdf["floors_3dbag"] = gdf["b3_bouwlagen"]

        # Roof/ground height aliases:
        # - older tiles: b3_h_dak_50p / b3_h_dak_max / b3_h_maaiveld
        # - newer lod*_2d: b3_h_50p / b3_h_max / b3_h_min
        if "height_median" not in gdf.columns:
            if "b3_h_dak_50p" in gdf.columns:
                gdf["height_median"] = gdf["b3_h_dak_50p"]
            elif "b3_h_50p" in gdf.columns:
                gdf["height_median"] = gdf["b3_h_50p"]

        if "height_max" not in gdf.columns:
            if "b3_h_dak_max" in gdf.columns:
                gdf["height_max"] = gdf["b3_h_dak_max"]
            elif "b3_h_max" in gdf.columns:
                gdf["height_max"] = gdf["b3_h_max"]

        if "height_ground" not in gdf.columns:
            if "b3_h_maaiveld" in gdf.columns:
                gdf["height_ground"] = gdf["b3_h_maaiveld"]
            elif "b3_h_min" in gdf.columns:
                gdf["height_ground"] = gdf["b3_h_min"]

        return gdf

    def _add_bag_unit_counts(self, gdf_buildings):
        """Add bag_units and bag_dwellings, or leave both out when the units cannot be fetched.

        A partial download would read as buildings without units, which apply_bag_dwellings drops.
        """
        try:
            units = self._fetch_bag_units(gdf_buildings)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"BAG unit download failed, dwellings stay estimated: {e}")
            return gdf_buildings
        gdf = count_units_in_footprints(gdf_buildings, units)
        logger.info(
            "BAG units: %d in use, %d dwellings in %d of %d buildings",
            len(units), int(gdf["bag_dwellings"].sum()), int((gdf["bag_dwellings"] > 0).sum()), len(gdf),
        )
        return gdf

    def _fetch_bag_units(self, gdf_buildings):
        """BAG units in use over every cell a building touches, as points in the buildings' CRS."""
        import geopandas as gpd

        bounds = gdf_buildings.to_crs("EPSG:28992").bounds // BAG_UNIT_CELL_M
        cells = {
            (x, y)
            for x0, y0, x1, y1 in bounds.astype(int).itertuples(index=False)
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)
        }
        logger.info(f"Fetching BAG units for {len(cells)} cells of {BAG_UNIT_CELL_M} m")
        features = {}
        for x, y in sorted(cells):
            for feature in self._fetch_bag_unit_cell(x, y):
                features[feature["properties"]["identificatie"]] = feature
        units = gpd.GeoDataFrame.from_features(
            list(features.values()), crs="EPSG:28992", columns=["status", "gebruiksdoel", "geometry"]
        )
        units = units[units["status"].isin(BAG_UNIT_STATUSES_IN_USE)]
        return units.to_crs(gdf_buildings.crs)

    def _fetch_bag_unit_cell(self, x: int, y: int) -> list:
        """All BAG units in one cell from the PDOK WFS, cached as GeoJSON features."""
        cache = self.cache_dir / "bag_units" / f"{x}_{y}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        size = BAG_UNIT_CELL_M
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "bag:verblijfsobject", "outputFormat": "application/json",
            "srsName": "EPSG:28992", "count": BAG_WFS_PAGE,
            "bbox": f"{x * size},{y * size},{(x + 1) * size},{(y + 1) * size},EPSG:28992",
        }
        features = []
        while True:
            response = self.session.get(BAG_WFS_URL, params={**params, "startIndex": len(features)}, timeout=120)
            response.raise_for_status()
            page = response.json()["features"]
            features += [
                {"type": "Feature", "geometry": f["geometry"],
                 "properties": {k: f["properties"].get(k) for k in ("identificatie", "status", "gebruiksdoel")}}
                for f in page
            ]
            if len(page) < BAG_WFS_PAGE:
                break
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(features))
        return features

    def _log_stats(self, gdf_original, gdf_enriched):
        """Log enrichment statistics."""
        total = len(gdf_enriched)
        if total == 0:
            return

        stats = {}
        for col in ["bag_id", "construction_year", "floors_3dbag", "height_max"]:
            if col in gdf_enriched.columns:
                matched = gdf_enriched[col].notna().sum()
                stats[col] = f"{matched}/{total} ({100*matched/total:.1f}%)"

        logger.info(f"3D BAG enrichment stats: {stats}")
