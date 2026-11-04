"""Unit tests for the dwelling count stored as households_per_building.

No network and no database. Run from the project root:

    python -m unittest tests.test_dwellings -v
"""
import math
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from datapipeline.downloaders.buildings import estimate_dwellings
from datapipeline.main import DataPipeline


class EstimateDwellingsTest(unittest.TestCase):
    def test_house_is_one_dwelling_whatever_its_size(self):
        self.assertEqual(estimate_dwellings("house", 400.0, 3), 1)
        self.assertEqual(estimate_dwellings("terrace", 120.0, 2), 1)

    def test_apartments_count_gross_floor_area(self):
        self.assertEqual(estimate_dwellings("apartments", 400.0, 4), 18)  # 1600 m² / 90

    def test_small_untyped_building_is_one_house(self):
        self.assertEqual(estimate_dwellings("yes", 150.0, 2), 1)  # 300 m² < 400 m²

    def test_large_untyped_building_counts_by_area_unless_non_residential(self):
        self.assertEqual(estimate_dwellings("yes", 300.0, 3, "residential"), 10)
        self.assertEqual(estimate_dwellings("yes", 300.0, 3), 10)
        self.assertEqual(estimate_dwellings("yes", 300.0, 3, "non-residential"), 1)

    def test_missing_floors_count_as_one(self):
        self.assertEqual(estimate_dwellings("yes", 500.0, math.nan), 6)
        self.assertEqual(estimate_dwellings("apartments", 500.0, None), 6)


class RecountDwellingsTest(unittest.TestCase):
    def test_recount_uses_enriched_floors_and_type(self):
        gdf = gpd.GeoDataFrame(
            {
                "Building_T": ["yes", "yes", "house"],
                "Area": [300.0, 300.0, 300.0],
                "Floors": [3.0, 3.0, 3.0],
                "type_eubucco": ["residential", "non-residential", "residential"],
                "Occupants": [99, 99, 99],
            },
            geometry=[box(0, 0, 10, 30), box(100, 0, 110, 30), box(200, 0, 210, 30)],
            crs="EPSG:3035",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Res_1_enriched_eubucco.gpkg"
            gdf.to_file(path, driver="GPKG")
            DataPipeline._recount_dwellings(Path(tmp))
            self.assertEqual(gpd.read_file(path)["Occupants"].tolist(), [10, 1, 1])


if __name__ == "__main__":
    unittest.main()
