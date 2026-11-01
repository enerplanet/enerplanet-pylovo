"""
EUBUCCO Enrichment Module

Enriches building data with attributes from the EUBUCCO dataset
(https://eubucco.com), a pan-European building characteristics database.

Provides:
- Number of floors (storeys)
- Building height (m)
- Construction year (age)
- Building type (residential / non-residential)

Data source: https://eubucco.com
License: MIT (commercial use permitted)
Attribution: EUBUCCO by ai4up (Wagner et al., 2023)
NUTS 2 outlines for choosing files: GISCO, © EuroGeographics for the administrative boundaries
"""

import logging
from pathlib import Path
from typing import Dict, Any, Optional
import time

import requests
import pyogrio

logger = logging.getLogger("datapipeline")

# EUBUCCO v0.2 is published as one GeoPackage per NUTS 2 region (EPSG:3035).
EUBUCCO_VERSION = "v0.2"
EUBUCCO_PARTITIONS_URL = f"https://api.eubucco.com/v1/datalake/nuts/{EUBUCCO_VERSION}"

# NUTS 2021 is the edition whose ids match the EUBUCCO v0.2 partitions. The
# 1:10M outlines are coarse, so a region matches within NUTS_MATCH_BUFFER_M.
NUTS2_BOUNDARIES_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_10M_2021_4326_LEVL_2.geojson"
)
NUTS_MATCH_BUFFER_M = 2000

# Country key → NUTS country code
EUBUCCO_COUNTRY_FILES: Dict[str, str] = {
    "germany": "DE",
    "france": "FR",
    "austria": "AT",
    "spain": "ES",
    "italy": "IT",
    "poland": "PL",
    "czech_republic": "CZ",
    "switzerland": "CH",
}


def gpkg_urls(partitions: list[dict]) -> Dict[str, str]:
    """Map NUTS 2 id to GeoPackage download URL from the EUBUCCO partitions listing."""
    urls = {}
    for partition in partitions:
        for f in partition.get("files", []):
            if f.get("key", "").endswith(".gpkg"):
                urls[partition["nuts_id"]] = f["download_url"]
    return urls


def select_nuts2(boundaries, country_code: str, bounds_3035) -> list[str]:
    """Return the NUTS 2 ids of one country whose outline meets the buffered bounds (EPSG:3035)."""
    from shapely.geometry import box

    area = box(*bounds_3035).buffer(NUTS_MATCH_BUFFER_M)
    regions = boundaries[boundaries["CNTR_CODE"] == country_code].to_crs("EPSG:3035")
    return sorted(regions.loc[regions.geometry.intersects(area), "NUTS_ID"])


