"""
Transformer data processor for validation and enrichment.

This module applies the same filtering logic as the original pylovo import_transformers.py:
1. Remove Point transformers that are inside Polygon transformers
2. Remove transformers with area >= 60m² (large substations)
3. Remove high voltage (>= 110kV) transformers
4. Remove transformers within 8m of each other
5. Remove transformers inside shopping areas (if shopping data available)
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

import numpy as np
import shapely

try:
    import geopandas as gpd
    import pandas as pd
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False

logger = logging.getLogger("datapipeline")

# Original pylovo filtering constants
AREA_THRESHOLD = 60  # m² - remove transformers with area >= this
MIN_DISTANCE_BETWEEN_TRAFOS = 8  # meters - remove transformers too close together
VOLTAGE_THRESHOLD = 110000  # Volts - remove high voltage transformers


def drop_close_transformers(gdf, min_distance: float):
    """Drop every transformer whose centroid lies closer than min_distance to a later row's centroid.

    Matches upstream pylovo's upper-triangle distance-matrix rule. Pairs come from the
    spatial index, because the dense matrix needs n² memory (11 GB for 38,000 transformers).
    """
    centroids = np.asarray(gdf.geometry.centroid.values)
    left, right = shapely.STRtree(centroids).query(centroids, predicate="dwithin", distance=min_distance)
    later = right > left
    left, right = left[later], right[later]
    close = shapely.distance(centroids[left], centroids[right]) < min_distance
    return gdf.drop(index=gdf.index[np.unique(left[close])])


class TransformerProcessor:
    """Process and validate transformer data."""
    
    # Voltage levels for classification
    VOLTAGE_LEVELS = {
        "low": (0, 1000),           # Low voltage: < 1kV
        "medium": (1000, 35000),    # Medium voltage: 1-35kV
        "high": (35000, 220000),    # High voltage: 35-220kV
        "extra_high": (220000, None) # Extra high: > 220kV
    }
    
    def __init__(self, region_config: Dict[str, Any]):
        """Initialize the processor."""
        self.region_config = region_config
    
    def classify_voltage(self, voltage_str: Optional[str]) -> str:
        """
        Classify transformer by voltage level.
        
        Args:
            voltage_str: Voltage string from OSM (e.g., "20000", "20000;400", "20 kV")
        
        Returns:
            Voltage classification
        """
        if not voltage_str:
            return "unknown"

        text = str(voltage_str).strip().lower()
        if not text:
            return "unknown"

        # Handle textual voltage labels that appear in OSM.
        keyword_order = [
            ("extra_high", "extra_high"),
            ("ehv", "extra_high"),
            ("high", "high"),
            ("hv", "high"),
            ("medium", "medium"),
            ("mv", "medium"),
            ("low", "low"),
            ("lv", "low"),
        ]
        for keyword, level in keyword_order:
            if re.search(rf"(^|[^a-z]){re.escape(keyword)}([^a-z]|$)", text):
                return level

        try:
            # Normalize separators
            cleaned = text
            # Treat decimal comma as decimal dot (e.g. 0,4kV).
            cleaned = re.sub(r'(?<=\d),(?=\d)', '.', cleaned)
            cleaned = cleaned.replace(";", ",").replace("/", ",").replace("|", ",")
            parts = cleaned.split(",")
            
            voltages = []
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                
                try:
                    # Handle kV
                    if "kv" in part:
                        val_str = part.replace("kv", "").strip()
                        val = float(val_str) * 1000
                    else:
                        val_str = part.replace("v", "").strip()
                        val = float(val_str)
                    
                    voltages.append(val)
                except ValueError:
                    # Ignore non-numeric parts like "medium"
                    continue
            
            if not voltages:
                return "unknown"

            max_voltage = max(voltages)
            
            for level, (min_v, max_v) in self.VOLTAGE_LEVELS.items():
                if max_v is None:
                    if max_voltage >= min_v:
                        return level
                elif min_v <= max_voltage < max_v:
                    return level
            
            return "unknown"
        except (ValueError, TypeError):
            return "unknown"

    def infer_voltage_class(self, properties: Dict[str, Any]) -> str:
        """
        Infer voltage class from tags when explicit voltage is missing.

        This keeps inference conservative: only obvious distribution tags are
        promoted to medium voltage.
        """
        voltage_class = self.classify_voltage(properties.get("voltage"))
        if voltage_class != "unknown":
            return voltage_class

        substation_type = str(properties.get("substation", "") or "").strip().lower()
        power_type = str(properties.get("power", "") or "").strip().lower()
        building_type = str(properties.get("building", "") or "").strip().lower()

        if substation_type in {"distribution", "minor_distribution"}:
            return "medium"
        if power_type == "minor_substation":
            return "medium"
        if building_type == "transformer_tower":
            return "medium"

        return "unknown"
    
    def is_distribution_transformer(self, properties: Dict[str, Any]) -> bool:
        """
        Check if a transformer is a distribution transformer (MV/LV).
        
        Args:
            properties: Feature properties from GeoJSON
        
        Returns:
            True if distribution transformer
        """
        power_type = properties.get("power", "")
        substation_type = properties.get("substation", "")
        
        # Distribution substations
        if substation_type in ("distribution", "minor_distribution"):
            return True
        
        # Minor substations are typically distribution
        if power_type == "minor_substation":
            return True
        
        # Check voltage - distribution is typically 10-20kV to 400V
        voltage = properties.get("voltage", "")
        if voltage:
            voltage_class = self.classify_voltage(voltage)
            if voltage_class in ("low", "medium"):
                return True
        
        # Transformer houses
        if properties.get("building") == "transformer_tower":
            return True
        
        return False
    
    def process_geojson(self, geojson_path: Path, output_path: Optional[Path] = None) -> Path:
        """
        Process transformer GeoJSON with classification and filtering.
        
        Args:
            geojson_path: Path to input GeoJSON
            output_path: Optional output path
        
        Returns:
            Path to processed GeoJSON
        """
        logger.info(f"Processing transformers from {geojson_path}")
        
        with open(geojson_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        processed_features = []
        distribution_count = 0
        
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            geom_type = geom.get("type", "Unknown")
            
            # Use original_geom_type if available (set by convert_to_points before conversion)
            # This ensures correct osm_id prefix even after geometry conversion
            original_geom_type = props.get("original_geom_type", geom_type)
            
            # Determine OSM type based on ORIGINAL geometry (not converted)
            if original_geom_type == "Point":
                osm_type = "node"
            elif original_geom_type in ("Polygon", "MultiPolygon"):
                osm_type = "way"
            elif original_geom_type in ("LineString", "MultiLineString"):
                osm_type = "way"
            else:
                osm_type = "way"
            
            # Calculate area for polygons (will be 0 for points)
            area = 0.0
            if original_geom_type in ("Polygon", "MultiPolygon"):
                try:
                    from shapely.geometry import shape
                    # Can't calculate area if already converted to point
                    if geom_type in ("Polygon", "MultiPolygon"):
                        area = shape(geom).area
                except:
                    pass
            
            # Build properties in expected format
            new_props = {
                "type": osm_type,
                "osm_id": f"{osm_type}/{props.get('osm_id', '')}",
                "area": area,
                "power": props.get("power", ""),
                "geom_type": original_geom_type,  # Store original geom type
                "within_shopping": False,  # Default, would need shopping area data to compute
                # Keep additional useful fields
                "substation": props.get("substation", ""),
                "voltage": props.get("voltage", ""),
                "operator": props.get("operator", ""),
                "ref": props.get("ref", ""),
                "name": props.get("name", ""),
            }
            
            # Add voltage classification
            new_props["voltage_class"] = self.infer_voltage_class(props)
            
            # Check if distribution transformer
            new_props["is_distribution"] = self.is_distribution_transformer(props)
            
            if new_props["is_distribution"]:
                distribution_count += 1
            
            feature["properties"] = new_props
            processed_features.append(feature)
        
        data["features"] = processed_features
        
        if output_path is None:
            output_path = geojson_path.parent / f"{geojson_path.stem}_processed.geojson"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        
        logger.info(f"Processed {len(processed_features)} transformers "
                   f"({distribution_count} distribution), saved to {output_path}")
        
        return output_path
    
    def filter_distribution_transformers(self, geojson_path: Path, output_path: Optional[Path] = None) -> Path:
        """
        Filter to keep only distribution transformers.
        
        Args:
            geojson_path: Path to input GeoJSON
            output_path: Optional output path
        
        Returns:
            Path to filtered GeoJSON
        """
        logger.info(f"Filtering distribution transformers from {geojson_path}")
        
        with open(geojson_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Filter to distribution transformers
        distribution_features = [
            f for f in data.get("features", [])
            if f.get("properties", {}).get("is_distribution", False) or
               self.is_distribution_transformer(f.get("properties", {}))
        ]
        
        data["features"] = distribution_features
        
        if output_path is None:
            output_path = geojson_path.parent / f"{geojson_path.stem}_distribution.geojson"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        
        logger.info(f"Filtered to {len(distribution_features)} distribution transformers")
        
        return output_path
    
    def apply_pylovo_filters(self, geojson_path: Path, output_path: Optional[Path] = None,
                              shopping_geojson_path: Optional[Path] = None,
                              target_crs: str = "EPSG:3035") -> Path:
        """
        Apply the original pylovo filtering logic to transformer data.
        
        This replicates the filtering from the original import_transformers.py:
        1. Remove Point transformers inside Polygon transformers
        2. Remove transformers with area >= 60m² (large substations)
        3. Remove high voltage (>= 110kV) transformers
        4. Remove transformers within 8m of each other
        5. Remove transformers inside shopping areas (optional)
        
        Args:
            geojson_path: Path to input GeoJSON
            output_path: Optional output path
            shopping_geojson_path: Optional path to shopping areas GeoJSON
            target_crs: CRS for distance calculations (default EPSG:3035)
        
        Returns:
            Path to filtered GeoJSON
        """
        if not HAS_GEOPANDAS:
            raise ImportError("geopandas is required for pylovo filtering")
        
        logger.info(f"Applying pylovo filters to {geojson_path}")
        
        # Load transformer data
        gdf = gpd.read_file(geojson_path)
        initial_count = len(gdf)
        logger.info(f"Start: {initial_count} transformers")
        
        if len(gdf) == 0:
            logger.warning("No transformers to filter")
            if output_path:
                gdf.to_file(output_path, driver="GeoJSON")
                return output_path
            return geojson_path
        
        # Convert to projected CRS for accurate area/distance calculations
        if gdf.crs and gdf.crs.to_string() != target_crs:
            gdf = gdf.to_crs(target_crs)
        elif not gdf.crs:
            # Assume WGS84 if no CRS
            gdf = gdf.set_crs("EPSG:4326").to_crs(target_crs)
        
        # Step 1: Remove Point transformers inside Polygon transformers
        gdf['geom_type'] = gdf.geometry.geom_type
        
        # Handle both Point and MultiPoint
        point_mask = gdf['geom_type'].isin(['Point', 'MultiPoint'])
        polygon_mask = gdf['geom_type'].isin(['Polygon', 'MultiPolygon'])
        
        if point_mask.any() and polygon_mask.any():
            gdf_points = gdf[point_mask].copy()
            gdf_polygons = gdf[polygon_mask]
            
            if len(gdf_polygons) > 0:
                union_of_polygons = gdf_polygons.geometry.unary_union
                # Get centroids for MultiPoint geometries
                point_geoms = gdf_points.geometry.apply(
                    lambda g: g.centroid if g.geom_type == 'MultiPoint' else g
                )
                within_poly = point_geoms.within(union_of_polygons)
                indices_to_drop = gdf_points[within_poly].index
                gdf = gdf.drop(indices_to_drop)
                logger.info(f"After step 1 (remove points inside polygons): {len(gdf)}")
        
        # Step 2: Remove transformers with area >= AREA_THRESHOLD
        gdf['area'] = gdf.geometry.area
        gdf = gdf[gdf['area'] < AREA_THRESHOLD]
        logger.info(f"After step 2 (remove large area >= {AREA_THRESHOLD}m²): {len(gdf)}")
        
        # Step 3: Remove high voltage transformers
        if 'voltage' in gdf.columns:
            # Parse voltage - handle various formats
            def parse_voltage(v):
                if pd.isna(v) or v is None or v == '':
                    return 0  # Unknown voltage, keep it
                try:
                    # Handle string formats like "20000", "20 kV", "20000;400"
                    v_str = str(v).lower().replace('kv', '000').replace(' ', '')
                    # Take first value if multiple (e.g., "20000;400" -> "20000")
                    v_str = v_str.split(';')[0].split('/')[0]
                    return float(v_str)
                except (ValueError, TypeError):
                    return 0
            
            gdf['voltage_parsed'] = gdf['voltage'].apply(parse_voltage)
            gdf = gdf[gdf['voltage_parsed'] < VOLTAGE_THRESHOLD]
            gdf = gdf.drop(columns=['voltage_parsed'])
            logger.info(f"After step 3 (remove high voltage >= {VOLTAGE_THRESHOLD}V): {len(gdf)}")
        
        # Step 4: Remove transformers within MIN_DISTANCE_BETWEEN_TRAFOS of each other
        if len(gdf) > 1:
            gdf = drop_close_transformers(gdf, MIN_DISTANCE_BETWEEN_TRAFOS)
            logger.info(f"After step 4 (remove within {MIN_DISTANCE_BETWEEN_TRAFOS}m): {len(gdf)}")
        
        # Step 5: Remove transformers inside shopping areas (optional)
        if shopping_geojson_path and Path(shopping_geojson_path).exists():
            try:
                gdf_shopping = gpd.read_file(shopping_geojson_path)
                if len(gdf_shopping) > 0:
                    gdf_shopping = gdf_shopping.to_crs(target_crs)
                    union_of_shopping = gdf_shopping.geometry.unary_union
                    # Use centroids for within check
                    centroids = gdf.geometry.centroid
                    within_shopping = centroids.within(union_of_shopping)
                    gdf = gdf[~within_shopping]
                    logger.info(f"After step 5 (remove inside shopping): {len(gdf)}")
            except Exception as e:
                logger.warning(f"Could not filter by shopping areas: {e}")
        
        # Clean up temporary columns
        if 'geom_type' in gdf.columns:
            gdf = gdf.drop(columns=['geom_type'])
        if 'area' in gdf.columns:
            gdf = gdf.drop(columns=['area'])
        
        logger.info(f"Filtering complete: {initial_count} -> {len(gdf)} transformers "
                   f"({initial_count - len(gdf)} removed)")
        
        # Save result
        if output_path is None:
            output_path = geojson_path.parent / f"{geojson_path.stem}_filtered.geojson"
        
        gdf.to_file(output_path, driver="GeoJSON")
        logger.info(f"Saved filtered transformers to {output_path}")
        
        return output_path
    
    def convert_to_points(self, geojson_path: Path, output_path: Optional[Path] = None) -> Path:
        """
        Convert all geometries to Points using centroids.

        This ensures compatibility with the pylovo database which expects
        Point geometry for transformers.

        Args:
            geojson_path: Path to input GeoJSON (may contain Points, Lines, Polygons)
            output_path: Optional output path

        Returns:
            Path to GeoJSON with all Point geometries
        """
        logger.info(f"Converting all transformer geometries to Points...")

        if not HAS_GEOPANDAS:
            logger.warning("geopandas not available, using shapely fallback")
            return self._convert_to_points_shapely(geojson_path, output_path)

        from shapely.geometry import Point as ShapelyPoint

        # Use geopandas for efficient conversion
        gdf = gpd.read_file(geojson_path)

        if len(gdf) == 0:
            logger.warning("No features to convert")
            if output_path:
                gdf.to_file(output_path, driver="GeoJSON")
                return output_path
            return geojson_path

        # Log original geometry types
        geom_types = gdf.geometry.geom_type.value_counts().to_dict()
        logger.info(f"Original geometry types: {geom_types}")

        # Save original geometry type BEFORE converting (for correct osm_id prefix later)
        gdf['original_geom_type'] = gdf.geometry.geom_type

        # Convert all geometries to their centroid (Point, not MultiPoint)
        # This matches the original pylovo format
        gdf['geometry'] = gdf.geometry.centroid

        # Verify all are now Points
        new_geom_types = gdf.geometry.geom_type.value_counts().to_dict()
        logger.info(f"Converted geometry types: {new_geom_types}")

        if output_path is None:
            output_path = geojson_path.parent / f"{geojson_path.stem}_points.geojson"

        gdf.to_file(output_path, driver="GeoJSON")
        logger.info(f"Saved {len(gdf)} Point features to {output_path}")

        return output_path

    def _convert_to_points_shapely(self, geojson_path: Path, output_path: Optional[Path] = None) -> Path:
        """Fallback conversion using shapely only."""
        from shapely.geometry import shape, mapping, Point as ShapelyPoint

        with open(geojson_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        converted_features = []
        for feature in data.get("features", []):
            geom = feature.get("geometry")
            if geom:
                try:
                    shp = shape(geom)
                    # Save original geometry type before converting
                    original_geom_type = geom.get("type", "Unknown")
                    props = feature.get("properties", {})
                    props["original_geom_type"] = original_geom_type
                    feature["properties"] = props
                    
                    # Convert to Point (centroid)
                    centroid = shp.centroid
                    feature["geometry"] = mapping(centroid)
                except Exception as e:
                    logger.warning(f"Could not convert geometry: {e}")
                    continue
            converted_features.append(feature)

        data["features"] = converted_features

        if output_path is None:
            output_path = geojson_path.parent / f"{geojson_path.stem}_points.geojson"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

        logger.info(f"Converted {len(converted_features)} features to Points")
        return output_path

    def get_statistics(self, geojson_path: Path) -> Dict[str, Any]:
        """
        Get statistics for transformer dataset.

        Args:
            geojson_path: Path to GeoJSON file

        Returns:
            Dictionary of statistics
        """
        with open(geojson_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        features = data.get("features", [])

        stats = {
            "total_count": len(features),
            "by_voltage_class": {},
            "by_power_type": {},
            "distribution_count": 0,
            "with_operator": 0
        }

        for f in features:
            props = f.get("properties", {})

            # Count by voltage class
            v_class = props.get("voltage_class", "unknown")
            stats["by_voltage_class"][v_class] = stats["by_voltage_class"].get(v_class, 0) + 1

            # Count by power type
            p_type = props.get("power", "unknown")
            stats["by_power_type"][p_type] = stats["by_power_type"].get(p_type, 0) + 1

            # Count distribution transformers
            if props.get("is_distribution") or self.is_distribution_transformer(props):
                stats["distribution_count"] += 1

            # Count with operator info
            if props.get("operator") and props.get("operator") != "unknown":
                stats["with_operator"] += 1

        return stats
