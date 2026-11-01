"""Unit tests for the EUBUCCO enricher (datapipeline/enrichment/eubucco.py).

No network and no database. Run from the project root:

    python -m unittest tests.test_eubucco -v
"""
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from datapipeline.enrichment.eubucco import EUBUCCOEnricher, gpkg_urls, select_nuts2

# Two adjacent German NUTS 2 squares and one Austrian one, in EPSG:3035 metres.
BOUNDARIES = gpd.GeoDataFrame(
    {"NUTS_ID": ["DE13", "DE14", "AT11"], "CNTR_CODE": ["DE", "DE", "AT"]},
    geometry=[box(0, 0, 10_000, 10_000), box(10_000, 0, 20_000, 10_000), box(0, 0, 20_000, 10_000)],
    crs="EPSG:3035",
)


class SelectNuts2Test(unittest.TestCase):
    def test_area_inside_one_region(self):
        self.assertEqual(select_nuts2(BOUNDARIES, "DE", (2_000, 2_000, 3_000, 3_000)), ["DE13"])

    def test_area_near_border_takes_both_regions(self):
        self.assertEqual(select_nuts2(BOUNDARIES, "DE", (8_500, 2_000, 9_000, 3_000)), ["DE13", "DE14"])

    def test_other_country_is_ignored(self):
        self.assertNotIn("AT11", select_nuts2(BOUNDARIES, "DE", (2_000, 2_000, 3_000, 3_000)))


class GpkgUrlsTest(unittest.TestCase):
    def test_picks_gpkg_file_per_partition(self):
        listing = [{
            "nuts_id": "DE13",
            "files": [
                {"key": "v0.2/buildings/parquet/nuts_id=DE13/DE13.parquet", "download_url": "https://x/DE13.parquet"},
                {"key": "v0.2/buildings/gpkg/nuts_id=DE13/DE13.gpkg", "download_url": "https://x/DE13.gpkg"},
            ],
        }]
        self.assertEqual(gpkg_urls(listing), {"DE13": "https://x/DE13.gpkg"})


class SpatialJoinTest(unittest.TestCase):
    def test_v02_columns_become_eubucco_fields(self):
        buildings = gpd.GeoDataFrame({"Floors": [None]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:3035")
        eubucco = gpd.GeoDataFrame(
            {"floors": [2.6], "height": [8.4], "construction_year": [1975], "type": ["residential"]},
            geometry=[box(-1, -1, 11, 11)],
            crs="EPSG:3035",
        )
        with tempfile.TemporaryDirectory() as cache:
            enricher = EUBUCCOEnricher({"country": "germany"}, cache_dir=Path(cache))
            joined = enricher._spatial_join(buildings, eubucco)
        self.assertEqual(joined.loc[0, "age_eubucco"], 1975)
        self.assertEqual(joined.loc[0, "Floors"], 3)


if __name__ == "__main__":
    unittest.main()
