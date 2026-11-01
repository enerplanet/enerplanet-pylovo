"""
Building data downloader from Geofabrik/OpenStreetMap.
"""

import os
import logging
import shutil
import subprocess
import zipfile
import tempfile
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, Optional

from .base import BaseDownloader
from ..building_constants import NO_ELECTRICITY_BUILDING_TYPES

logger = logging.getLogger("datapipeline")


class BuildingDownloader(BaseDownloader):
    """Download building data from Geofabrik/OpenStreetMap."""

    @property
    def data_type(self) -> str:
        return "buildings"

    # Priority order for determining f_class from OSM tags on building polygons.
    # More specific tags (amenity, shop) take priority over generic 'building' tag.
    USE_TAG_PRIORITY = ['amenity', 'shop', 'office', 'craft', 'leisure', 'tourism', 'healthcare']

    # Tokens that do not carry a meaningful use and should not dominate mixed tags
    NON_INFORMATIVE_TOKENS = frozenset({
        "", "yes", "building", "none", "null", "unknown", "na", "n_a"
    })

    # Canonicalization map to reduce noisy/variant tags into stable f_class keys
    F_CLASS_ALIASES = {
        "semi_detached": "semidetached_house",
        "semi-detached": "semidetached_house",
        "city_hall": "townhall",
        "theater": "theatre",
        "carports": "carport",
    }

    # Residential building types (from OSM 'building' tag)
    RESIDENTIAL_BUILDING_TYPES = frozenset([
        'residential', 'house', 'apartments', 'detached',
        'semidetached_house', 'terrace', 'dormitory', 'bungalow',
        'farm', 'farmhouse', 'cabin', 'yes', 'apartment',
        'townhouse', 'town_house', 'row_house', 'villa',
        'allotment_house', 'houseboat', 'boathouse', 'boat_house',
        'stilt_house', 'conservatory',
    ])

    # Residential amenity/use values that should keep a building classified as residential
    RESIDENTIAL_USE_VALUES = frozenset([
        'dormitory', 'shelter', 'social_facility', 'retirement_home',
        'nursing_home', 'assisted_living',
    ])

    # Building types that do not consume electricity — excluded from output.
    # These are passive, open, or non-habitable structures.
    NO_ELECTRICITY_TYPES = NO_ELECTRICITY_BUILDING_TYPES

    # Generic classes are kept, but deprioritized when a more specific class exists.
    GENERIC_PRIMARY_F_CLASSES = frozenset([
        'yes', 'building', 'residential', 'house', 'unclassified', 'other',
        'apartments', 'apartment', 'detached', 'semidetached_house',
        'terrace', 'townhouse'
    ])

    @classmethod
    def _canonicalize_f_class(cls, value: Optional[str]) -> str:
        """Normalize noisy OSM class strings to a single canonical f_class."""
        if value is None:
            return "yes"

        raw = str(value).strip().lower()
        if not raw:
            return "yes"

        # Normalize separators and keep alnum/_ so values stay DB-friendly
        normalized = re.sub(r"\s+", "_", raw)
        normalized = re.sub(r"[^a-z0-9_,;/\\-]", "", normalized)

        # Split multi-valued classes, then pick the first informative token
        tokens = [t.strip(" _-") for t in re.split(r"[;,/|]+", normalized) if t and t.strip(" _-")]
        if not tokens:
            return "yes"

        mapped_tokens = [cls.F_CLASS_ALIASES.get(t, t) for t in tokens]
        for token in mapped_tokens:
            if token not in cls.NON_INFORMATIVE_TOKENS:
                return token

        return mapped_tokens[0] if mapped_tokens else "yes"

    @classmethod
    @lru_cache(maxsize=100000)
    def _parse_other_tags(cls, other_tags: str) -> Dict[str, str]:
        """Parse OSM other_tags text (hstore-like) into a key/value dict."""
        if not other_tags or not isinstance(other_tags, str):
            return {}

        parsed: Dict[str, str] = {}

        # Typical OSM format from GDAL/OGR: "key"=>"value","key2"=>"value2"
        for key, value in re.findall(r'"([^"]+)"=>"((?:[^"\\]|\\.)*)"', other_tags):
            clean_key = key.strip()
            clean_value = value.replace('\\"', '"').strip()
            if clean_key and clean_value:
                parsed[clean_key] = clean_value

        # Fallback for less-structured forms like key=value,key2=value2
        if not parsed:
            for key, value in re.findall(r'([^=,\s]+)\s*=\s*([^,]+)', other_tags):
                clean_key = key.strip().strip('"')
                clean_value = value.strip().strip('"')
                if clean_key and clean_value:
                    parsed[clean_key] = clean_value

        return parsed

    @classmethod
    def _extract_tag_value(cls, row, tag_key: str) -> Optional[str]:
        """Get a tag value from explicit columns first, then from other_tags."""
        if hasattr(row, "get"):
            direct = row.get(tag_key)
            if direct is not None and isinstance(direct, str) and direct.strip():
                return direct.strip()

            other_tags = row.get("other_tags")
            if other_tags is not None and isinstance(other_tags, str) and other_tags.strip():
                parsed = cls._parse_other_tags(other_tags)
                v = parsed.get(tag_key)
                if v is not None and isinstance(v, str) and v.strip():
                    return v.strip()

        return None

    @classmethod
    def _resolve_best_f_class(cls, row):
        """Pick the most specific f_class from available OSM tag columns.

        Priority: amenity > shop > office > craft > leisure > tourism > healthcare > building.
        Values are read from explicit columns first and then from `other_tags`.
        Returns lowercase stripped string.
        """
        for col in cls.USE_TAG_PRIORITY:
            val = cls._extract_tag_value(row, col)
            if val:
                return cls._canonicalize_f_class(val)
        # Fallback to building tag
        val = cls._extract_tag_value(row, 'building')
        if val:
            return cls._canonicalize_f_class(val)
        return 'yes'

    @classmethod
    def _sanitize_string_column(cls, series):
        """Return a Series where only actual non-empty str values survive; everything else is NaN.

        Mirrors the semantics of _extract_tag_value: isinstance(val, str) and val.strip().
        Numeric, boolean, and whitespace-only values are rejected (set to NaN).
        """
        import numpy as np
        import pandas as pd

        # Start with an object-typed output so mixed or float-backed input columns
        # cannot trigger pandas .str accessor dtype errors.
        out = pd.Series(np.nan, index=series.index, dtype='object')

        # Only keep values that are actual Python str instances.
        is_str = series.map(lambda v: isinstance(v, str))
        if not is_str.any():
            return out

        stripped = series.loc[is_str].astype(str).str.strip()
        out.loc[stripped.index] = stripped.where(stripped.str.len() > 0)
        return out

    @classmethod
    def _resolve_best_f_class_vectorized(cls, df):
        """Vectorized version of _resolve_best_f_class for entire DataFrames.

        Same logic and identical output as the row-by-row version, but uses
        pandas Series operations instead of Python loops.

        Preserves exact semantics of _extract_tag_value:
        - Only actual str values are accepted (not int, float, bool)
        - Whitespace-only strings are rejected
        - other_tags is lower priority than explicit columns for the same tag
        - building is the final fallback after all priority tags
        - _canonicalize_f_class is reused unchanged per value
        """
        import pandas as pd
        import numpy as np

        result = pd.Series('yes', index=df.index, dtype='object')
        resolved = pd.Series(False, index=df.index)

        # Parse other_tags column once in bulk using the existing _parse_other_tags method.
        # This preserves identical parsing rules (hstore format, fallback regex).
        ot_dicts = {}
        if 'other_tags' in df.columns:
            ot_col = df['other_tags']
            has_ot = ot_col.apply(lambda v: isinstance(v, str) and bool(v.strip()))
            if has_ot.any():
                for idx, ot_val in ot_col[has_ot].items():
                    parsed = cls._parse_other_tags(ot_val)
                    if parsed:
                        ot_dicts[idx] = parsed

        # Check columns in priority order: amenity > shop > ... > healthcare > building
        all_tag_cols = cls.USE_TAG_PRIORITY + ['building']

        for col in all_tag_cols:
            if resolved.all():
                break

            unresolved = ~resolved

            # Get values from explicit column — only real non-empty strings
            vals = pd.Series(np.nan, index=df.index, dtype='object')
            if col in df.columns:
                col_data = df.loc[unresolved, col]
                sanitized = cls._sanitize_string_column(col_data)
                has_val = sanitized.notna()
                if has_val.any():
                    vals[has_val[has_val].index] = sanitized[has_val]

            # Fill missing from pre-parsed other_tags (lower priority than explicit columns)
            if ot_dicts:
                needs_ot = unresolved & vals.isna()
                if needs_ot.any():
                    ot_vals = {}
                    for idx in needs_ot[needs_ot].index:
                        parsed = ot_dicts.get(idx)
                        if parsed:
                            v = parsed.get(col)
                            if v and isinstance(v, str) and v.strip():
                                ot_vals[idx] = v.strip()
                    if ot_vals:
                        ot_series = pd.Series(ot_vals, dtype='object')
                        vals[ot_series.index] = ot_series

            # Canonicalize using the original method — no reimplementation
            has_val = unresolved & vals.notna()
            if has_val.any():
                canonicalized = vals[has_val].apply(cls._canonicalize_f_class)
                result[has_val] = canonicalized
                resolved[has_val] = True

        return result

    @classmethod
    def _normalize_f_class_list(cls, values) -> list[str]:
        """Normalize, dedupe, and keep only meaningful class tokens."""
        seen = set()
        out = []
        for value in values:
            fc = cls._canonicalize_f_class(value)
            if not fc:
                continue
            if fc in cls.NON_INFORMATIVE_TOKENS:
                continue
            if fc in seen:
                continue
            seen.add(fc)
            out.append(fc)
        return out

    @classmethod
    def _select_primary_f_class(cls, classes: list[str]) -> str:
        """Select the most specific class while avoiding generic residential defaults."""
        if not classes:
            return "yes"
        prioritized = sorted(
            classes,
            key=lambda fc: (
                1 if fc in cls.GENERIC_PRIMARY_F_CLASSES else 0,
                fc,
            ),
        )
        return prioritized[0]

    def _extract_pois_gpkg(self, full_pbf_path: Path, output_dir: Path) -> Optional[Path]:
        """Extract POIs from full PBF into a GPKG.

        Includes OSM nodes, ways, and relations for key POI tags so we don't
        miss POIs that are mapped as areas instead of points.
        """
        region_name = self.region_config.get("state", self.region_config["country"])
        target_crs = self.region_config.get("crs", "EPSG:3035")
        pois_pbf = output_dir / f"{region_name}_pois.osm.pbf"
        pois_gpkg = output_dir / f"pois_{region_name}.gpkg"

        # Step 1: Extract POI objects (nodes/ways/relations) from full PBF with osmium
        logger.info("Extracting POIs (nodes/ways/relations) from PBF with osmium...")
        poi_keys = ["amenity", "shop", "office", "craft", "leisure", "tourism", "healthcare"]
        tag_filters = []
        for key in poi_keys:
            tag_filters.extend([f"n/{key}", f"w/{key}", f"r/{key}"])

        osmium_cmd = [
            "osmium", "tags-filter",
            str(full_pbf_path),
            *tag_filters,
            "-o", str(pois_pbf),
            "--overwrite"
        ]
        try:
            subprocess.run(osmium_cmd, check=True, capture_output=True)
            logger.info(f"Extracted POIs to {pois_pbf}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"osmium POI extraction failed ({e}), skipping POI join")
            return None

        # Step 2: Convert available OSM layers into one GPKG (separate layers)
        # Layer names from OSM driver: points, lines, multipolygons
        layer_map = [
            ("points", "poi_points"),
            ("lines", "poi_lines"),
            ("multipolygons", "poi_multipolygons"),
        ]
        if pois_gpkg.exists():
            try:
                pois_gpkg.unlink()
            except Exception:
                # If cleanup fails, continue and rely on ogr2ogr flags below.
                pass
        converted_any = False
        for src_layer, out_layer in layer_map:
            ogr_cmd = [
                "ogr2ogr",
                "--config", "OGR_INTERLEAVED_READING", "YES",
                "-f", "GPKG",
                "-t_srs", target_crs,
                str(pois_gpkg),
                str(pois_pbf),
                src_layer,
                "-nln", out_layer,
                "-progress",
            ]
            if converted_any and pois_gpkg.exists():
                ogr_cmd.insert(8, "-update")
            else:
                ogr_cmd.insert(8, "-overwrite")

            try:
                result = subprocess.run(ogr_cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    converted_any = True
                    logger.info(f"Converted POI layer '{src_layer}' to '{out_layer}'")
                else:
                    logger.debug(f"Skipping unavailable POI layer '{src_layer}': {result.stderr.strip()}")
            except Exception as e:
                logger.debug(f"Failed converting POI layer '{src_layer}': {e}")

        if not converted_any:
            logger.warning("POI GPKG conversion failed for all layers; skipping POI join")
            return None

        logger.info(f"POI layers written to {pois_gpkg}")
        return pois_gpkg

    @staticmethod
    def _spatial_join_pois(gdf_buildings, pois_gpkg_path):
        """Spatially join POI points to building polygons.

        For each building, find all POIs (nodes/ways/relations) that fall within it.
        Returns the buildings GeoDataFrame with an added 'poi_f_classes' column
        containing a list of f_class values from contained POIs.
        """
        import pandas as pd
        import geopandas as gpd
        import time

        logger.info(f"Loading POIs from {pois_gpkg_path}...")
        load_started = time.perf_counter()

        def _list_layers(gpkg_path: Path) -> list[str]:
            # Prefer fiona when available; fall back to pyogrio to avoid hard dependency.
            try:
                import fiona  # type: ignore
                return list(fiona.listlayers(gpkg_path))
            except Exception:
                try:
                    import pyogrio  # type: ignore
                    layer_rows = pyogrio.list_layers(gpkg_path)
                    return [str(row[0]) for row in layer_rows]
                except Exception:
                    # Last-resort default names used by _extract_pois_gpkg().
                    return ["poi_points", "poi_lines", "poi_multipolygons"]

        try:
            layers = _list_layers(pois_gpkg_path)
            poi_parts = []
            for layer_idx, layer_name in enumerate(layers, start=1):
                layer_started = time.perf_counter()
                try:
                    gdf_layer = gpd.read_file(pois_gpkg_path, layer=layer_name)
                except Exception as layer_err:
                    logger.debug(f"Skipping unreadable POI layer '{layer_name}': {layer_err}")
                    continue
                if gdf_layer.empty:
                    continue

                keep_cols = [
                    c for c in (BuildingDownloader.USE_TAG_PRIORITY + ['building', 'other_tags'])
                    if c in gdf_layer.columns
                ]
                gdf_layer = gdf_layer[keep_cols + ['geometry']].copy()
                gdf_layer = gdf_layer[gdf_layer.geometry.notnull()].copy()
                if gdf_layer.empty:
                    continue

                # Convert area/line POIs to representative points for point-in-polygon join
                non_point = gdf_layer.geometry.geom_type != "Point"
                if non_point.any():
                    gdf_layer.loc[non_point, "geometry"] = gdf_layer.loc[non_point, "geometry"].representative_point()

                poi_parts.append(gdf_layer)
                logger.info(
                    "Loaded POI layer %s/%s '%s': %s features kept in %.1fs",
                    layer_idx,
                    len(layers),
                    layer_name,
                    len(gdf_layer),
                    time.perf_counter() - layer_started,
                )

            if not poi_parts:
                logger.info("No POIs found")
                return gdf_buildings, None

            gdf_pois = gpd.GeoDataFrame(pd.concat(poi_parts, ignore_index=True), geometry='geometry', crs=poi_parts[0].crs)
        except Exception as e:
            logger.warning(f"Failed to load POIs: {e}")
            return gdf_buildings, None

        if len(gdf_pois) == 0:
            logger.info("No POIs found")
            return gdf_buildings, None

        logger.info(
            "Loaded %s POIs across %s layer(s) in %.1fs, performing spatial join...",
            len(gdf_pois),
            len(poi_parts),
            time.perf_counter() - load_started,
        )

        # Determine f_class for each POI using vectorized resolution
        logger.info(f"Resolving f_class for {len(gdf_pois)} POIs (vectorized)...")
        resolve_started = time.perf_counter()
        gdf_pois = gdf_pois.copy()
        gdf_pois['poi_f_class'] = BuildingDownloader._resolve_best_f_class_vectorized(gdf_pois)
        logger.info(
            "Resolved f_class for %s POIs in %.1fs",
            len(gdf_pois),
            time.perf_counter() - resolve_started,
        )

        # Ensure same CRS
        if gdf_buildings.crs != gdf_pois.crs:
            gdf_pois = gdf_pois.to_crs(gdf_buildings.crs)

        # Add a temporary building index for the join
        gdf_buildings = gdf_buildings.copy()
        gdf_buildings['_bldg_idx'] = range(len(gdf_buildings))

        # Spatial join: find POIs within each building
        join_started = time.perf_counter()
        poi_join_input = gdf_pois[['poi_f_class', 'geometry']]
        building_join_input = gdf_buildings[['_bldg_idx', 'geometry']]
        chunk_size = 200_000

        if len(poi_join_input) > chunk_size:
            num_chunks = (len(poi_join_input) + chunk_size - 1) // chunk_size
            logger.info(
                "Running spatial join in %s chunks of up to %s POIs...",
                num_chunks,
                chunk_size,
            )
            joined_parts = []
            joined_rows = 0

            for chunk_idx, start_idx in enumerate(range(0, len(poi_join_input), chunk_size), start=1):
                end_idx = min(start_idx + chunk_size, len(poi_join_input))
                joined_chunk = gpd.sjoin(
                    poi_join_input.iloc[start_idx:end_idx],
                    building_join_input,
                    how='inner',
                    predicate='intersects',
                )
                if not joined_chunk.empty:
                    joined_parts.append(joined_chunk)
                    joined_rows += len(joined_chunk)
                logger.info(
                    "Spatial join progress: chunk %s/%s, POIs %s-%s/%s, joined rows %s, %.1fs elapsed",
                    chunk_idx,
                    num_chunks,
                    start_idx + 1,
                    end_idx,
                    len(poi_join_input),
                    joined_rows,
                    time.perf_counter() - join_started,
                )

            if joined_parts:
                joined = gpd.GeoDataFrame(
                    pd.concat(joined_parts, ignore_index=False),
                    geometry='geometry',
                    crs=gdf_pois.crs,
                )
            else:
                joined = pd.DataFrame(columns=['poi_f_class', '_bldg_idx'])
        else:
            logger.info("Running spatial join...")
            joined = gpd.sjoin(
                poi_join_input,
                building_join_input,
                how='inner',
                predicate='intersects',
            )

        logger.info(
            "Spatial join completed in %.1fs with %s joined row(s)",
            time.perf_counter() - join_started,
            len(joined),
        )

        # Group POI f_classes by building index (vectorized)
        aggregate_started = time.perf_counter()
        valid = joined['poi_f_class'].notna() & ~joined['poi_f_class'].isin(('yes', 'building'))
        filtered = joined.loc[valid, ['_bldg_idx', 'poi_f_class']]
        poi_map = filtered.groupby('_bldg_idx')['poi_f_class'].apply(list).to_dict()

        matched_count = len(poi_map)
        total_pois = sum(len(v) for v in poi_map.values())
        logger.info(
            "Spatial join aggregation completed in %.1fs: %s POIs matched to %s buildings",
            time.perf_counter() - aggregate_started,
            total_pois,
            matched_count,
        )

        gdf_buildings.drop(columns=['_bldg_idx'], inplace=True)
        return gdf_buildings, poi_map

    def _extract_buildings_with_osmium(self, pbf_path: Path, output_dir: Path) -> Path:
        """Extract buildings from PBF using osmium-tool."""
        region_name = self.region_config.get("state", self.region_config["country"])
        buildings_pbf = output_dir / f"{region_name}_buildings.osm.pbf"
        
        logger.info("Extracting buildings with osmium...")
        
        cmd = [
            "osmium", "tags-filter",
            str(pbf_path),
            "w/building",  # Ways with building tag
            "r/building",  # Relations with building tag
            "-o", str(buildings_pbf),
            "--overwrite"
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info(f"Extracted buildings to {buildings_pbf}")
            return buildings_pbf
        except subprocess.CalledProcessError as e:
            logger.error(f"osmium extraction failed: {e.stderr.decode()}")
            raise
        except FileNotFoundError:
            logger.warning("osmium-tool not found, using full PBF")
            return pbf_path
    
    def _convert_to_shapefile(self, pbf_path: Path, output_dir: Path, full_pbf_path: Path = None) -> Path:
        """Convert PBF to building vectors compatible with pylovo.

        pylovo expects:
        - Res: osm_id, area (computed), building_t (type), floors, geom
        - Oth: osm_id, area (computed), use (category), geom

        Uses a two-step approach for large PBF files:
        1. Extract all buildings from PBF to GPKG (no SQL dialect - faster)
        2. Filter and process with geopandas

        Files are named with AGS code for compatibility with import paths.

        Args:
            pbf_path: Path to the buildings-filtered PBF
            output_dir: Output directory for building vectors
            full_pbf_path: Path to the original full PBF (for POI extraction)
        """
        region_name = self.region_config.get("state", self.region_config["country"])
        target_crs = self.region_config.get("crs", "EPSG:3035")

        # Use AGS code for filename if available, otherwise use OSM relation ID
        ags_code = self.region_config.get("ags") or str(self.region_config["osm_relation_id"])

        # Output files - use AGS code in filename
        all_buildings_gpkg = output_dir / f"buildings_{region_name}.gpkg"
        res_gpkg = output_dir / f"Res_{ags_code}.gpkg"
        oth_gpkg = output_dir / f"Oth_{ags_code}.gpkg"

        logger.info("Converting buildings to GPKG (pylovo-compatible schema)...")

        # Step 1: Extract ALL buildings from PBF to GPKG using simple -where filter
        # This avoids SQLite dialect memory issues with large files
        all_cmd = [
            "ogr2ogr",
            "--config", "OGR_INTERLEAVED_READING", "YES",
            "--config", "OSM_MAX_TMPFILE_SIZE", "1024",
            "-f", "GPKG",
            "-t_srs", target_crs,
            str(all_buildings_gpkg),
            str(pbf_path),
            "multipolygons",
            "-where", "building IS NOT NULL",
            "-overwrite",
            "-progress",
            "-nlt", "PROMOTE_TO_MULTI"
        ]

        try:
            # Remove existing files to avoid conflicts
            for f in [all_buildings_gpkg, res_gpkg, oth_gpkg]:
                if f.exists():
                    f.unlink()
                    logger.debug(f"Removed existing file: {f}")

            # Remove stale shapefile artifacts from older pipeline versions.
            for prefix in ["Res", "Oth"]:
                for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                    f = output_dir / f"{prefix}_{ags_code}{ext}"
                    if f.exists():
                        f.unlink()
                        logger.debug(f"Removed stale file: {f}")

            logger.info("Extracting all buildings from PBF (this may take a while for large regions)...")
            result = subprocess.run(all_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"Building extraction failed: {result.stderr}")
                raise subprocess.CalledProcessError(result.returncode, all_cmd, result.stderr)

            # Step 2: Extract POI nodes from full PBF for spatial join
            pois_gpkg = None
            if full_pbf_path and full_pbf_path.exists():
                try:
                    pois_gpkg = self._extract_pois_gpkg(full_pbf_path, output_dir)
                except Exception as e:
                    logger.warning(f"POI extraction failed, continuing without POIs: {e}")

            # Step 3: Use geopandas to filter and create Res/Oth vectors
            logger.info("Filtering and processing buildings with geopandas...")
            self._filter_buildings_with_geopandas(
                all_buildings_gpkg,
                res_gpkg=res_gpkg,
                oth_gpkg=oth_gpkg,
                pois_gpkg=pois_gpkg,
            )

            # Count features
            self._log_building_counts(output_dir, ags_code)

            # Copy to raw_data/buildings/ for import_buildings.py compatibility
            self._copy_to_raw_data(output_dir, ags_code)

            return output_dir

        except subprocess.CalledProcessError as e:
            logger.error(f"Building extraction failed: {e}")
            raise

    def _filter_buildings_with_geopandas(
        self,
        input_gpkg: Path,
        res_gpkg: Path,
        oth_gpkg: Path,
        pois_gpkg: Path = None,
    ):
        """Filter buildings into residential and other categories using geopandas.

        Uses the best available OSM tag (amenity/shop/office/etc) for f_class.
        Optionally joins POI nodes to building polygons for multi-use buildings.
        """
        import re
        import geopandas as gpd
        import numpy as np

        logger.info(f"Loading buildings from {input_gpkg}...")
        gdf = gpd.read_file(input_gpkg)
        logger.info(f"Loaded {len(gdf)} buildings")

        if len(gdf) == 0:
            logger.warning("No buildings found in GPKG")
            return

        # Step 1: Resolve best f_class from polygon tags (amenity > shop > ... > building)
        logger.info(f"Resolving f_class for {len(gdf)} buildings (vectorized)...")
        gdf['f_class'] = self._resolve_best_f_class_vectorized(gdf)
        gdf['f_classes'] = gdf['f_class']
        tag_upgraded = (gdf['f_class'] != gdf['building'].fillna('yes').str.lower().str.strip()).sum()
        logger.info(f"f_class: {tag_upgraded} buildings upgraded from building tag to more specific tag")

        # Step 1b: Exclude buildings that don't use electricity
        no_elec_mask = gdf['f_class'].isin(self.NO_ELECTRICITY_TYPES)
        excluded_count = no_elec_mask.sum()
        if excluded_count > 0:
            logger.info(f"Excluding {excluded_count} buildings with no electricity usage "
                        f"(types: {gdf.loc[no_elec_mask, 'f_class'].value_counts().to_dict()})")
            gdf = gdf[~no_elec_mask].copy()

        # Step 2: Spatial join POI nodes to buildings (if available)
        poi_map = None
        if pois_gpkg and Path(pois_gpkg).exists():
            try:
                gdf, poi_map = self._spatial_join_pois(gdf, pois_gpkg)
            except Exception as e:
                logger.warning(f"POI spatial join failed, continuing without: {e}")

        # Step 3: Merge POI classes into f_classes on the same building row.
        # Keep one geometry per building and store all detected use classes.
        if poi_map:
            gdf, updated_count, multi_count = self._merge_poi_classes(gdf, poi_map)
            logger.info(
                "POI class merge: updated %s buildings (%s with multi-use classes)",
                updated_count,
                multi_count,
            )

        # Step 4: Determine residential vs other based on FINAL f_class
        # A building is residential if:
        # - Its f_class is in RESIDENTIAL_BUILDING_TYPES, OR
        # - Its building tag is residential AND its f_class is a residential use
        building_col = gdf['building'].fillna('yes').str.lower().str.strip()
        is_res_building = building_col.isin(self.RESIDENTIAL_BUILDING_TYPES)
        is_res_use = gdf['f_class'].isin(self.RESIDENTIAL_BUILDING_TYPES | self.RESIDENTIAL_USE_VALUES)
        # If building tag says residential, keep as residential unless f_class is clearly non-residential
        # If building tag says non-residential, classify by f_class
        res_mask = is_res_building & is_res_use
        # Also include buildings with building=yes and no specific use tag (default residential)
        res_mask = res_mask | ((building_col == 'yes') & (gdf['f_class'] == 'yes'))

        gdf_res = gdf[res_mask].copy()
        gdf_oth = gdf[~res_mask].copy()

        logger.info(f"Split: {len(gdf_res)} residential, {len(gdf_oth)} other buildings")

        # Process residential buildings
        if len(gdf_res) > 0:
            self._process_residential_gdf(gdf_res, res_gpkg)

        # Process other buildings
        if len(gdf_oth) > 0:
            self._process_other_gdf(gdf_oth, oth_gpkg)

    @classmethod
    def _merge_poi_classes(cls, gdf, poi_map):
        """Attach POI-derived classes to each building without duplicating rows.

        Stores:
        - `f_class`: primary class (most specific)
        - `f_classes`: ';' separated list of all classes for this building
        """
        if 'f_classes' not in gdf.columns:
            gdf['f_classes'] = gdf['f_class']

        # Pre-read base f_class values for all POI-matched rows at once
        # to avoid expensive per-row iloc lookups on a large GeoDataFrame.
        max_idx = len(gdf)
        valid_items = [(idx, pcs) for idx, pcs in poi_map.items() if idx < max_idx]
        if not valid_items:
            return gdf, 0, 0

        indices = [idx for idx, _ in valid_items]
        base_values = gdf['f_class'].iloc[indices].fillna('yes').values

        new_primaries = {}
        new_f_classes = {}
        multi_count = 0

        for (idx, poi_classes), base_fc in zip(valid_items, base_values):
            merged = cls._normalize_f_class_list([base_fc, *poi_classes])
            if not merged:
                merged = [cls._canonicalize_f_class(base_fc)]

            primary = cls._select_primary_f_class(merged)
            ordered = [primary, *[fc for fc in merged if fc != primary]]

            new_primaries[idx] = primary
            new_f_classes[idx] = ";".join(ordered)
            if len(ordered) > 1:
                multi_count += 1

        # Batch-assign all computed values at once
        idx_list = list(new_primaries.keys())
        gdf.iloc[idx_list, gdf.columns.get_loc('f_class')] = [new_primaries[i] for i in idx_list]
        gdf.iloc[idx_list, gdf.columns.get_loc('f_classes')] = [new_f_classes[i] for i in idx_list]

        updated_count = len(idx_list)

        # Fill missing f_classes for rows without POI matches.
        missing_mask = gdf['f_classes'].isna() | (gdf['f_classes'].astype(str).str.strip() == '')
        if missing_mask.any():
            gdf.loc[missing_mask, 'f_classes'] = gdf.loc[missing_mask, 'f_class']

        return gdf, updated_count, multi_count

    def _process_residential_gdf(self, gdf, output_gpkg: Path):
        """Process residential buildings and save to lossless GPKG."""
        import re

        import pandas as pd

        logger.info(f"Processing {len(gdf)} residential buildings...")

        # Use osm_way_id if osm_id is null
        gdf['osm_id'] = gdf['osm_way_id'].fillna(gdf['osm_id'])

        # Compute area (only if not already set by multi-POI expansion)
        if 'Area' not in gdf.columns:
            gdf['Area'] = gdf.geometry.area
        else:
            gdf['Area'] = gdf['Area'].fillna(gdf.geometry.area)

        # Ensure canonical f_class values
        if 'f_class' not in gdf.columns:
            gdf['f_class'] = gdf['building'].fillna('yes').apply(self._canonicalize_f_class)
        else:
            gdf['f_class'] = gdf['f_class'].apply(self._canonicalize_f_class)
        if 'f_classes' not in gdf.columns:
            gdf['f_classes'] = gdf['f_class']
        else:
            gdf['f_classes'] = gdf['f_classes'].fillna(gdf['f_class'])
        gdf['Building_T'] = gdf['f_class']
        gdf['Use'] = gdf['Building_T']

        # Extract floors from other_tags
        def extract_floors(other_tags):
            if other_tags and isinstance(other_tags, str):
                match = re.search(r'"building:levels"=>"(\d+)"', other_tags)
                if match:
                    return int(match.group(1))
            return None

        # Left empty without a building:levels tag so enrichment can fill it.
        gdf['Floors'] = gdf['other_tags'].apply(extract_floors) if 'other_tags' in gdf.columns else None

        # Estimate occupants
        _res_low_density_types = {'house', 'detached', 'bungalow', 'cabin', 'hut', 'farm', 'farmhouse', 'static_caravan', 'yes'}
        _res_semi_dense_types = {'semidetached_house'}
        _res_apartment_types = {'apartments', 'residential', 'dormitory'}
        _res_terrace_types = {'terrace', 'row_house'}

        def estimate_occupants(row):
            area = row['Area']
            b_type = row['Building_T']
            # Households are estimated with one floor where OSM has no level count.
            floors = row['Floors'] if pd.notna(row['Floors']) else 1
            if b_type in _res_low_density_types:
                return max(1, int(area * floors / 50))
            elif b_type in _res_semi_dense_types:
                return max(2, int(area * floors / 40))
            elif b_type in _res_apartment_types:
                return max(4, int(area * floors / 35))
            elif b_type in _res_terrace_types:
                return max(2, int(area * floors / 45))
            return 1

        gdf['Occupants'] = gdf.apply(estimate_occupants, axis=1)

        # Set free_walls
        def calc_free_walls(b_type):
            if b_type in _res_low_density_types:
                return 4
            elif b_type in (_res_semi_dense_types | _res_terrace_types | _res_apartment_types):
                return 2
            return 4

        gdf['Free_walls'] = gdf['Building_T'].apply(calc_free_walls)

        # Add remaining columns
        gdf['Comment'] = None
        gdf['Constructi'] = None
        gdf['Refurb_wal'] = None
        gdf['Refurb_roo'] = None
        gdf['Refurb_bas'] = None
        gdf['Refurb_win'] = None

        # Select final columns
        final_cols = ['osm_id', 'Area', 'Use', 'Comment', 'Free_walls', 'Building_T',
                      'Occupants', 'Floors', 'Constructi', 'Refurb_wal', 'Refurb_roo',
                      'Refurb_bas', 'Refurb_win', 'f_class', 'f_classes', 'geometry']
        gdf = gdf[[c for c in final_cols if c in gdf.columns or c == 'geometry']]

        if output_gpkg.exists():
            output_gpkg.unlink()
        gdf.to_file(output_gpkg, driver="GPKG")

        logger.info(f"Saved {len(gdf)} residential buildings to {output_gpkg}")

    def _process_other_gdf(self, gdf, output_gpkg: Path):
        """Process other buildings and save to lossless GPKG."""
        logger.info(f"Processing {len(gdf)} other buildings...")

        # Use osm_way_id if osm_id is null
        gdf['osm_id'] = gdf['osm_way_id'].fillna(gdf['osm_id'])

        # Compute area (only if not already set by multi-POI expansion)
        if 'Area' not in gdf.columns:
            gdf['Area'] = gdf.geometry.area
        else:
            gdf['Area'] = gdf['Area'].fillna(gdf.geometry.area)

        # Ensure canonical f_class values
        if 'f_class' not in gdf.columns:
            gdf['f_class'] = gdf['building'].fillna('yes').apply(self._canonicalize_f_class)
        else:
            gdf['f_class'] = gdf['f_class'].apply(self._canonicalize_f_class)
        if 'f_classes' not in gdf.columns:
            gdf['f_classes'] = gdf['f_class']
        else:
            gdf['f_classes'] = gdf['f_classes'].fillna(gdf['f_class'])
        gdf['Use'] = gdf['f_class']
        gdf['Comment'] = None
        gdf['Free_walls'] = 4

        # Select final columns
        final_cols = ['osm_id', 'Area', 'Use', 'Comment', 'Free_walls', 'f_class', 'f_classes', 'geometry']
        gdf = gdf[[c for c in final_cols if c in gdf.columns or c == 'geometry']]

        if output_gpkg.exists():
            output_gpkg.unlink()
        gdf.to_file(output_gpkg, driver="GPKG")

        logger.info(f"Saved {len(gdf)} other buildings to {output_gpkg}")

    def _postprocess_buildings(self, input_shp: Path, output_shp: Path, building_type: str):
        """Post-process buildings to add pylovo-compatible columns.

        Creates shapefiles with all columns expected by the pylovo database:
        - Res: osm_id, Area, Use, Comment, Free_walls, Building_T, Occupants, Floors,
               Constructi, Refurb_wal, Refurb_roo, Refurb_bas, Refurb_win
        - Oth: osm_id, Area, Use, Comment, Free_walls
        """
        import re

        try:
            import geopandas as gpd
            import numpy as np
            HAS_GEOPANDAS = True
        except ImportError:
            HAS_GEOPANDAS = False
            logger.warning("geopandas not found, falling back to osgeo.ogr")

        logger.info(f"Post-processing {building_type} buildings for pylovo compatibility...")

        if HAS_GEOPANDAS:
            gdf = gpd.read_file(input_shp)
            
            # Ensure it's a GeoDataFrame (sometimes read_file returns a DataFrame if geometry is not detected)
            if not isinstance(gdf, gpd.GeoDataFrame):
                if 'geometry' in gdf.columns:
                    gdf = gpd.GeoDataFrame(gdf, geometry='geometry')
                else:
                    logger.error(f"Input file {input_shp} has no geometry column. Available columns: {gdf.columns.tolist()}")
                    return

            logger.info(f"Loaded {len(gdf)} raw features from {input_shp}")

            if len(gdf) == 0:
                logger.warning(f"No {building_type} buildings found")
                # Create empty shapefile with correct schema
                if building_type == "residential":
                    gdf = gpd.GeoDataFrame(columns=['osm_id', 'Area', 'Use', 'Comment', 'Free_walls',
                                                     'Building_T', 'Occupants', 'Floors', 'Constructi',
                                                     'Refurb_wal', 'Refurb_roo', 'Refurb_bas', 'Refurb_win',
                                                     'f_class', 'f_classes', 'geometry'])
                else:
                    gdf = gpd.GeoDataFrame(columns=['osm_id', 'Area', 'Use', 'Comment', 'Free_walls', 'f_class', 'f_classes', 'geometry'])
                gdf.to_file(output_shp)
                return

            # Compute area from geometry
            gdf['Area'] = gdf.geometry.area

            # Common columns for both types
            gdf['Comment'] = None
            gdf['Free_walls'] = 4  # Default: detached building

            # Resolve f_class from best available tag (amenity > shop > ... > building)
            gdf['f_class'] = self._resolve_best_f_class_vectorized(gdf)
            gdf['f_classes'] = gdf['f_class']

            if building_type == "residential":
                # Use f_class directly as Building_T (granular classification)
                gdf['Building_T'] = gdf['f_class']
                gdf['Use'] = gdf['Building_T']

                # Extract floors from other_tags
                def extract_floors(other_tags):
                    if other_tags and isinstance(other_tags, str):
                        match = re.search(r'"building:levels"=>"(\d+)"', other_tags)
                        if match:
                            return int(match.group(1))
                    return 1  # Default to 1 floor

                gdf['Floors'] = gdf['other_tags'].apply(extract_floors)

                # Estimate occupants (households) based on f_class and area
                _res_low_density_types = {'house', 'detached', 'bungalow', 'cabin', 'hut', 'farm', 'farmhouse', 'static_caravan'}
                _res_semi_dense_types = {'semidetached_house'}
                _res_apartment_types = {'apartments', 'residential', 'dormitory'}
                _res_terrace_types = {'terrace', 'row_house'}

                def estimate_occupants(row):
                    area = row['Area']
                    b_type = row['Building_T']
                    floors = row['Floors']
                    if b_type in _res_low_density_types or b_type in {'yes', 'building', 'unclassified', 'other'}:
                        return max(1, int(area * floors / 50))
                    elif b_type in _res_semi_dense_types:
                        return max(2, int(area * floors / 40))
                    elif b_type in _res_apartment_types:
                        return max(4, int(area * floors / 35))
                    elif b_type in _res_terrace_types:
                        return max(2, int(area * floors / 45))
                    return 1

                gdf['Occupants'] = gdf.apply(estimate_occupants, axis=1)

                # Set free_walls based on f_class
                def calc_free_walls(b_type):
                    if b_type in _res_low_density_types or b_type in {'yes', 'building', 'unclassified', 'other'}:
                        return 4  # Detached
                    elif b_type in _res_semi_dense_types or b_type in _res_terrace_types:
                        return 2  # Semi-detached/row
                    elif b_type in _res_apartment_types:
                        return 2  # Apartment block
                    return 4

                gdf['Free_walls'] = gdf['Building_T'].apply(calc_free_walls)

                # Default values for refurbishment columns (not available from OSM)
                gdf['Constructi'] = None  # Construction year not available
                gdf['Refurb_wal'] = None
                gdf['Refurb_roo'] = None
                gdf['Refurb_bas'] = None
                gdf['Refurb_win'] = None

                # Keep only pylovo-required columns (matching original case)
                gdf = gdf[['osm_id', 'Area', 'Use', 'Comment', 'Free_walls', 'Building_T',
                           'Occupants', 'Floors', 'Constructi', 'Refurb_wal', 'Refurb_roo',
                           'Refurb_bas', 'Refurb_win', 'f_class', 'f_classes', 'geometry']]

            else:  # other buildings
                # Use f_class directly as Use (granular classification)
                gdf['Use'] = gdf['f_class']

                # Keep only pylovo-required columns
                gdf = gdf[['osm_id', 'Area', 'Use', 'Comment', 'Free_walls', 'f_class', 'f_classes', 'geometry']]

            # Save to shapefile
            gdf.to_file(output_shp)
            logger.info(f"Saved {len(gdf)} {building_type} buildings to {output_shp}")

        else:
            # Fallback using OGR - create schema matching pylovo database
            from osgeo import ogr, osr

            # Open input (can be GPKG or SHP)
            ds = ogr.Open(str(input_shp), 0)  # Read-only
            if ds is None:
                logger.error(f"Failed to open {input_shp}")
                return

            layer = ds.GetLayer()

            # Create output (always Shapefile for pylovo)
            driver = ogr.GetDriverByName("ESRI Shapefile")
            if output_shp.exists():
                driver.DeleteDataSource(str(output_shp))

            out_ds = driver.CreateDataSource(str(output_shp))
            out_layer = out_ds.CreateLayer(output_shp.stem, layer.GetSpatialRef(), ogr.wkbMultiPolygon)

            # Create fields matching pylovo database schema
            out_layer.CreateField(ogr.FieldDefn("osm_id", ogr.OFTString))
            out_layer.CreateField(ogr.FieldDefn("Area", ogr.OFTReal))
            out_layer.CreateField(ogr.FieldDefn("Use", ogr.OFTString))
            out_layer.CreateField(ogr.FieldDefn("Comment", ogr.OFTString))
            out_layer.CreateField(ogr.FieldDefn("Free_walls", ogr.OFTInteger))

            if building_type == "residential":
                out_layer.CreateField(ogr.FieldDefn("Building_T", ogr.OFTString))
                out_layer.CreateField(ogr.FieldDefn("Occupants", ogr.OFTReal))
                out_layer.CreateField(ogr.FieldDefn("Floors", ogr.OFTInteger))
                out_layer.CreateField(ogr.FieldDefn("Constructi", ogr.OFTString))
                out_layer.CreateField(ogr.FieldDefn("Refurb_wal", ogr.OFTReal))
                out_layer.CreateField(ogr.FieldDefn("Refurb_roo", ogr.OFTReal))
                out_layer.CreateField(ogr.FieldDefn("Refurb_bas", ogr.OFTReal))
                out_layer.CreateField(ogr.FieldDefn("Refurb_win", ogr.OFTReal))

            # Add f_class field for both types
            out_layer.CreateField(ogr.FieldDefn("f_class", ogr.OFTString))
            out_layer.CreateField(ogr.FieldDefn("f_classes", ogr.OFTString))

            feature_defn = out_layer.GetLayerDefn()

            count = 0
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom is None:
                    continue

                out_feat = ogr.Feature(feature_defn)
                out_feat.SetGeometry(geom)

                # Common fields
                osm_id = feature.GetField("osm_id")
                area = geom.GetArea()
                out_feat.SetField("osm_id", osm_id)
                out_feat.SetField("Area", area)

                b_type = feature.GetField("building")
                # Resolve best f_class from available tag columns
                row_dict = {'building': b_type}
                for tag_col in self.USE_TAG_PRIORITY:
                    try:
                        val = feature.GetField(tag_col)
                        if val:
                            row_dict[tag_col] = val
                    except Exception:
                        pass
                f_class = self._resolve_best_f_class(row_dict)
                if b_type:
                    b_type = b_type.lower()
                other_tags = feature.GetField("other_tags")

                # Set f_class for all building types
                out_feat.SetField("f_class", f_class)
                out_feat.SetField("f_classes", f_class)

                if building_type == "residential":
                    # Use f_class directly as Building_T (granular classification)
                    b_t = b_type if b_type else 'yes'

                    out_feat.SetField("Building_T", b_t)
                    out_feat.SetField("Use", b_t)

                    # Extract floors
                    floors = 1
                    if other_tags:
                        match = re.search(r'"building:levels"=>"(\d+)"', other_tags)
                        if match:
                            floors = int(match.group(1))
                    out_feat.SetField("Floors", floors)

                    # Estimate occupants based on f_class
                    _res_low_density_set = {'house', 'detached', 'bungalow', 'cabin', 'hut', 'farm', 'farmhouse', 'static_caravan', 'yes', 'building', 'unclassified', 'other'}
                    _res_semi_dense_set = {'semidetached_house'}
                    _res_apartment_set = {'apartments', 'residential', 'dormitory'}
                    _res_terrace_set = {'terrace', 'row_house'}
                    if b_t in _res_low_density_set:
                        occupants = max(1, int(area * floors / 50))
                        free_walls = 4
                    elif b_t in _res_semi_dense_set or b_t in _res_terrace_set:
                        occupants = max(2, int(area * floors / 40))
                        free_walls = 2
                    elif b_t in _res_apartment_set:
                        occupants = max(4, int(area * floors / 35))
                        free_walls = 2
                    else:
                        occupants = 1
                        free_walls = 4

                    out_feat.SetField("Occupants", occupants)
                    out_feat.SetField("Free_walls", free_walls)

                else:  # Other buildings
                    # Use f_class directly as Use (granular classification)
                    use = b_type if b_type else 'commercial'
                    out_feat.SetField("Use", use)
                    out_feat.SetField("Free_walls", 4)

                out_layer.CreateFeature(out_feat)
                out_feat = None
                count += 1

            ds = None
            out_ds = None
            logger.info(f"Saved {count} {building_type} buildings to {output_shp}")
    
    def _copy_to_raw_data(self, output_dir: Path, ags_code: str):
        """Copy building outputs to raw_data/buildings/ for compatibility."""
        from datapipeline.utils import get_project_root

        raw_data_dir = get_project_root() / "raw_data" / "buildings"
        raw_data_dir.mkdir(parents=True, exist_ok=True)

        for prefix in ["Res", "Oth"]:
            # Remove stale shapefile artifacts from older pipeline versions.
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                stale = raw_data_dir / f"{prefix}_{ags_code}{ext}"
                if stale.exists():
                    stale.unlink()
            gpkg_src = output_dir / f"{prefix}_{ags_code}.gpkg"
            if gpkg_src.exists():
                gpkg_dst = raw_data_dir / f"{prefix}_{ags_code}.gpkg"
                shutil.copy2(gpkg_src, gpkg_dst)
                logger.debug(f"Copied {gpkg_src.name} to {raw_data_dir}")

        logger.info(f"Building outputs copied to {raw_data_dir} for import compatibility")

    def _log_building_counts(self, output_dir: Path, ags_code: str):
        """Log building counts from extracted GPKG files."""
        try:
            import subprocess

            for layer_type in ["Res", "Oth"]:
                gpkg_path = output_dir / f"{layer_type}_{ags_code}.gpkg"
                if gpkg_path.exists():
                    result = subprocess.run(
                        ["ogrinfo", "-so", str(gpkg_path), "-al"],
                        capture_output=True, text=True
                    )
                    for line in result.stdout.split('\n'):
                        if 'Feature Count' in line:
                            logger.info(f"{layer_type}: {line.strip()}")
                            break
        except Exception as e:
            logger.debug(f"Could not get building counts: {e}")
    
    def download(self, use_overpass: bool = False) -> Path:
        """
        Download building data for the configured region.
        
        Args:
            use_overpass: Ignored (kept for backward compatibility)
        
        Returns:
            Path to the output directory containing building files
        """
        output_dir = self.get_output_dir()
        
        logger.info(f"Downloading buildings for {self.region_config['name']}...")
        
        # Get PBF clipped to region boundary (state-level if applicable)
        region_pbf_path = self.get_region_pbf_path()
        # Keep reference to full PBF for POI extraction
        full_pbf_path = self.get_pbf_path()
        
        # Try to extract buildings with osmium first (faster)
        try:
            buildings_pbf = self._extract_buildings_with_osmium(region_pbf_path, output_dir)
        except Exception as e:
            logger.warning(f"osmium extraction failed, using full PBF: {e}")
            buildings_pbf = region_pbf_path
        
        # Convert to building files with f_class (pass region PBF for POI extraction)
        self._convert_to_shapefile(buildings_pbf, output_dir, full_pbf_path=region_pbf_path)
        
        logger.info(f"Building data saved to {output_dir}")
        return output_dir