class EUBUCCOEnricher:
    """Enrich building data with EUBUCCO attributes (floors, height, age).

    Downloads the EUBUCCO NUTS 2 GeoPackages covering the buildings and
    spatially joins them to existing building footprints, adding:
    - floors_eubucco: number of above-ground floors
    - height_eubucco: building height (m)
    - age_eubucco: construction year
    - type_eubucco: building usage type
    """

    ENRICH_COLUMNS = [
        "floors",
        "height",
        "construction_year",
        "type",
    ]

    # Columns renamed on output to avoid clashing with existing pylovo fields
    OUTPUT_RENAME = {
        "floors": "floors_eubucco",
        "height": "height_eubucco",
        "construction_year": "age_eubucco",
        "type": "type_eubucco",
    }

    # Process-local cache: avoid retrying the same failed partition download for
    # every GPKG in one pipeline run (e.g. Res/Oth/raw/POI files).
    _FAILED_DOWNLOADS: Dict[str, str] = {}
    _PARTITION_URLS: Optional[Dict[str, str]] = None

    def __init__(self, region_config: Dict[str, Any], cache_dir: Optional[Path] = None):
        self.region_config = region_config
        self.country = region_config.get("country", "").lower().strip()
        self.cache_dir = cache_dir or (Path(__file__).parent.parent / "cache" / "eubucco")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "pylovo-datapipeline/1.0 (+https://github.com/tum-ens/pylovo)"
        })

    def _partition_urls(self) -> Dict[str, str]:
        """Load the EUBUCCO partitions listing (cached per process)."""
        if self.__class__._PARTITION_URLS is None:
            logger.info(f"Loading EUBUCCO partitions: {EUBUCCO_PARTITIONS_URL}")
            resp = self.session.get(EUBUCCO_PARTITIONS_URL, timeout=60)
            resp.raise_for_status()
            self.__class__._PARTITION_URLS = gpkg_urls(resp.json())
        return self.__class__._PARTITION_URLS

    def _nuts2_ids(self, country_code: str, gdf_buildings) -> list[str]:
        """Return the NUTS 2 regions that cover the buildings."""
        import geopandas as gpd

        boundaries_path = self.cache_dir / EUBUCCO_VERSION / Path(NUTS2_BOUNDARIES_URL).name
        if not boundaries_path.exists():
            self._download(NUTS2_BOUNDARIES_URL, boundaries_path)
        bounds = gdf_buildings.to_crs("EPSG:3035").total_bounds
        return select_nuts2(gpd.read_file(boundaries_path), country_code, bounds)

    @staticmethod
    def _buffered_bbox(bounds, buffer: float = 500):
        """Apply a symmetric buffer to a bbox tuple/list."""
        return (
            bounds[0] - buffer,
            bounds[1] - buffer,
            bounds[2] + buffer,
            bounds[3] + buffer,
        )

    @staticmethod
    def _format_bytes(num_bytes: Optional[float]) -> str:
        """Format byte counts in a human-readable unit."""
        if num_bytes is None:
            return "unknown"
        value = float(num_bytes)
        units = ["B", "KB", "MB", "GB", "TB"]
        unit = units[0]
        for unit in units:
            if value < 1024 or unit == units[-1]:
                break
            value /= 1024.0
        if unit == "B":
            return f"{int(value)} {unit}"
        return f"{value:.1f} {unit}"

    @staticmethod
    def _format_duration(seconds: Optional[float]) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds is None or seconds < 0:
            return "unknown"
        total = int(round(seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _get_default_layer_name(read_target) -> Optional[str]:
        """Return a stable default layer for multi-layer GPKG sources."""
        try:
            layers = pyogrio.list_layers(read_target)
        except Exception:
            return None
        if layers is None:
            return None
        try:
            if len(layers) == 0:
                return None
        except TypeError:
            return None
        first = layers[0]
        if not isinstance(first, (str, bytes)):
            try:
                if len(first) > 0:
                    return str(first[0])
            except TypeError:
                pass
        return str(first)


    def enrich(self, buildings_gpkg: Path, output_gpkg: Optional[Path] = None) -> Path:
        """Enrich building footprints with EUBUCCO data.

        Uses GPKG (with geometry) for spatial join. Falls back to
        height-based floor estimation when floor data is missing.
        """
        import geopandas as gpd

        if output_gpkg is None:
            output_gpkg = buildings_gpkg.parent / f"{buildings_gpkg.stem}_enriched_eubucco.gpkg"

        country_code = EUBUCCO_COUNTRY_FILES.get(self.country)
        if not country_code:
            logger.info(f"EUBUCCO: no data available for country '{self.country}', skipping")
            return buildings_gpkg

        logger.info(f"Enriching buildings with EUBUCCO data ({country_code}): {buildings_gpkg}")

        gdf_buildings = gpd.read_file(buildings_gpkg)
        if gdf_buildings.empty:
            logger.warning("No buildings to enrich")
            return buildings_gpkg

        logger.info(f"Loaded {len(gdf_buildings)} buildings")

        gdf_eubucco = self._load_eubucco_gpkg(country_code, gdf_buildings)
        if gdf_eubucco is None or gdf_eubucco.empty:
            logger.warning("No EUBUCCO data loaded for this region")
            return buildings_gpkg

        gdf_enriched = self._spatial_join(gdf_buildings, gdf_eubucco)

        if output_gpkg.exists():
            output_gpkg.unlink()
        gdf_enriched.to_file(output_gpkg, driver="GPKG")
        logger.info(f"Saved EUBUCCO-enriched buildings to {output_gpkg}")

        self._log_stats(gdf_buildings, gdf_enriched)
        return output_gpkg

    def _load_eubucco_gpkg(self, country_code: str, gdf_buildings):
        """Load the EUBUCCO buildings of every NUTS 2 region that covers the buildings."""
        import geopandas as gpd
        import pandas as pd

        try:
            nuts_ids = self._nuts2_ids(country_code, gdf_buildings)
            urls = self._partition_urls()
        except Exception as e:
            logger.warning(f"Failed to resolve EUBUCCO partitions (optional enrichment): {e}")
            return None
        logger.info(f"EUBUCCO NUTS 2 regions for these buildings: {nuts_ids}")

        frames = []
        for nuts_id in nuts_ids:
            if nuts_id not in urls:
                logger.warning(f"EUBUCCO {EUBUCCO_VERSION} has no partition for NUTS region {nuts_id}")
                continue
            gpkg_path = self.cache_dir / EUBUCCO_VERSION / f"{nuts_id}.gpkg"
            if not gpkg_path.exists():
                if nuts_id in self._FAILED_DOWNLOADS:
                    logger.warning(
                        f"Skipping EUBUCCO download retry for {nuts_id} in this run "
                        f"(previous failure: {self._FAILED_DOWNLOADS[nuts_id]})"
                    )
                    continue
                try:
                    self._download(urls[nuts_id], gpkg_path)
                except Exception as e:
                    # EUBUCCO is optional enrichment; keep the pipeline running.
                    self._FAILED_DOWNLOADS[nuts_id] = str(e)
                    logger.warning(f"Failed to download EUBUCCO {nuts_id} (optional enrichment): {e}")
                    continue
            frames.append(self._read_eubucco_region(gpkg_path, gdf_buildings))

        frames = [f for f in frames if not f.empty]
        if not frames:
            return None
        return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)

    def _download(self, url: str, target: Path) -> None:
        """Download url to target, resuming a partial .tmp file when the server allows it."""
        target.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Downloading {url}")
        tmp_path = Path(str(target) + ".tmp")

        # Resume partial downloads if a .tmp file exists
        resume_from = 0
        file_mode = "wb"
        headers = {}
        if tmp_path.exists():
            resume_from = tmp_path.stat().st_size
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
                file_mode = "ab"
                logger.info(
                    "Resuming download from %s (partial .tmp file found)",
                    self._format_bytes(resume_from),
                )

        with self.session.get(url, stream=True, timeout=600, headers=headers) as r:
            # 206 = partial content (resume supported), 200 = full content
            if r.status_code == 200 and resume_from > 0:
                # Server doesn't support Range; restart from scratch
                logger.info("Server does not support resume; restarting download")
                resume_from = 0
                file_mode = "wb"
            elif r.status_code not in (200, 206):
                r.raise_for_status()

            total_bytes = None
            content_length = r.headers.get("Content-Length")
            if content_length:
                try:
                    total_bytes = int(content_length) + resume_from
                except (TypeError, ValueError):
                    total_bytes = None
            if total_bytes:
                logger.info(
                    "EUBUCCO download size: %s total",
                    self._format_bytes(total_bytes),
                )
            else:
                logger.info("EUBUCCO download size: unknown (no Content-Length header)")

            downloaded_bytes = resume_from
            start_ts = time.monotonic()
            last_log_ts = start_ts
            last_logged_percent = -1
            with open(tmp_path, file_mode) as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    now = time.monotonic()
                    elapsed = max(now - start_ts, 1e-6)
                    percent = int((downloaded_bytes * 100) / total_bytes) if total_bytes else None
                    should_log = (now - last_log_ts) >= 30
                    if percent is not None and percent >= (last_logged_percent + 10):
                        should_log = True

                    if should_log:
                        speed_bps = downloaded_bytes / elapsed
                        speed_str = f"{self._format_bytes(speed_bps)}/s"
                        if total_bytes:
                            remaining_bytes = max(total_bytes - downloaded_bytes, 0)
                            eta_seconds = (remaining_bytes / speed_bps) if speed_bps > 0 else None
                            logger.info(
                                "EUBUCCO download progress: %s / %s (%0.1f%%), remaining %s, speed %s, ETA %s",
                                self._format_bytes(downloaded_bytes),
                                self._format_bytes(total_bytes),
                                (downloaded_bytes * 100.0 / total_bytes),
                                self._format_bytes(remaining_bytes),
                                speed_str,
                                self._format_duration(eta_seconds),
                            )
                            last_logged_percent = percent if percent is not None else last_logged_percent
                        else:
                            logger.info(
                                "EUBUCCO download progress: %s downloaded, total unknown, speed %s",
                                self._format_bytes(downloaded_bytes),
                                speed_str,
                            )
                        last_log_ts = now

            total_elapsed = max(time.monotonic() - start_ts, 0.0)
            logger.info(
                "EUBUCCO download complete: %s in %s",
                self._format_bytes(downloaded_bytes),
                self._format_duration(total_elapsed),
            )
            tmp_path.rename(target)
        logger.info(f"Saved {target}")

    def _read_eubucco_region(self, read_target, gdf_buildings):
        """Read EUBUCCO data from a source file, filtered to the buildings bbox."""
        import geopandas as gpd

        bounds = gdf_buildings.total_bounds  # [minx, miny, maxx, maxy]
        read_kwargs = {}
        if str(read_target).lower().endswith(".gpkg"):
            layer_name = self._get_default_layer_name(read_target)
            if layer_name:
                read_kwargs["layer"] = layer_name

        # EUBUCCO uses EPSG:3035 (ETRS89-LAEA); building data may use EPSG:4326
        # Convert bbox to EUBUCCO CRS for efficient spatial filtering
        try:
            from shapely.geometry import box
            bbox_geom = gpd.GeoDataFrame(
                geometry=[box(*bounds)], crs=gdf_buildings.crs
            ).to_crs("EPSG:3035")
            eubucco_bounds = bbox_geom.total_bounds

            # Add buffer (~500m) to avoid edge clipping
            bbox_filter = self._buffered_bbox(eubucco_bounds, buffer=500)
            logger.debug("Reading EUBUCCO within bbox (EPSG:3035): %s", bbox_filter)
            gdf = gpd.read_file(read_target, bbox=bbox_filter, **read_kwargs)
        except Exception as e:
            logger.warning(f"Bbox filter failed ({e}), reading full file")
            gdf = gpd.read_file(read_target, **read_kwargs)

        if gdf.empty:
            return gdf

        # Reproject to match buildings CRS
        if gdf.crs != gdf_buildings.crs:
            gdf = gdf.to_crs(gdf_buildings.crs)

        logger.info(f"Loaded {len(gdf)} EUBUCCO buildings in region")
        return gdf

    def _spatial_join(self, gdf_buildings, gdf_eubucco):
        """Join EUBUCCO attributes to building footprints via centroid spatial join."""
        import geopandas as gpd

        if gdf_eubucco.crs != gdf_buildings.crs:
            gdf_eubucco = gdf_eubucco.to_crs(gdf_buildings.crs)

        # Keep only enrichment columns + geometry
        keep_cols = [c for c in self.ENRICH_COLUMNS if c in gdf_eubucco.columns]
        keep_cols.append("geometry")
        gdf_slim = gdf_eubucco[keep_cols].copy()

        # Rename to avoid column clashes
        rename_map = {k: v for k, v in self.OUTPUT_RENAME.items() if k in gdf_slim.columns}
        gdf_slim = gdf_slim.rename(columns=rename_map)

        # Centroid-based spatial join
        gdf_buildings = gdf_buildings.copy()
        orig_geom = gdf_buildings.geometry.copy()
        gdf_buildings.geometry = gdf_buildings.geometry.centroid

        joined = gpd.sjoin(
            gdf_buildings,
            gdf_slim,
            how="left",
            predicate="intersects",
        )

        # Deduplicate (keep first match)
        joined = joined[~joined.index.duplicated(keep="first")]
        joined = joined.set_geometry(orig_geom.loc[joined.index])
        joined = joined.drop(columns=["index_right"], errors="ignore")

        # Coerce EUBUCCO numeric columns (source data may contain strings)
        import pandas as pd
        for col in ["floors_eubucco", "height_eubucco", "age_eubucco"]:
            if col in joined.columns:
                joined[col] = pd.to_numeric(joined[col], errors="coerce")

        # Ensure Floors column exists and is numeric, preserving existing valid values
        if "Floors" in joined.columns:
            # Save original floors before coercion so we can detect corruption
            orig_floors = joined["Floors"].copy()
            joined["Floors"] = pd.to_numeric(joined["Floors"], errors="coerce")
            # Restore any values that were valid integers but got coerced to NaN
            restored = pd.to_numeric(orig_floors, errors="coerce")
            restore_mask = joined["Floors"].isna() & restored.notna() & (restored > 0)
            if restore_mask.any():
                joined.loc[restore_mask, "Floors"] = restored[restore_mask]
        else:
            joined["Floors"] = pd.NA

        # 1. Update Floors from EUBUCCO floors if available and better
        if "floors_eubucco" in joined.columns:
            has_eubucco_floors = joined["floors_eubucco"].notna() & (joined["floors_eubucco"] > 0)
            # Only fill where OSM floors are missing or invalid
            missing_floors = joined["Floors"].isna() | (joined["Floors"] <= 0)
            fill_mask = has_eubucco_floors & missing_floors
            if fill_mask.any():
                joined.loc[fill_mask, "Floors"] = joined.loc[fill_mask, "floors_eubucco"].round().astype(int)

        # 2. Estimate floors from height where both OSM and EUBUCCO floors are missing
        if "height_eubucco" in joined.columns:
            still_missing = joined["Floors"].isna() | (joined["Floors"] <= 0)
            has_height = joined["height_eubucco"].notna() & (joined["height_eubucco"] > 0)
            estimate_mask = still_missing & has_height
            if estimate_mask.any():
                # Typical German floor height ~3.0m
                estimated = (joined.loc[estimate_mask, "height_eubucco"] / 3.0).round().clip(lower=1).astype(int)
                joined.loc[estimate_mask, "Floors"] = estimated
                logger.info(f"Estimated floors from EUBUCCO height for {estimate_mask.sum()} buildings")

        # 3. Final fallback to 2 for any remaining buildings with missing floors
        # (most buildings have at least 2 floors; 1-floor fallback was too aggressive)
        final_missing = joined["Floors"].isna() | (joined["Floors"] <= 0)
        if final_missing.any():
            joined.loc[final_missing, "Floors"] = 2

        joined["Floors"] = joined["Floors"].astype(int)

        # Fill construction year where missing
        if "age_eubucco" in joined.columns:
            has_age = joined["age_eubucco"].notna() & (joined["age_eubucco"] > 0)

            # Always ensure the canonical internal column is populated/created
            if "construction_year" not in joined.columns:
                joined["construction_year"] = pd.NA

            # Standard names for construction year in OSM and pipeline outputs
            target_cols = ["construction_year", "start_date", "Constructi"]

            for col in target_cols:
                if col in joined.columns:
                    # Fill if column exists and value is missing/empty
                    missing_mask = joined[col].isna() | (joined[col].astype(str).str.strip() == "")
                    fill_mask = has_age & missing_mask
                    if fill_mask.any():
                        joined.loc[fill_mask, col] = joined.loc[fill_mask, "age_eubucco"].round().astype(int).astype(str)
                        logger.info(f"Filled EUBUCCO construction year into {col} for {fill_mask.sum()} buildings")
        # Fill heights where missing
        if "height_eubucco" in joined.columns:
            has_h = joined["height_eubucco"].notna() & (joined["height_eubucco"] > 0)
            if "height_max" not in joined.columns or joined["height_max"].isna().all():
                joined.loc[has_h, "height_max"] = joined.loc[has_h, "height_eubucco"]
            else:
                missing_h = joined["height_max"].isna()
                joined.loc[missing_h & has_h, "height_max"] = joined.loc[missing_h & has_h, "height_eubucco"]

        return joined

    def _log_stats(self, gdf_original, gdf_enriched):
        """Log enrichment statistics."""
        total = len(gdf_enriched)
        if total == 0:
            return

        stats = {}
        for col in ["floors_eubucco", "height_eubucco", "age_eubucco", "type_eubucco"]:
            if col in gdf_enriched.columns:
                matched = gdf_enriched[col].notna().sum()
                stats[col] = f"{matched}/{total} ({100 * matched / total:.1f}%)"

        # Also report how many buildings got floors filled
        if "Floors" in gdf_enriched.columns:
            has_floors = (gdf_enriched["Floors"].notna() & (gdf_enriched["Floors"] > 0)).sum()
            stats["Floors_filled"] = f"{has_floors}/{total} ({100 * has_floors / total:.1f}%)"

        logger.info(f"EUBUCCO enrichment stats: {stats}")
