"""
Building data processor for classification and validation.
"""

import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from ..building_constants import NO_ELECTRICITY_BUILDING_TYPES
from ..downloaders.buildings import estimate_dwellings

try:
    import geopandas as gpd
    import pandas as pd
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False

logger = logging.getLogger("datapipeline")


class BuildingProcessor:
    """Process and classify building data."""
    
    # Building type classification based on OSM f_class values
    # Reference: https://wiki.openstreetmap.org/wiki/Key:building
    RESIDENTIAL_TYPES = {
        'residential', 'house', 'apartments', 'detached', 
        'semidetached_house', 'terrace', 'dormitory', 'bungalow',
        'farm', 'farmhouse', 'cabin',
        'houseboat', 'boathouse', 'boat_house',
        'stilt_house', 'conservatory',
    }
    
    COMMERCIAL_TYPES = {
        'commercial', 'retail', 'office', 'supermarket',
        'shop', 'kiosk', 'mall', 'store', 'marketplace',
        'hotel', 'motel', 'hostel', 'guest_house',
        'restaurant', 'cafe', 'pub', 'bar', 'fast_food',
        'bakehouse', 'bakery', 'bank', 'pharmacy'
    }
    
    INDUSTRIAL_TYPES = {
        'industrial', 'warehouse', 'factory', 'manufacture',
        'digester', 'works', 'workshop',
        'brewery', 'winery', 'oil_mill', 'sawmill', 'watermill', 'windmill'
    }
    
    PUBLIC_TYPES = {
        'public', 'civic', 'government', 'hospital', 'school',
        'university', 'kindergarten', 'church', 'chapel', 'mosque',
        'temple', 'cathedral', 'synagogue', 'shrine', 'monastery',
        'library', 'museum', 'theatre', 'cinema', 'community_centre',
        'fire_station', 'police', 'townhall', 'courthouse', 'embassy',
        'sports_hall', 'stadium', 'grandstand', 'pavilion', 'sports_centre',
        'toilets', 'public_bath', 'healthcare', 'clinic',
        'nursing_home', 'social_facility', 'prison', 'military',
        'religious', 'college', 'training',
        'barracks', 'presbytery', 'kingdom_hall',
    }
    
    INFRASTRUCTURE_TYPES = {
        'train_station', 'transportation', 'station', 'terminal',
        'parking', 'garage', 'garages', 'hangar',
        'service',
        'electricity',
        'data_center', 'utility', 'power', 'substation'
    }
    
    AGRICULTURAL_TYPES = {
        'barn', 'cowshed', 'stable', 'greenhouse',
        'farm_auxiliary', 'riding_hall',
        'chicken_coop', 'livestock', 'agricultural'
    }
    
    # Generic/Unknown types that appear frequently in OSM
    GENERIC_TYPES = {
        'yes', 'building',
        'unclassified', 'other', 'mixed_use'
    }
    
    # Building types that do not consume electricity — excluded from processing.
    NO_ELECTRICITY_TYPES = NO_ELECTRICITY_BUILDING_TYPES
    
    # f_class to consumer category mapping for pylovo
    # Categories: H=Household, C=Commercial, I=Industrial, P=Public, A=Agricultural, U=Unknown
    F_CLASS_TO_CATEGORY = {
        # Residential -> Household
        'residential': 'H',
        'house': 'H',
        'apartments': 'H',
        'detached': 'H',
        'semidetached_house': 'H',
        'terrace': 'H',
        'dormitory': 'H',
        'bungalow': 'H',
        'farmhouse': 'H',
        'cabin': 'H',
        'boathouse': 'H',
        'boat_house': 'H',
        'houseboat': 'H',
        'stilt_house': 'H',
        'conservatory': 'H',
        # Commercial
        'commercial': 'C',
        'retail': 'C',
        'office': 'C',
        'supermarket': 'C',
        'shop': 'C',
        'mall': 'C',
        'kiosk': 'C',
        'store': 'C',
        'marketplace': 'C',
        'hotel': 'C',
        'motel': 'C',
        'hostel': 'C',
        'guest_house': 'C',
        'restaurant': 'C',
        'cafe': 'C',
        'pub': 'C',
        'bar': 'C',
        'fast_food': 'C',
        'bakehouse': 'C',
        'bakery': 'C',
        'bank': 'C',
        'pharmacy': 'C',
        # Industrial
        'industrial': 'I',
        'warehouse': 'I',
        'factory': 'I',
        'manufacture': 'I',
        'digester': 'I',
        'works': 'I',
        'workshop': 'I',
        'brewery': 'I',
        'winery': 'I',
        'oil_mill': 'I',
        'sawmill': 'I',
        'watermill': 'I',
        'windmill': 'I',
        # Public
        'public': 'P',
        'civic': 'P',
        'government': 'P',
        'school': 'P',
        'hospital': 'P',
        'church': 'P',
        'university': 'P',
        'kindergarten': 'P',
        'chapel': 'P',
        'mosque': 'P',
        'temple': 'P',
        'cathedral': 'P',
        'synagogue': 'P',
        'shrine': 'P',
        'monastery': 'P',
        'library': 'P',
        'museum': 'P',
        'theatre': 'P',
        'cinema': 'P',
        'community_centre': 'P',
        'fire_station': 'P',
        'police': 'P',
        'townhall': 'P',
        'courthouse': 'P',
        'embassy': 'P',
        'sports_hall': 'P',
        'stadium': 'P',
        'grandstand': 'P',
        'pavilion': 'P',
        'sports_centre': 'P',
        'toilets': 'P',
        'public_bath': 'P',
        'healthcare': 'P',
        'clinic': 'P',
        'nursing_home': 'P',
        'social_facility': 'P',
        'prison': 'P',
        'military': 'P',
        'religious': 'P',
        'college': 'P',
        'training': 'P',
        'barracks': 'P',
        'presbytery': 'P',
        'kingdom_hall': 'P',
        # Agricultural
        'farm': 'A',
        'barn': 'A',
        'cowshed': 'A',
        'stable': 'A',
        'greenhouse': 'A',
        'farm_auxiliary': 'A',
        'riding_hall': 'A',
        'chicken_coop': 'A',
        'livestock': 'A',
        'agricultural': 'A',
        # Infrastructure (mapped to Industrial for load calculation)
        'train_station': 'I',
        'transportation': 'I',
        'station': 'I',
        'terminal': 'I',
        'parking': 'I',
        'garage': 'I',
        'garages': 'I',
        'hangar': 'I',
        'service': 'I',
        'electricity': 'I',
        'data_center': 'I',
        'utility': 'I',
        'power': 'I',
        'substation': 'I',
        # Generic/Unknown
        'yes': 'U',
        'building': 'U',
        'unclassified': 'U',
        'other': 'U',
        'mixed_use': 'U',
    }
    
    def __init__(self, region_config: Dict[str, Any]):
        """Initialize the processor."""
        self.region_config = region_config
        
        if not HAS_GEOPANDAS:
            logger.warning("geopandas not available. Some processing features disabled.")
    
    def classify_building(self, f_class: Optional[str]) -> str:
        """
        Classify a building based on its f_class (building type).
        
        Args:
            f_class: OSM building type tag (f_class field)
        
        Returns:
            Classification category
        """
        if not f_class:
            return "unknown"
        
        f_class = f_class.lower().strip()
        
        if f_class in self.RESIDENTIAL_TYPES:
            return "residential"
        elif f_class in self.COMMERCIAL_TYPES:
            return "commercial"
        elif f_class in self.INDUSTRIAL_TYPES:
            return "industrial"
        elif f_class in self.PUBLIC_TYPES:
            return "public"
        elif f_class in self.INFRASTRUCTURE_TYPES:
            return "infrastructure"
        elif f_class in self.AGRICULTURAL_TYPES:
            return "agricultural"
        elif f_class == "yes":
            return "unspecified"
        else:
            return "other"
    
    def get_consumer_category(self, f_class: Optional[str]) -> str:
        """
        Get pylovo consumer category from f_class.
        
        Args:
            f_class: OSM building type tag
        
        Returns:
            Consumer category code (H, C, I, P, A, U)
        """
        if not f_class:
            return "U"
        
        f_class = f_class.lower().strip()
        return self.F_CLASS_TO_CATEGORY.get(f_class, "U")
    
    def estimate_households(self, f_class: str, area: float, levels: Optional[int] = None) -> int:
        """
        Estimate number of households in a building.
        
        Args:
            f_class: Building f_class/type
            area: Building footprint area in m²
            levels: Number of building levels (floors)
        
        Returns:
            Estimated number of households
        """
        if not f_class:
            f_class = 'yes'
        f_class = f_class.lower().strip()

        # Only residential f_classes get household estimates
        if f_class not in self.RESIDENTIAL_TYPES and f_class not in self.GENERIC_TYPES:
            return 0
        
        # Average floor area per household (m²)
        AVG_HOUSEHOLD_AREA = 90
        
        # Use provided levels or estimate
        if levels and levels > 0:
            num_floors = levels
        else:
            # Estimate floors based on f_class
            if f_class in ('apartments', 'dormitory'):
                num_floors = 4  # Multi-story
            elif f_class in ('house', 'detached', 'bungalow'):
                num_floors = 1.5  # Single family with attic
            else:
                num_floors = 2  # Default
        
        total_area = area * num_floors
        households = max(1, int(total_area / AVG_HOUSEHOLD_AREA))
        
        return households
    
    def get_pylovo_type(self, f_class: Optional[str]) -> str:
        """
        Return the f_class directly as the pylovo building type (lowercase).
        
        Returns granular f_class values so each building type can map to its own
        peak load definition in consumer_categories.
        """
        if not f_class:
            return "yes"  # Default for unknown
        
        return f_class.lower().strip()

    def process_shapefile(self, shp_path: Path, output_path: Optional[Path] = None) -> Path:
        """
        Process a building vector file with classification and household estimation.
        
        Uses capitalized column names to match pylovo database expectations:
        - Building_T (not building_t)
        - Floors (not floors)
        - Occupants (not occupants)
        - Area (not area)
        
        Args:
            shp_path: Path to input building file (.gpkg expected)
            output_path: Optional output path
        
        Returns:
            Path to processed file
        """
        if not HAS_GEOPANDAS:
            raise RuntimeError("geopandas required for shapefile processing")
        
        logger.info(f"Processing buildings from {shp_path}")
        
        gdf = gpd.read_file(shp_path)
        
        # Get f_class column (may be 'f_class' or 'building')
        f_class_col = 'f_class' if 'f_class' in gdf.columns else 'building'
        
        # Ensure f_class exists
        if f_class_col not in gdf.columns:
            gdf[f_class_col] = 'yes'
        else:
            gdf[f_class_col] = gdf[f_class_col].fillna('yes')

        # Normalize class tokens before filtering/classification.
        gdf[f_class_col] = gdf[f_class_col].astype(str).str.lower().str.strip()

        # Exclude structures that are not meaningful electricity consumers.
        no_elec_mask = gdf[f_class_col].isin(self.NO_ELECTRICITY_TYPES)
        excluded_count = int(no_elec_mask.sum())
        if excluded_count > 0:
            logger.info(
                "Excluding %s buildings with no electricity usage in processor "
                "(types: %s)",
                excluded_count,
                gdf.loc[no_elec_mask, f_class_col].value_counts().to_dict(),
            )
            gdf = gdf.loc[~no_elec_mask].copy()

        # Add pylovo classification (Building_T - capitalized to match pylovo!)
        # Vectorized: f_class_col is already lowercased/stripped from line 360
        gdf['Building_T'] = gdf[f_class_col].fillna('yes')
        
        # Add consumer category (vectorized via .map)
        gdf['cons_cat'] = gdf[f_class_col].map(self.F_CLASS_TO_CATEGORY).fillna('U')
        
        # Calculate area from geometry (Area - capitalized to match pylovo!)
        gdf['Area'] = gdf.geometry.area

        # Exclude very small buildings (< 25 m²) which are likely sheds/garages
        small_bldg_mask = gdf['Area'] < 25
        small_excluded_count = int(small_bldg_mask.sum())
        if small_excluded_count > 0:
            logger.info(
                "Excluding %s buildings with area < 25m2",
                small_excluded_count,
            )
            gdf = gdf.loc[~small_bldg_mask].copy()
        
        # Get levels if available
        levels_col = 'levels' if 'levels' in gdf.columns else ('Floors' if 'Floors' in gdf.columns else None)
        levels = pd.to_numeric(gdf[levels_col], errors='coerce') if levels_col else pd.Series(float('nan'), index=gdf.index)
        levels = levels.where(levels > 0)

        # Populate 'Floors' explicitly (vectorized, capitalized to match pylovo!)
        gdf['Floors'] = 2  # default
        gdf.loc[gdf[f_class_col].isin(['apartments', 'dormitory']), 'Floors'] = 4
        gdf.loc[gdf[f_class_col] == 'bungalow', 'Floors'] = 1
        if levels_col:
            # Override with actual levels where available and numeric
            has_levels = pd.to_numeric(gdf[levels_col], errors='coerce')
            valid_mask = has_levels.notna() & (has_levels > 0)
            gdf.loc[valid_mask, 'Floors'] = has_levels[valid_mask].astype(int)

        # Dwellings by the download step's rule. Floors above carries type defaults, so only
        # explicit levels are passed; the EUBUCCO type is not known yet at this stage.
        gdf['Occupants'] = [
            estimate_dwellings(t, a, f) for t, a, f in zip(gdf['Building_T'], gdf['Area'], levels)
        ]
        is_res = gdf[f_class_col].isin(self.RESIDENTIAL_TYPES | self.GENERIC_TYPES)
        gdf.loc[~is_res, 'Occupants'] = 0
        
        # Calculate centroid for point representation
        centroids = gdf.geometry.centroid
        gdf['centroid_x'] = centroids.x
        gdf['centroid_y'] = centroids.y
        
        # Rename f_class column to standard 'f_class' if needed
        if f_class_col != 'f_class':
            gdf['f_class'] = gdf[f_class_col]

        # Preserve all detected classes (lossless path via GPKG).
        if 'f_classes' not in gdf.columns:
            gdf['f_classes'] = gdf['f_class']
        else:
            gdf['f_classes'] = gdf['f_classes'].fillna(gdf['f_class'])
            gdf.loc[gdf['f_classes'].astype(str).str.strip() == '', 'f_classes'] = gdf['f_class']
        
        if output_path is None:
            output_path = shp_path.parent / f"{shp_path.stem}_processed.gpkg"
        elif output_path.suffix.lower() != ".gpkg":
            output_path = output_path.with_suffix(".gpkg")

        if output_path.exists():
            output_path.unlink()
        gdf.to_file(output_path, driver="GPKG")
        logger.info(f"Processed {len(gdf)} buildings, saved to {output_path}")
        
        return output_path
    
    def get_statistics(self, shp_path: Path) -> Dict[str, Any]:
        """
        Get statistics for a building dataset.
        
        Args:
            shp_path: Path to shapefile
        
        Returns:
            Dictionary of statistics
        """
        if not HAS_GEOPANDAS:
            raise RuntimeError("geopandas required for statistics")
        
        gdf = gpd.read_file(shp_path)
        
        stats = {
            "total_buildings": len(gdf),
            "total_area_m2": float(gdf.geometry.area.sum()),
            "avg_area_m2": float(gdf.geometry.area.mean()),
        }
        
        # f_class distribution
        f_class_col = 'f_class' if 'f_class' in gdf.columns else 'building'
        if f_class_col in gdf.columns:
            stats["by_f_class"] = gdf[f_class_col].value_counts().head(20).to_dict()
        
        if 'bldg_class' in gdf.columns:
            stats["by_classification"] = gdf['bldg_class'].value_counts().to_dict()
        
        if 'cons_cat' in gdf.columns:
            stats["by_consumer_category"] = gdf['cons_cat'].value_counts().to_dict()
        
        if 'est_hh' in gdf.columns:
            stats["total_households"] = int(gdf['est_hh'].sum())
        
        return stats
